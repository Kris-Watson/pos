# Copyright (c) 2026 and contributors
# For license information, please see license.txt

"""Realtime (Socket.IO) push of the payment result to the originating cashier.

Frappe realtime is fire-and-forget — there is no redelivery if the socket is momentarily down. So this
is only a latency optimisation: the till also reconciles via ``api.session_status`` on reconnect / a
slow fallback timer. We target the specific ``terminal_user`` so only that cashier's screen wakes.
"""

import frappe

CHECKOUT_EVENT = "checkout_update"


def publish_session_result(session_name: str) -> None:
    """Push the current session status to its cashier (called from the webhook processor)."""
    doc = frappe.db.get_value(
        "Checkout Session",
        session_name,
        ["name", "status", "hitpay_payment_id", "pos_invoice", "terminal_user", "grand_total", "modified"],
        as_dict=True,
    )
    if not doc or not doc.terminal_user:
        return
    frappe.publish_realtime(
        CHECKOUT_EVENT,
        {
            "session": doc.name,
            "status": doc.status,
            "hitpay_payment_id": doc.hitpay_payment_id,
            "pos_invoice": doc.pos_invoice,
            "grand_total": doc.grand_total,
            "modified": str(doc.modified) if doc.modified else None,
        },
        user=doc.terminal_user,
        after_commit=True,
    )


def publish_sale_change(doc, method=None) -> None:
    """POS Invoice ``on_submit`` hook: belt-and-suspenders push so the cashier's screen advances even if
    the direct webhook push was missed. No-op for POS Invoices not created by the self-checkout flow."""
    session = frappe.db.get_value(
        "Checkout Session",
        {"pos_invoice": doc.name},
        ["name", "status", "terminal_user", "grand_total"],
        as_dict=True,
    )
    if not session or not session.terminal_user:
        return
    frappe.publish_realtime(
        CHECKOUT_EVENT,
        {"session": session.name, "status": session.status, "pos_invoice": doc.name, "grand_total": session.grand_total},
        user=session.terminal_user,
        after_commit=True,
    )
