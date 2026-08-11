# Copyright (c) 2026 and contributors
# For license information, please see license.txt

"""Turn a paid Checkout Session into a native ERPNext sale, and run the stock experiment.

On payment success a session becomes a submitted **POS Invoice** (accounting + payment to the HitPay
clearing account). The **stock arm** is chosen by Checkout Settings ``stock_mode``:

* **Realtime** — the POS Invoice carries ``update_stock=1``, so the stock ledger moves at sale time
  (one batch per sale).
* **End of Day** — the POS Invoice carries ``update_stock=0``; stock is left untouched until
  ``close_day`` posts a single consolidated Stock Entry for the day's aggregate sold qty (one batch
  per day). ``close_day`` also creates the native POS Closing Entry that consolidates the session.

The two arms are directly comparable (N stock batches vs 1) — that comparison is the whole point of the
toggle. All work here runs in a trusted context (webhook-triggered system job / whitelisted day ops).

NOTE (verify on the v16 bench): POS Invoice submission may require an open POS Opening Entry, and the
exact POS Closing Entry helper name. Both are handled defensively below and flagged for confirmation.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, today

from t3x_pos.utils import get_settings, resolve_pos_profile


def convert_session_to_invoice(session_name: str) -> str | None:
    """Create + submit the POS Invoice for a Paid session. Idempotent: a session that already has an
    invoice is left alone. Returns the POS Invoice name (or the existing one)."""
    session = frappe.get_doc("Checkout Session", session_name)
    if session.pos_invoice:
        return session.pos_invoice
    if session.status != "Paid":
        return None

    settings = get_settings()
    profile = frappe.get_doc("POS Profile", session.pos_profile)
    company = settings.company or profile.company
    realtime_stock = (session.stock_mode or settings.stock_mode) == "Realtime"

    _ensure_open_pos_entry(profile, session.terminal_user or frappe.session.user, company)

    inv = frappe.new_doc("POS Invoice")
    inv.customer = session.customer or settings.default_customer
    inv.company = company
    inv.pos_profile = profile.name
    inv.is_pos = 1
    inv.currency = session.currency or profile.currency
    inv.selling_price_list = profile.selling_price_list
    inv.set_posting_time = 1
    inv.posting_date = today()
    inv.update_stock = 1 if realtime_stock else 0
    inv.set_warehouse = session.warehouse

    for row in session.items:
        inv.append(
            "items",
            {"item_code": row.item_code, "qty": row.qty, "rate": row.rate, "warehouse": session.warehouse},
        )
    if session.bag_qty and settings.bag_item:
        inv.append(
            "items",
            {
                "item_code": settings.bag_item,
                "qty": session.bag_qty,
                "rate": flt(session.bag_amount) / session.bag_qty,
                "warehouse": session.warehouse,
            },
        )

    # Book the whole amount to the HitPay mode of payment (its account is the clearing account).
    inv.append(
        "payments",
        {"mode_of_payment": settings.hitpay_mode_of_payment, "amount": flt(session.grand_total)},
    )

    inv.flags.ignore_permissions = True
    inv.insert(ignore_permissions=True)
    inv.submit()

    session.db_set("pos_invoice", inv.name, update_modified=False)
    session.db_set("stock_posted", 1 if realtime_stock else 0, update_modified=False)
    frappe.db.commit()
    return inv.name


def _ensure_open_pos_entry(profile, user: str, company: str) -> str:
    """Find (or open) a POS Opening Entry for this profile+user so POS Invoices belong to a session and
    can later be consolidated by a POS Closing Entry."""
    existing = frappe.db.get_value(
        "POS Opening Entry",
        {"pos_profile": profile.name, "user": user, "status": "Open", "docstatus": 1},
        "name",
    )
    if existing:
        return existing

    entry = frappe.new_doc("POS Opening Entry")
    entry.pos_profile = profile.name
    entry.user = user
    entry.company = company
    entry.period_start_date = now_datetime()
    entry.posting_date = today()
    for pay in profile.payments:
        entry.append("balance_details", {"mode_of_payment": pay.mode_of_payment, "opening_amount": 0})
    entry.flags.ignore_permissions = True
    entry.insert(ignore_permissions=True)
    entry.submit()
    return entry.name


# --------------------------------------------------------------------------------------------------
# Day sessions + the End-of-day consolidated stock posting
# --------------------------------------------------------------------------------------------------
def open_day(pos_profile: str | None = None) -> dict:
    """Open the POS session for the shop (idempotent). Returns the POS Opening Entry name."""
    profile = frappe.get_doc("POS Profile", pos_profile) if pos_profile else resolve_pos_profile()
    settings = get_settings()
    company = settings.company or profile.company
    opening = _ensure_open_pos_entry(profile, frappe.session.user, company)
    return {"pos_profile": profile.name, "pos_opening_entry": opening}


def close_day(pos_profile: str | None = None) -> dict:
    """Close the shop's POS session: post the End-of-day consolidated stock movement (if in that mode),
    then create the native POS Closing Entry. Returns comparison metrics."""
    profile = frappe.get_doc("POS Profile", pos_profile) if pos_profile else resolve_pos_profile()
    settings = get_settings()
    started = now_datetime()

    stock_entry, stock_lines = None, 0
    if (settings.stock_mode) == "End of Day":
        stock_entry, stock_lines = _post_consolidated_stock(profile)

    closing = _make_closing_entry(profile)

    order_count = frappe.db.count(
        "Checkout Session", {"pos_profile": profile.name, "status": "Paid", "pos_invoice": ["is", "set"]}
    )
    return {
        "pos_profile": profile.name,
        "pos_closing_entry": closing,
        "consolidated_stock_entry": stock_entry,
        "stock_lines_posted": stock_lines,
        "order_count": order_count,
        "close_ms": int((now_datetime() - started).total_seconds() * 1000),
    }


def _post_consolidated_stock(profile) -> tuple[str | None, int]:
    """One Stock Entry (Material Issue) for the aggregate sold qty of the day's Paid-but-unposted
    sessions on this profile. Marks those sessions stock_posted so a second close is a no-op."""
    settings = get_settings()
    sessions = frappe.get_all(
        "Checkout Session",
        filters={"pos_profile": profile.name, "status": "Paid", "stock_posted": 0},
        pluck="name",
    )
    if not sessions:
        return None, 0

    qty_by_item: dict[str, float] = {}
    for name in sessions:
        doc = frappe.get_doc("Checkout Session", name)
        for row in doc.items:
            qty_by_item[row.item_code] = qty_by_item.get(row.item_code, 0.0) + flt(row.qty)
        if doc.bag_qty and settings.bag_item:
            qty_by_item[settings.bag_item] = qty_by_item.get(settings.bag_item, 0.0) + flt(doc.bag_qty)

    if not qty_by_item:
        return None, 0

    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Issue"
    se.company = settings.company or profile.company
    se.posting_date = today()
    se.set_posting_time = 1
    for item_code, qty in qty_by_item.items():
        se.append("items", {"item_code": item_code, "qty": qty, "s_warehouse": profile.warehouse})
    se.flags.ignore_permissions = True
    se.insert(ignore_permissions=True)
    se.submit()

    for name in sessions:
        frappe.db.set_value("Checkout Session", name, "stock_posted", 1, update_modified=False)
    frappe.db.commit()
    return se.name, len(qty_by_item)


def _make_closing_entry(profile) -> str | None:
    """Consolidate the open POS session into a POS Closing Entry. Uses ERPNext's helper to prefill from
    the opening entry; flagged for on-bench verification of the exact signature in v16."""
    opening = frappe.db.get_value(
        "POS Opening Entry",
        {"pos_profile": profile.name, "user": frappe.session.user, "status": "Open", "docstatus": 1},
        "name",
    )
    if not opening:
        return None
    try:
        from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
            make_closing_entry_from_opening,
        )

        closing = make_closing_entry_from_opening(frappe.get_doc("POS Opening Entry", opening))
        closing.period_end_date = now_datetime()
        closing.flags.ignore_permissions = True
        closing.insert(ignore_permissions=True)
        closing.submit()
        return closing.name
    except Exception:
        # Don't fail the whole close (esp. the stock posting) if the consolidation helper differs on
        # this bench — surface it for the operator to complete/verify.
        frappe.log_error(frappe.get_traceback(), "POS Closing Entry consolidation failed")
        return None
