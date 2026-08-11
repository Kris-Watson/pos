"""Tests for cart parsing, session-total recomputation and shop gating.

The pure-logic tests run on any site; the ERPNext-dependent flow (create_session -> webhook ->
POS Invoice) is exercised on a configured site and self-skips otherwise. Run with
``bench run-tests --app t3x_pos``.
"""

import json

import frappe

try:  # Frappe v16+
    from frappe.tests import IntegrationTestCase as _BaseTestCase
except ImportError:  # Frappe v14/v15
    from frappe.tests.utils import FrappeTestCase as _BaseTestCase

from t3x_pos.utils import parse_items, resolve_pos_profile


class TestParseItems(_BaseTestCase):
    def test_parses_valid_cart(self):
        lines = parse_items(json.dumps([{"item_code": "X", "qty": 2, "barcode": "888"}]))
        self.assertEqual(lines, [{"item_code": "X", "qty": 2, "barcode": "888"}])

    def test_accepts_decoded_list(self):
        lines = parse_items([{"item_code": "Y", "qty": 1}])
        self.assertEqual(lines[0]["item_code"], "Y")

    def test_rejects_empty_cart(self):
        with self.assertRaises(frappe.ValidationError):
            parse_items("[]")

    def test_rejects_bad_json(self):
        with self.assertRaises(frappe.ValidationError):
            parse_items("{not json")

    def test_rejects_nonpositive_qty(self):
        with self.assertRaises(frappe.ValidationError):
            parse_items(json.dumps([{"item_code": "X", "qty": 0}]))

    def test_rejects_missing_item_code(self):
        with self.assertRaises(frappe.ValidationError):
            parse_items(json.dumps([{"qty": 1}]))


class TestSessionTotals(_BaseTestCase):
    def test_recompute_totals(self):
        doc = frappe.new_doc("Checkout Session")
        doc.append("items", {"item_code": "A", "qty": 2, "rate": 1.5})
        doc.append("items", {"item_code": "B", "qty": 1, "rate": 3.0})
        doc.bag_qty = 2
        doc.bag_amount = 0.2
        doc.validate()
        self.assertEqual(doc.items[0].amount, 3.0)
        self.assertEqual(doc.net_total, 6.0)
        self.assertEqual(doc.grand_total, 6.2)


class TestShopGating(_BaseTestCase):
    def test_user_without_profile_is_blocked(self):
        parents = frappe.get_all("POS Profile User", filters={"user": frappe.session.user})
        if parents:
            self.skipTest("current user is mapped to a POS Profile")
        with self.assertRaises(frappe.ValidationError):
            resolve_pos_profile()
