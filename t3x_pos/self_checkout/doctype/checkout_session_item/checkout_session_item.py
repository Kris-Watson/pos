# Copyright (c) 2026 and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CheckoutSessionItem(Document):
    """One line of a Checkout Session cart: item, qty and the server-recomputed rate/amount."""

    pass
