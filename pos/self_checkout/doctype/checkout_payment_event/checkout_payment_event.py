# Copyright (c) 2026 and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CheckoutPaymentEvent(Document):
    """Audit + idempotency record for one inbound HitPay webhook.

    ``event_key`` (the HitPay payment/charge/event id) is the doc name and is unique, so a replayed
    webhook simply fails to insert and the handler treats it as already-processed. Stores the raw
    payload for reconciliation/debugging.
    """

    pass
