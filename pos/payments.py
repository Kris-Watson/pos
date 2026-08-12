# Copyright (c) 2026 and contributors
# For license information, please see license.txt

"""HitPay proxy: create payment requests server-side and receive the result webhook.

The Business API key + webhook salt live only in the encrypted Checkout Settings — never on the
device. The webhook is the single guest endpoint: it verifies the HMAC signature, then hands off to an
**enqueued, idempotent** processor (runs as a trusted system job) so the HTTP handler returns 200 fast
and HitPay's retries are safe. Confirmation flows to the till via realtime; polling is only a fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import flt, get_url, now_datetime

from pos.utils import SERVICE_USER, cas_session_status, get_settings

# QR-rendered methods (device shows the QR); everything else is a hosted redirect, except the
# card-present reader which just waits for a tap on the terminal.
QR_METHODS = {"paynow_online", "grabpay_direct", "shopee_pay", "wechat", "alipay"}
TERMINAL_METHOD = "wifi_card_reader"

WEBHOOK_PATH = "/api/method/pos.payments.hitpay_webhook"


def _base_url(env: str) -> str:
    return "https://api.sandbox.hit-pay.com" if (env or "sandbox") != "prod" else "https://api.hit-pay.com"


# --------------------------------------------------------------------------------------------------
# Outbound: create a payment request for a session
# --------------------------------------------------------------------------------------------------
def create_payment_request(session_name: str, method: str, terminal_id: str = "") -> dict:
    """Create the HitPay payment request for a Pending session and return what the screen should show.

    Called by ``api.start_payment`` (whitelisted). Stores the returned request id on the session so the
    webhook can correlate the result. ``terminal_id`` is the device's HitPay Wi-Fi card-reader id (set per
    kiosk on the app); for the card-present method the charge is routed to that reader.
    """
    doc = frappe.get_doc("Checkout Session", session_name)
    doc.check_permission("write")
    if doc.status != "Pending":
        frappe.throw(_("This checkout is no longer pending (status: {0}).").format(doc.status))

    # Defense in depth: never ask HitPay to charge zero/negative. create_session already refuses a 0
    # total, so reaching here with one means the session was tampered with or a pricing edit zeroed it —
    # fail instead of creating a junk 0.00 charge that can never be paid.
    amount = flt(doc.grand_total, 2)
    if amount <= 0:
        frappe.throw(_("This checkout has no payable amount (total is {0}).").format(amount))

    settings = get_settings()
    api_key = settings.get_password("hitpay_api_key", raise_exception=False)
    if not api_key:
        frappe.throw(_("HitPay is not configured (missing Business API key in Checkout Settings)."))

    body = {
        "amount": f"{amount:.2f}",
        "currency": (doc.currency or "SGD"),
        "payment_methods": [method],
        "reference_number": doc.name,
        "webhook": get_url(WEBHOOK_PATH),
        "redirect_url": get_url("/checkout-complete"),
        "purpose": f"Self-checkout {doc.name}",
    }
    if method in QR_METHODS:
        body["generate_qr"] = True
    if method == TERMINAL_METHOD:
        reader = (terminal_id or "").strip()
        if not reader:
            frappe.throw(_("No card terminal is configured on this till. Set the Card Terminal ID in the app before taking a card-present payment."))
        body["wifi_terminal_id"] = reader

    resp = _hitpay_post(settings, api_key, "/v1/payment-requests", body)

    frappe.db.set_value(
        "Checkout Session",
        doc.name,
        {"hitpay_request_id": resp.get("id"), "payment_method": method},
        update_modified=False,
    )
    frappe.db.commit()

    # HitPay returns qr_code_data as an object {qr_code, qr_code_expiry} (qr_code = raw payload in prod, a
    # URL in sandbox); older responses sent a bare string. Hand the device a plain payload string to render.
    qr = resp.get("qr_code_data")
    qr_payload = qr.get("qr_code") if isinstance(qr, dict) else qr
    qr_expiry = qr.get("qr_code_expiry") if isinstance(qr, dict) else None

    return {
        "session": doc.name,
        "status": "pending",
        "hitpay_request_id": resp.get("id"),
        "qr_code_data": qr_payload,
        "qr_code_expiry": qr_expiry,
        "url": resp.get("url"),
        "terminal_wait": method == TERMINAL_METHOD,
    }


def _hitpay_post(settings, api_key: str, path: str, body: dict) -> dict:
    """POST JSON to HitPay; surface a non-2xx with the server message as a clean error."""
    import requests

    url = _base_url(settings.hitpay_env) + path
    try:
        r = requests.post(
            url,
            json=body,
            headers={"X-BUSINESS-API-KEY": api_key, "Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as e:
        frappe.throw(_("Could not reach HitPay: {0}").format(str(e)))

    if not (200 <= r.status_code < 300):
        msg = _extract_error(r.text) or f"HTTP {r.status_code}"
        frappe.log_error(f"HitPay {path} -> {r.status_code}\n{r.text}", "HitPay request failed")
        frappe.throw(_("HitPay rejected the payment request: {0}").format(msg))
    return r.json() if r.text else {}


def _extract_error(text: str) -> str | None:
    try:
        o = json.loads(text)
    except Exception:
        return None
    if o.get("message"):
        return o["message"]
    errors = o.get("errors") or {}
    for key in errors:
        val = errors[key]
        if isinstance(val, list) and val:
            return val[0]
    return None


# --------------------------------------------------------------------------------------------------
# Inbound: the webhook (guest) + enqueued idempotent processor (system job)
# --------------------------------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def hitpay_webhook():
    """HitPay posts the payment result here. Verify the HMAC, then enqueue the processing and return
    200 immediately. Idempotency + all writes happen in the background job (as the service account)."""
    raw = frappe.request.get_data(as_text=True) if frappe.request else ""
    form = dict(frappe.local.form_dict or {})
    form.pop("cmd", None)
    signature = frappe.get_request_header("Hitpay-Signature")

    settings = get_settings()
    salt = settings.get_password("hitpay_salt", raise_exception=False)
    if not salt or not _verify_signature(raw, signature, form, salt):
        frappe.local.response["http_status_code"] = 400
        return {"ok": False, "error": "invalid signature"}

    payload = _parse_payload(raw, form)
    frappe.enqueue(
        "pos.payments.process_payment_event",
        queue="short",
        enqueue_after_commit=True,
        payload=payload,
    )
    return {"ok": True}


def _verify_signature(raw: str, signature: str | None, form: dict, salt: str) -> bool:
    """Support both HitPay signing schemes: v2 (JSON body + ``Hitpay-Signature`` header HMAC of the raw
    body) and v1 (form-encoded with an ``hmac`` field over the sorted ``key+value`` concatenation)."""
    # v2 — header signature over the raw JSON body.
    if signature:
        expected = hmac.new(salt.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    # v1 — form field `hmac` over sorted key+value pairs (excluding `hmac`).
    provided = form.get("hmac")
    if provided:
        data = "".join(f"{k}{form[k]}" for k in sorted(k for k in form if k != "hmac"))
        expected = hmac.new(salt.encode(), data.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(str(provided), expected)
    return False


def _parse_payload(raw: str, form: dict) -> dict:
    """Normalise the v1 (form) and v2 (JSON) webhook bodies into one dict of the fields we use."""
    body: dict = {}
    if raw:
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
    src = body or form
    payments = body.get("payments") if isinstance(body.get("payments"), list) else None
    payment_id = (
        src.get("payment_id")
        or (payments[0].get("id") if payments else None)
        or src.get("id")
    )
    amount = src.get("amount") or (payments[0].get("amount") if payments else None)
    return {
        "reference_number": src.get("reference_number"),
        "request_id": src.get("payment_request_id") or (body.get("id") if body else form.get("id")),
        "payment_id": payment_id,
        "amount": flt(amount) if amount is not None else None,
        "status": (src.get("status") or "").lower(),
        "object_type": _safe_header("Hitpay-Event-Object"),
        "event_type": _safe_header("Hitpay-Event-Type"),
        "raw": src,
    }


def _safe_header(name: str) -> str | None:
    """``frappe.get_request_header`` outside a request context raises — tolerate that (tests)."""
    try:
        return frappe.get_request_header(name)
    except Exception:
        return None


def process_payment_event(payload: dict) -> None:
    """Idempotently apply one payment result: record the event, flip the session, and on success turn it
    into a native POS Invoice + push the result to the cashier.

    The webhook has no logged-in caller, so this runs as the least-privilege **service account** (holds
    exactly the roles a sale needs, nothing more) and restores the prior user when done."""
    prior_user = frappe.session.user
    frappe.set_user(SERVICE_USER)
    try:
        _apply_payment_event(payload)
    finally:
        frappe.set_user(prior_user)


def _apply_payment_event(payload: dict) -> None:
    status = (payload.get("status") or "").lower()
    reference = payload.get("reference_number")
    event_key = payload.get("payment_id") or f"{payload.get('request_id')}:{status}"
    if not event_key:
        return

    # Idempotency: the event_key is the (unique) doc name — a replay fails to insert and we bail.
    try:
        frappe.get_doc(
            {
                "doctype": "Checkout Payment Event",
                "event_key": event_key,
                "checkout_session": reference,
                "object_type": payload.get("object_type"),
                "event_type": payload.get("event_type"),
                "status": status,
                "processed_on": now_datetime(),
                "payload": frappe.as_json(payload.get("raw") or {}),
            }
        ).insert()  # service account holds create on Checkout Payment Event (Checkout Service role)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return

    session = _find_session(reference, payload.get("request_id"))
    if not session:
        frappe.db.commit()
        return

    new_status = {"completed": "Paid", "failed": "Failed", "expired": "Failed", "canceled": "Failed"}.get(status)
    if not new_status:
        frappe.db.commit()
        return

    flipped = cas_session_status(session, "Pending", new_status)
    if flipped:
        frappe.db.set_value(
            "Checkout Session",
            session,
            {"hitpay_payment_id": payload.get("payment_id"), "paid_on": now_datetime() if new_status == "Paid" else None},
            update_modified=False,
        )

    frappe.db.commit()

    if flipped and new_status == "Paid":
        from pos import stock

        # Submit the draft invoice, reconciling its total against HitPay's gross charge (fees excluded).
        stock.finalize_paid_session(session, payload.get("amount"))
    elif flipped and new_status == "Failed":
        from pos import stock

        # No sale happened — drop the unpaid draft invoice so it doesn't linger.
        stock.discard_draft_invoice(session)

    # Push the result to the originating cashier (fire-and-forget; app also polls session_status).
    from pos import realtime

    realtime.publish_session_result(session)


def _find_session(reference: str | None, request_id: str | None) -> str | None:
    if reference and frappe.db.exists("Checkout Session", reference):
        return reference
    if request_id:
        return frappe.db.get_value("Checkout Session", {"hitpay_request_id": request_id}, "name")
    return None
