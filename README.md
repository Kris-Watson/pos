# Self Checkout (`t3x_pos`)

Server-side Frappe/ERPNext **v16** app for the self-checkout POS. It is the payment proxy and the
system of record for sales: the Android till (`com.t3x.pos`) posts a cart, this app creates the HitPay
payment request, receives the HitPay **webhook**, and on success records the sale natively in ERPNext
(POS Invoice + payment to a HitPay clearing account, stock movement, POS day sessions).

Accounting runs in the background from day one but is **hidden from the cashier** — the till never shows
invoices or GL. "Show accounting later" is just unhiding ERPNext reports; there is no backfill.

> The `t3x_pos` package name is the only place the internal project name appears. Everything a Desk,
> ERP or POS user sees is branding-neutral ("Self Checkout", "Shop Cashier", "Checkout …" DocTypes).

## Layout

- `t3x_pos/hooks.py` — app metadata, `required_apps=["erpnext"]`, `after_install`, `doc_events`.
- `t3x_pos/api.py` — whitelisted endpoints: login context, catalogue delta-sync, checkout, day sessions.
- `t3x_pos/payments.py` — HitPay client + the `hitpay_webhook` receiver (HMAC-verified, idempotent).
- `t3x_pos/stock.py` — paid session → POS Invoice conversion + the realtime/end-of-day stock arms.
- `t3x_pos/install.py` — idempotent bootstrap (role, HitPay Mode of Payment + account, Walk-in Customer,
  Checkout Settings).
- `t3x_pos/realtime.py` — user-targeted `checkout_update` push.
- `t3x_pos/self_checkout/doctype/` — custom DocTypes: Checkout Settings, Checkout Session (+ Item),
  Checkout Payment Event.

## Install

```
bench get-app ./t3x_pos            # or: bench get-app <repo-url>
bench --site <site> install-app t3x_pos
bench --site <site> migrate
```

Then configure **Checkout Settings** (HitPay key + salt, environment, stock mode) and create a **POS
Profile** per shop with its warehouse, price list, applicable cashier user, and the "HitPay" payment
method.
