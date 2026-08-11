# Copyright (c) 2026 and contributors
# For license information, please see license.txt

"""Post-install bootstrap for the Self Checkout app (wired via ``after_install`` in hooks.py).

Idempotent — safe to re-run on reinstall/migrate. Runs as Administrator during install, so
``ignore_permissions=True`` is appropriate here (unlike ``api.py``, which respects the caller's role).

Provisions: the **Shop Cashier** role (API-only) + the resource reads the till needs, a **"HitPay"
Mode of Payment**, a generic **"Walk-in Customer"**, and default **Checkout Settings**. It intentionally
does NOT create shops — POS Profiles (with their warehouse, price list, cashier users and the HitPay
payment method) are created per deployment.
"""

from __future__ import annotations

import frappe

CASHIER_ROLE = "Shop Cashier"
# The till reads these to populate its catalogue; the role is otherwise scoped to Checkout DocTypes.
RESOURCE_DOCTYPES = ("Item", "Item Price", "Item Barcode", "Bin", "Warehouse", "Customer")
HITPAY_MODE_OF_PAYMENT = "HitPay"
WALK_IN_CUSTOMER = "Walk-in Customer"


def after_install() -> None:
    """Provision the app's bootstrap objects.

    The **cashier role is critical** — nothing in the app works without it, so its failure is left to
    propagate and abort ``install-app`` (better a clean rollback than a silently useless install). The
    remaining steps are isolated: a failure in one is logged and skipped rather than aborting. Every step
    is idempotent, so once the cause is fixed this can be re-run with ``bench execute pos.install.after_install``.
    """
    # Critical: let this raise. An abort here rolls the app back cleanly instead of installing it broken.
    _ensure_cashier_role()
    frappe.db.commit()

    steps = (
        ("resource read grants", _grant_resource_read),
        ("HitPay mode of payment", _ensure_hitpay_mode_of_payment),
        ("walk-in customer", _ensure_walk_in_customer),
        ("checkout settings", _seed_settings),
    )
    failed = []
    for label, step in steps:
        try:
            step()
        except Exception:
            failed.append(label)
            # Roll back the partial writes of this step so the next one starts clean.
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"Self Checkout install: {label} failed")
        else:
            frappe.db.commit()
    if failed:
        frappe.log_error(
            "Steps skipped: " + ", ".join(failed) + ". Fix the cause and re-run "
            "`bench execute pos.install.after_install`.",
            "Self Checkout install incomplete",
        )


def _ensure_cashier_role() -> None:
    if not frappe.db.exists("Role", CASHIER_ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": CASHIER_ROLE, "desk_access": 0}).insert(
            ignore_permissions=True
        )


def _grant_resource_read() -> None:
    """Give the cashier role read on the catalogue source DocTypes (else /api/resource calls 403)."""
    for doctype in RESOURCE_DOCTYPES:
        if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": CASHIER_ROLE}):
            frappe.get_doc(
                {
                    "doctype": "Custom DocPerm",
                    "parent": doctype,
                    "parenttype": "DocType",
                    "parentfield": "permissions",
                    "role": CASHIER_ROLE,
                    "read": 1,
                }
            ).insert(ignore_permissions=True)


def _ensure_hitpay_mode_of_payment() -> None:
    """A Mode of Payment recorded on POS Invoices for HitPay-settled sales. Its per-company account
    (the clearing account) is linked by an administrator in the Mode of Payment form."""
    if not frappe.db.exists("Mode of Payment", HITPAY_MODE_OF_PAYMENT):
        frappe.get_doc(
            {"doctype": "Mode of Payment", "mode_of_payment": HITPAY_MODE_OF_PAYMENT, "type": "Bank"}
        ).insert(ignore_permissions=True)


def _ensure_walk_in_customer() -> None:
    if frappe.db.exists("Customer", WALK_IN_CUSTOMER):
        return
    group = frappe.db.get_default("customer_group") or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    territory = frappe.db.get_default("territory") or frappe.db.get_value("Territory", {"is_group": 0}, "name")
    # Non-fatal if this raises: the after_install harness logs + skips it, and an admin can point the
    # default customer in Checkout Settings later.
    frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": WALK_IN_CUSTOMER,
            "customer_type": "Individual",
            "customer_group": group,
            "territory": territory,
        }
    ).insert(ignore_permissions=True)


def _seed_settings() -> None:
    """Default Checkout Settings without clobbering anything an admin already set."""
    settings = frappe.get_single("Checkout Settings")
    dirty = False
    defaults = {
        "hitpay_env": "sandbox",
        "stock_mode": "Realtime",
        "default_currency": "SGD",
    }
    for field, value in defaults.items():
        if not settings.get(field):
            settings.set(field, value)
            dirty = True
    if not settings.hitpay_mode_of_payment and frappe.db.exists("Mode of Payment", HITPAY_MODE_OF_PAYMENT):
        settings.hitpay_mode_of_payment = HITPAY_MODE_OF_PAYMENT
        dirty = True
    if not settings.default_customer and frappe.db.exists("Customer", WALK_IN_CUSTOMER):
        settings.default_customer = WALK_IN_CUSTOMER
        dirty = True
    if dirty:
        settings.save(ignore_permissions=True)
