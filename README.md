# Self Checkout (`pos`)

Server-side Frappe/ERPNext **v16** app for the self-checkout POS. It is the payment proxy and the
system of record for sales: the Android till (`com.t3x.pos`) posts a cart, this app creates the HitPay
payment request, receives the HitPay **webhook**, and on success records the sale natively in ERPNext
(POS Invoice + payment to a HitPay clearing account, stock movement, POS day sessions).

Accounting runs in the background from day one but is **hidden from the cashier** — the till never shows
invoices or GL. "Show accounting later" is just unhiding ERPNext reports; there is no backfill.

> The `pos` package name is the only place the internal project name appears. Everything a Desk,
> ERP or POS user sees is branding-neutral ("Self Checkout", "Shop Cashier", "Checkout …" DocTypes).

## Layout

- `pos/hooks.py` — app metadata, `required_apps=["erpnext"]`, `after_install`, `doc_events`.
- `pos/api.py` — whitelisted endpoints: login context, catalogue delta-sync, checkout, day sessions.
- `pos/payments.py` — HitPay client + the `hitpay_webhook` receiver (HMAC-verified, idempotent).
- `pos/stock.py` — paid session → POS Invoice conversion + the realtime/end-of-day stock arms.
- `pos/install.py` — idempotent bootstrap (role, HitPay Mode of Payment + account, Walk-in Customer,
  Checkout Settings).
- `pos/realtime.py` — user-targeted `checkout_update` push.
- `pos/self_checkout/doctype/` — custom DocTypes: Checkout Settings, Checkout Session (+ Item),
  Checkout Payment Event.

## Install

```
bench get-app /path/to/pos-app   # this repo's root (the folder containing setup.py), or a git URL
bench --site <site> install-app pos
bench --site <site> migrate
```

Then configure **Checkout Settings** (HitPay key + salt, environment, stock mode) and create a **POS
Profile** per shop with its warehouse, price list, applicable cashier user, and the "HitPay" payment
method.

## Permission model

Every API call runs as its real caller and is permission-checked natively — the only elevation is the
caller-less HitPay webhook, which runs as a dedicated, login-less service account (`checkout.service@…`,
created by `after_install`) holding just the roles a sale needs (Accounts User, Sales Manager, Stock User).

The **`Shop Cashier` role is self-contained**: `after_install` grants it, under the cashier's own
identity, every permission the till needs — read on the catalogue + pricing masters (Item, Item Price,
Account, Cost Center, Pricing Rule, tax templates, POS Profile, …) and create/submit on the POS
transaction docs it writes (draft POS Invoice — *create only*, the webhook submits; POS Opening Entry;
POS Closing Entry; Stock Entry). So **assigning a user the single `Shop Cashier` role (plus the POS
Profile mapping) is all that's needed** — no `Accounts User`, no `Sales Manager`, no separate manager.
The cashier opens the day (`open_day`), sells, and closes it (`close_day`) themselves. Until the day is
open, `create_session` refuses cleanly ("open the day before checking out") before any payment is taken.

> **Note on how the grants work.** Frappe's only mechanism for adding a role to an existing doctype's
> permissions is `Custom DocPerm`, and once any Custom DocPerm exists for a doctype its permissions become
> Custom-DocPerm-managed *site-wide* (later ERPNext upgrades to that doctype's standard perms won't
> auto-apply). `after_install` uses Frappe's additive API (`add_permission`), which copies the standard
> perms in first so no existing role loses access — but the doctypes it touches (POS Invoice/Opening/
> Closing Entry, Stock Entry, Account, Cost Center, Pricing Rule, tax templates, …) are thereafter
> custom-managed. That's an acceptable trade for a single-purpose POS site; on a shared ERP you'd instead
> assign the bundled ERPNext roles.
