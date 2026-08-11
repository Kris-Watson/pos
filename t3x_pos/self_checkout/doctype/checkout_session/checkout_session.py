# Copyright (c) 2026 and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import flt, cint


class CheckoutSession(Document):
    """A pending self-checkout cart awaiting an (asynchronous) HitPay payment.

    Lifecycle: created ``Pending`` by ``api.create_session`` (which recomputes every rate from Item
    Price — the client total is never trusted), a HitPay request is attached by ``api.start_payment``,
    and the ``hitpay_webhook`` flips it to ``Paid``/``Failed``. On ``Paid`` it is converted into a
    native POS Invoice (see ``stock.py``); abandoned or failed carts leave no invoice.

    ``validate`` re-derives the money fields from the line items + bag so totals stay internally
    consistent no matter who edits the doc (API, desk form, Data Import).
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
