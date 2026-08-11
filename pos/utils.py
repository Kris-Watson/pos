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
from frappe.utils import cint, flt, now_datetime

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


def item_price(item_code: str, price_list: str) -> float:
    """The current selling rate for an item on a price list. Throws if the item isn't priced there —
    prices are ALWAYS taken from ERPNext here, never trusted from the device."""
    rate = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": price_list, "selling": 1},
        "price_list_rate",
    )
    if rate is None:
        frappe.throw(_("No selling price for {0} on price list {1}.").format(item_code, price_list))
    return flt(rate)
