# Copyright (c) 2026 and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import flt, cint


class CheckoutSession(Document):
    """A pending self-checkout cart awaiting an (asynchronous) HitPay payment.

    Lifecycle: created ``Pending`` by ``api.create_session``, which also builds a **draft** POS Invoice
    (the authoritative pricing — rules + tax) and links it via ``pos_invoice``. A HitPay request is
    attached by ``api.start_payment``; the ``hitpay_webhook`` flips it to ``Paid``/``Failed``. On ``Paid``
    the draft is submitted (``stock.finalize_paid_session``); on failure it is deleted
    (``stock.discard_draft_invoice``) so no unpaid invoice lingers.

    ``validate`` re-derives *provisional* money fields from the line items + bag; the authoritative
    ``grand_total`` is overwritten from the priced draft invoice right after insert.
    """

    def validate(self):
        self._recompute_totals()

    def _recompute_totals(self):
        net = 0.0
        for row in self.items or []:
            row.amount = flt(row.qty) * flt(row.rate)
            net += flt(row.amount)
        self.net_total = net
        self.bag_amount = flt(self.bag_amount)
        self.bag_qty = cint(self.bag_qty)
        self.grand_total = flt(self.net_total) + flt(self.bag_amount)
