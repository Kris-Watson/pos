"""Unit tests for the HitPay webhook signature verification + payload parsing.

Pure functions — no site fixtures needed. Runs under ``bench run-tests --app t3x_pos``.
"""

import hashlib
import hmac
import json

try:  # Frappe v16+
    from frappe.tests import IntegrationTestCase as _BaseTestCase
except ImportError:  # Frappe v14/v15
    from frappe.tests.utils import FrappeTestCase as _BaseTestCase

from t3x_pos.payments import _parse_payload, _verify_signature

SALT = "test_salt_123"


class TestWebhookSignature(_BaseTestCase):
    def test_v2_header_signature_valid(self):
        raw = json.dumps({"status": "completed", "reference_number": "CHK-1"})
        sig = hmac.new(SALT.encode(), raw.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(_verify_signature(raw, sig, {}, SALT))

    def test_v2_header_signature_invalid(self):
        raw = json.dumps({"status": "completed"})
        self.assertFalse(_verify_signature(raw, "deadbeef", {}, SALT))

    def test_v1_form_hmac_valid(self):
        form = {"status": "completed", "reference_number": "CHK-1", "payment_id": "pay_1"}
        data = "".join(f"{k}{form[k]}" for k in sorted(form))
        provided = hmac.new(SALT.encode(), data.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(_verify_signature("", None, dict(form, hmac=provided), SALT))

    def test_v1_form_hmac_invalid(self):
        form = {"status": "completed", "reference_number": "CHK-1", "hmac": "nope"}
        self.assertFalse(_verify_signature("", None, form, SALT))

    def test_missing_signature_rejected(self):
        self.assertFalse(_verify_signature("{}", None, {}, SALT))

    def test_parse_payload_v2_json(self):
        raw = json.dumps(
            {"id": "req_1", "status": "COMPLETED", "reference_number": "CHK-2", "payments": [{"id": "pay_9"}]}
        )
        p = _parse_payload(raw, {})
        self.assertEqual(p["reference_number"], "CHK-2")
        self.assertEqual(p["status"], "completed")
        self.assertEqual(p["payment_id"], "pay_9")
        self.assertEqual(p["request_id"], "req_1")

    def test_parse_payload_v1_form(self):
        form = {
            "status": "failed",
            "reference_number": "CHK-3",
            "payment_id": "pay_x",
            "payment_request_id": "req_3",
        }
        p = _parse_payload("", form)
        self.assertEqual(p["reference_number"], "CHK-3")
        self.assertEqual(p["status"], "failed")
        self.assertEqual(p["payment_id"], "pay_x")
        self.assertEqual(p["request_id"], "req_3")
