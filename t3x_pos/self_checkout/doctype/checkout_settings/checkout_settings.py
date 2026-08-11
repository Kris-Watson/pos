# Copyright (c) 2026 and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CheckoutSettings(Document):
    """Single DocType: HitPay credentials + environment, the sales/accounting defaults used when a paid
    session is turned into a POS Invoice, and the stock-update mode toggle (Realtime vs End of Day).

    Secrets (`hitpay_api_key`, `hitpay_salt`) are Password fields — encrypted at rest and never returned
    to the device; the server reads them via ``get_password`` when calling HitPay / verifying webhooks.
    """

    pass
