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
toggle.

Permission model (native, no silent bypass): the cashier does all of this under its own identity — the
self-contained **Shop Cashier** role carries the perms for each step. The cashier builds the **draft**
invoice (``build_draft_invoice``) and opens/closes the POS day (``open_day``/``close_day``, incl. the EOD
stock arm). The only elevation is the **submit** on the caller-less webhook, which runs as the service
account (``finalize_paid_session``). The only retained ``ignore_permissions`` is the internal delete of an
unpaid draft (the cashier role has create but not delete on POS Invoice).

NOTE (verify on the v16 bench): POS Invoice submission may require an open POS Opening Entry, and the
exact POS Closing Entry helper name. Both are handled defensively below and flagged for confirmation.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, today

from pos.utils import get_settings, resolve_pos_profile


def _apply_full_payment(inv, mode: str, total: float) -> None:
    """Make the invoice fully paid by HitPay as a single payment row.

    POS Invoice.set_missing_values() already populates ``payments`` from the POS Profile and assigns the
    amount due to the profile's **default** mode (HitPay). We must therefore NOT append our own row — doing
    so left two HitPay rows, doubling ``paid_amount`` and producing a phantom ``change_amount``. Instead we
    put the full ``total`` on the existing HitPay row and zero every other mode, so exactly one line is paid
    regardless of how many modes the profile lists. Only appends a row if the profile seeded none.
    """
    target = None
    for p in inv.get("payments") or []:
        if target is None and p.mode_of_payment == mode:
            target = p
            p.amount = total
        else:
            p.amount = 0
    if target is None:
        inv.append("payments", {"mode_of_payment": mode, "amount": total})


def build_draft_invoice(session) -> object:
    """Build + insert an **unsubmitted** POS Invoice (docstatus 0) for a Pending session.

    This is the pricing source of truth: ERPNext prices the cart here — applying the selling price list,
    any **Pricing Rules/discounts**, item tax templates and rounding — so the draft's ``rounded_total`` is
    the exact amount payable that the HitPay request is built from. Lines are appended with item_code+qty
    only (no rate) so ``get_item_details`` derives the effective rate. Returns the inserted draft doc.

    Nothing hits the GL or stock ledger yet — that happens only when ``finalize_paid_session`` submits it.

    Runs as the **caller** (the cashier) with native permission checks — no elevation, no
    ``ignore_permissions``. The self-contained **Shop Cashier** role grants create on POS Invoice plus the
    accounting reads ERPNext's pricing validates against the live user. The POS day must be open first
    (``open_day``, which the cashier can also call), so if it isn't we fail cleanly **before** any payment.
    """
    settings = get_settings()
    profile = frappe.get_doc("POS Profile", session.pos_profile)
    company = settings.company or profile.company
    realtime_stock = (session.stock_mode or settings.stock_mode) == "Realtime"

    # Precondition: the POS day is open for this shop. Checked by pos_profile (user-agnostic) rather than
    # by the current cashier, so any operator can sell into a day another opened.
    if not frappe.db.get_value(
        "POS Opening Entry", {"pos_profile": profile.name, "status": "Open", "docstatus": 1}, "name"
    ):
        frappe.throw(_("The POS day isn't open for this shop yet — open the day before checking out."))

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
    inv.remarks = f"Self-checkout {session.name}"

    for row in session.items:
        inv.append("items", {"item_code": row.item_code, "qty": row.qty, "warehouse": session.warehouse})
    if session.bag_qty and settings.bag_item:
        inv.append("items", {"item_code": settings.bag_item, "qty": session.bag_qty, "warehouse": session.warehouse})

    # Price it (pricing rules + taxes + rounding), then make it fully paid by HitPay so the draft is valid
    # and not "partially paid". set_missing_values already seeded the payment from the profile's default
    # mode; we sync the amount onto that single row (see _apply_full_payment). Re-synced again at submit.
    inv.set_missing_values()
    inv.run_method("calculate_taxes_and_totals")
    total = flt(inv.rounded_total) or flt(inv.grand_total)
    _apply_full_payment(inv, settings.hitpay_mode_of_payment, total)

    inv.insert()  # native perms (cashier: Shop Cashier + Accounts User); docstatus 0 — no GL/stock yet
    return inv


def finalize_paid_session(session_name: str, charged_amount: float | None = None) -> str | None:
    """Submit the session's draft POS Invoice once payment is confirmed. Idempotent.

    Reconciles the invoice total against what HitPay actually charged (gross, fees excluded): if they
    differ by more than a cent we **still submit** (the money was taken — the sale must be recorded) but
    log the mismatch for review. Returns the submitted POS Invoice name.

    Runs on the caller-less webhook path as the **service account** (see payments.process_payment_event),
    whose roles cover submitting the invoice and stock natively — no ``ignore_permissions``.
    """
    session = frappe.get_doc("Checkout Session", session_name)
    if session.status != "Paid":
        return None
    if not session.pos_invoice:
        # No draft to submit (shouldn't happen) — build one now as a fallback so the sale is still booked.
        inv = build_draft_invoice(session)
        session.db_set("pos_invoice", inv.name, update_modified=False)
    else:
        inv = frappe.get_doc("POS Invoice", session.pos_invoice)

    if inv.docstatus == 1:
        return inv.name  # already submitted — replay/idempotent
    if inv.docstatus == 2:
        return None  # cancelled

    settings = get_settings()
    profile = frappe.get_doc("POS Profile", session.pos_profile)
    company = settings.company or profile.company
    realtime_stock = (session.stock_mode or settings.stock_mode) == "Realtime"

    # Safety net: the money is already taken, so make sure a POS session is open even if open_day was
    # missed (the service account holds Sales Manager for this). Keyed to the session's operator.
    _ensure_open_pos_entry(profile, session.terminal_user or frappe.session.user, company)

    # Re-sync the payment to the invoice's current total so submit is never rejected as a partial payment.
    inv.run_method("calculate_taxes_and_totals")
    total = flt(inv.rounded_total) or flt(inv.grand_total)
    _apply_full_payment(inv, settings.hitpay_mode_of_payment, total)

    if charged_amount is not None and abs(total - flt(charged_amount)) > 0.01:
        frappe.log_error(
            f"Session {session_name}: invoice total {total} != HitPay charged {flt(charged_amount)} "
            f"(fees excluded). Submitting anyway — verify pricing/tax config.",
            "Self Checkout amount mismatch",
        )

    inv.save()
    inv.submit()

    session.db_set("stock_posted", 1 if realtime_stock else 0, update_modified=False)
    frappe.db.commit()
    return inv.name


def discard_draft_invoice(session_name: str) -> None:
    """Delete the session's draft POS Invoice on a failed/expired/canceled payment — no sale happened, so
    the unpaid draft shouldn't linger. Only touches drafts (docstatus 0); a submitted invoice is left alone."""
    session = frappe.get_doc("Checkout Session", session_name)
    if not session.pos_invoice:
        return
    inv = frappe.get_doc("POS Invoice", session.pos_invoice)
    if inv.docstatus == 0:
        # Accounts User (the service account's invoice role) can create/submit but not delete a POS Invoice;
        # this is an internal cleanup of an unpaid draft, so bypass perms for just this delete.
        frappe.delete_doc("POS Invoice", inv.name, ignore_permissions=True, force=True)
        session.db_set("pos_invoice", None, update_modified=False)
        frappe.db.commit()


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
    entry.insert()  # native: open_day = the cashier (Shop Cashier); finalize safety-net = the service account
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
    se.insert()  # native: close_day runs as the cashier (Shop Cashier holds Stock Entry create+submit)
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
        closing.insert()  # native: close_day runs as the cashier (Shop Cashier holds POS Closing Entry perms)
        closing.submit()
        return closing.name
    except Exception:
        # Don't fail the whole close (esp. the stock posting) if the consolidation helper differs on
        # this bench — surface it for the operator to complete/verify.
        frappe.log_error(frappe.get_traceback(), "POS Closing Entry consolidation failed")
        return None
