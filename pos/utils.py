# Copyright (c) 2026 and contributors
# For license information, please see license.txt

"""Shared helpers for the Self Checkout backend.

Kept in one low-level module (imported by ``api`` / ``payments`` / ``stock`` / ``realtime``) so those
never import each other — no import cycles. Concurrency helpers mirror the reference app's
``rfid_tracking.api`` (optimistic compare-and-swap, deadlock-only retry, clean 409 conflicts).
"""

from __future__ import annotations

import functools
import json
import time

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, today

# Transient DB errors we retry; a logical CheckoutConflict is NEVER retried.
_RETRIES = 4
_BACKOFF = 0.05  # seconds, multiplied by the attempt number

SETTINGS_DOCTYPE = "Checkout Settings"
SESSION_DOCTYPE = "Checkout Session"

# Backend identity for the caller-less webhook path (no logged-in user to check permissions against).
# Holds only the roles needed to submit the sale — never used on a user-facing endpoint. See install.py.
SERVICE_USER = "checkout.service@selfcheckout.local"


class CheckoutConflict(frappe.ValidationError):
    """A concurrent write already changed the session; the caller should re-read and retry."""

    http_status_code = 409  # Frappe reads this attribute to set the HTTP response code


def conflict(msg: str) -> None:
    frappe.throw(_(msg), exc=CheckoutConflict, title=_("Conflict"))


def _is_db_lock(e) -> bool:
    """True for a transient MariaDB deadlock (1213) / lock-wait timeout (1205) worth retrying."""
    code = (getattr(e, "args", None) or [None])[0]
    if code in (1213, 1205):
        return True
    for cls in ("QueryDeadlockError", "QueryTimeoutError"):
        exc = getattr(frappe.db, cls, None)
        if exc and isinstance(e, exc):
            return True
    return False


def with_deadlock_retry(fn):
    """Retry the wrapped function on a transient DB deadlock/lock-wait timeout; never on a conflict."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(1, _RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except CheckoutConflict:
                raise  # logical conflict -> fail as intended, do not retry
            except Exception as e:
                if attempt < _RETRIES and _is_db_lock(e):
                    frappe.db.rollback()
                    time.sleep(_BACKOFF * attempt)
                    continue
                raise

    return wrapper


def cas_session_status(name: str, from_status: str, to_status: str) -> int:
    """Atomically flip a Checkout Session from_status -> to_status. Returns rows actually flipped (0/1).

    The ``WHERE status = from_status`` clause is the compare; the affected-row count is the swap result.
    Lets the webhook apply a result exactly once even if it is delivered twice concurrently.
    """
    # Raw SQL bypasses Frappe's permission layer — gate it explicitly.
    frappe.has_permission(SESSION_DOCTYPE, "write", throw=True)
    frappe.db.sql(
        """UPDATE `tabCheckout Session` SET status=%(to)s, modified=%(now)s, modified_by=%(user)s
           WHERE name=%(name)s AND status=%(frm)s""",
        {
            "to": to_status,
            "frm": from_status,
            "now": now_datetime(),
            "user": frappe.session.user,
            "name": name,
        },
    )
    return frappe.db._cursor.rowcount


def get_settings():
    """The Checkout Settings single doc."""
    return frappe.get_single(SETTINGS_DOCTYPE)


def resolve_pos_profile(user: str | None = None):
    """Return the enabled POS Profile the given user (default: session user) is allowed to sell on.

    The cashier -> shop mapping is native: a POS Profile lists its cashiers in ``applicable_for_users``.
    Throws a clean error if the user has no enabled profile (this is the app's shop-gating).
    """
    user = user or frappe.session.user
    parents = frappe.get_all(
        "POS Profile User", filters={"user": user}, fields=["parent"], pluck="parent"
    )
    profile = None
    if parents:
        enabled = frappe.get_all(
            "POS Profile", filters={"name": ["in", parents], "disabled": 0}, pluck="name", limit=1
        )
        profile = enabled[0] if enabled else None
    if not profile:
        frappe.throw(
            _("No POS Profile is configured for {0}. Ask an administrator to add you to a shop.").format(
                user
            )
        )
    return frappe.get_doc("POS Profile", profile)


def parse_items(items_json) -> list[dict]:
    """Accept a JSON string or an already-decoded list of cart line dicts; validate shape.

    Each entry needs ``item_code`` and a positive ``qty``; ``barcode`` is optional. Rejects malformed
    JSON / bad shapes with a clean 400 instead of a 500.
    """
    if items_json is None:
        frappe.throw(_("items_json is required."))
    if isinstance(items_json, str):
        try:
            items_json = json.loads(items_json or "[]")
        except (ValueError, TypeError):
            frappe.throw(_("items_json must be valid JSON (a list of cart lines)."))
    if not isinstance(items_json, (list, tuple)):
        frappe.throw(_("items_json must be a JSON array of cart lines."))

    lines = []
    for raw in items_json:
        if not isinstance(raw, dict):
            frappe.throw(_("Each cart line must be an object with item_code and qty."))
        code = (raw.get("item_code") or "").strip()
        qty = cint(raw.get("qty"))
        if not code:
            frappe.throw(_("A cart line is missing item_code."))
        if qty <= 0:
            frappe.throw(_("Quantity for {0} must be a positive whole number.").format(code))
        lines.append({"item_code": code, "qty": qty, "barcode": (raw.get("barcode") or "").strip()})
    if not lines:
        frappe.throw(_("The cart is empty."))
    return lines


def effective_rates(item_codes, price_list, *, warehouse=None, qty=1) -> dict:
    """Post-Pricing-Rule selling unit rates ``{item_code: rate}`` for the given items on ``price_list``.

    Prices through the SAME engine the POS Invoice uses (``get_item_details``), so a price shown to the
    shopper equals what checkout will charge. Overlays the engine result on the raw Item Price and never
    throws: any item the engine can't price falls back to its raw ``price_list_rate``; only truly unpriced
    items are omitted (callers treat a missing rate as "no selling price").

    LIMITATION: a catalogue preview has no cart or named customer yet, so this evaluates rules at ``qty``
    (default 1) for the walk-in customer. Rules whose CONDITIONS depend on the cart or party (quantity
    breaks, min-order amount, customer group, buy-X-get-Y) can therefore only be previewed; the POS
    Invoice recomputes every rule against the real cart at checkout, so the amount CHARGED is always
    correct — only this preview can differ for those rule types. A flat item/group markup previews exactly.
    """
    codes = [c for c in dict.fromkeys(item_codes) if c]
    if not codes:
        return {}

    # Raw baseline (also the per-item fallback if the pricing engine can't price something).
    raw: dict = {}
    for r in frappe.get_all(
        "Item Price",
        filters={"price_list": price_list, "selling": 1, "item_code": ["in", codes]},
        fields=["item_code", "price_list_rate"],
    ):
        raw.setdefault(r.item_code, flt(r.price_list_rate))

    # Engine overlay: Pricing Rules / margins, computed by a throwaway in-memory Sales Invoice.
    engine: dict = {}
    try:
        engine = _engine_rates(codes, price_list, warehouse, qty)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Self Checkout: pricing-rule preview failed")

    out: dict = {}
    for code in codes:
        rate = engine.get(code) or raw.get(code)
        if rate:
            out[code] = flt(rate)
    return out


def _engine_rates(codes, price_list, warehouse, qty) -> dict:
    """Run ERPNext's selling pricing engine over ``codes`` in one in-memory **Sales Invoice** (never
    inserted, no side effects); return ``{item_code: post-rule rate}``. Sales Invoice — not POS Invoice —
    so it never touches POS opening entries and is safe to call while merely browsing the catalogue;
    Pricing Rules are keyed on the *selling* transaction, so the resulting rate matches the POS charge."""
    settings = get_settings()
    customer = settings.default_customer
    if not customer:
        return {}  # no party -> the engine can't run pricing rules; caller falls back to the raw price
    inv = frappe.new_doc("Sales Invoice")
    inv.customer = customer
    inv.company = settings.company or frappe.db.get_default("company")
    inv.currency = settings.default_currency or "SGD"
    inv.selling_price_list = price_list
    inv.set_posting_time = 1
    inv.posting_date = today()
    inv.update_stock = 0
    for code in codes:
        row = {"item_code": code, "qty": qty}
        if warehouse:
            row["warehouse"] = warehouse
        inv.append("items", row)
    inv.set_missing_values()
    inv.run_method("calculate_taxes_and_totals")
    rates: dict = {}
    for row in inv.items:
        rates.setdefault(row.item_code, flt(row.rate))
    return rates


def item_price(item_code: str, price_list: str, *, warehouse=None, qty=1) -> float:
    """The effective selling rate for one item on a price list, AFTER Pricing Rules (see
    ``effective_rates``). Throws if the item has no selling price at all — prices are ALWAYS taken from
    ERPNext here, never trusted from the device."""
    rate = effective_rates([item_code], price_list, warehouse=warehouse, qty=qty).get(item_code)
    if not rate:
        frappe.throw(_("No selling price for {0} on price list {1}.").format(item_code, price_list))
    return flt(rate)
