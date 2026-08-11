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

Two operational requirements follow:

1. **Cashier users need `Shop Cashier` + `Accounts User`.** The till builds the *draft* POS Invoice under
   the cashier's own permissions (no bypass); Accounts User grants the POS Invoice create + accounting
   reads that requires. Assign both roles to each cashier user (alongside the POS Profile mapping).
2. **A manager must run `open_day` at shop start.** Opening the POS session creates a POS Opening Entry,
   which needs `Sales Manager` — deliberately *not* a cashier right. Until the day is open, `create_session`
   refuses with a clear "ask a manager to open the day" error (before any payment is taken). `close_day`
   likewise runs as a manager (`Sales Manager` + `Stock User` for the end-of-day stock arm).
