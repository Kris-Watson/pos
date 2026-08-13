# Copyright (c) 2026 and contributors
# For license information, please see license.txt

"""Post-install bootstrap for the Self Checkout app (wired via ``after_install`` in hooks.py).

Idempotent — safe to re-run on reinstall/migrate. Runs as Administrator during install, so
``ignore_permissions=True`` is appropriate here (unlike ``api.py``, which respects the caller's role).

Provisions: the self-contained **Shop Cashier** role (API-only, carrying every permission the till
needs), a login-less **service account** for the webhook, a **"HitPay" Mode of Payment**, a generic
**"Walk-in Customer"**, and default **Checkout Settings**. It intentionally does NOT create shops — POS
Profiles (with their warehouse, price list, cashier users and the HitPay payment method) are created
per deployment.
"""

from __future__ import annotations

import frappe

from pos.utils import SERVICE_USER

CASHIER_ROLE = "Shop Cashier"

# The Shop Cashier role is self-contained: it carries every permission the till needs under the
# cashier's own identity — no per-user Accounts User / Sales Manager assignment. Two sets:
#   * read masters — the catalogue the till lists, plus the accounting/pricing masters ERPNext's
#     get_item_details / tax engine validate against the live user while building the draft invoice;
#   * write transactions — the POS docs the cashier creates itself: the draft POS Invoice (create only,
#     the webhook service account submits it), opening/closing the POS day, and the End-of-Day
#     consolidated Stock Entry.
CASHIER_READ_DOCTYPES = (
    "Item", "Item Price", "Item Barcode", "Item Group", "Bin", "Warehouse", "UOM",
    "Customer", "Customer Group", "Territory",
    "Company", "Currency", "Price List",
    "Account", "Cost Center", "Mode of Payment",
    "Pricing Rule", "Sales Taxes and Charges Template", "Item Tax Template",
    "POS Profile",
)
CASHIER_WRITE_PERMS = {
    # create+write for the draft (the webhook service account also submits it on payment). submit is needed
    # too because closing-day consolidation re-saves each already-submitted POS Invoice to stamp its
    # consolidated Sales Invoice link (update_pos_invoices → doc.save on a docstatus-1 doc, which triggers a
    # submit permission check) — and that runs under the cashier's identity.
    "POS Invoice": ("read", "write", "create", "submit"),
    "POS Opening Entry": ("read", "write", "create", "submit"),
    "POS Closing Entry": ("read", "write", "create", "submit"),
    "Stock Entry": ("read", "write", "create", "submit"),  # End-of-Day consolidated Material Issue
    # Closing the POS day consolidates the day's POS Invoices into a Sales Invoice via a POS Invoice
    # Merge Log — submitting the POS Closing Entry (on_submit → consolidate_pos_invoices) creates+submits
    # both under the cashier's own identity, so the self-contained role needs them too.
    "POS Invoice Merge Log": ("read", "write", "create", "submit"),
    "Sales Invoice": ("read", "write", "create", "submit"),
}
HITPAY_MODE_OF_PAYMENT = "HitPay"
WALK_IN_CUSTOMER = "Walk-in Customer"

# Backend identity for the caller-less webhook (see payments.process_payment_event). Like Shop Cashier,
# the Checkout Service role is **self-contained**: assigning a user that one role gives it everything the
# webhook path needs — no Accounts User / Sales Manager / Stock User / Shop Cashier assignment. Its write
# on Checkout Session + create on Checkout Payment Event come from the doctype JSONs; the grants below add
# the rest:
#   * read masters — submitting the draft POS Invoice re-runs the same validation the cashier's draft did,
#     which READS the masters (Customer, Item, Account, Pricing Rule, …). The cashier grants make those
#     doctypes Custom-DocPerm-managed site-wide, so standard reads no longer apply — the role must be in
#     each doctype's perm list explicitly, or submit raises PermissionError (first on Customer).
#   * write POS Invoice — submit the cashier's draft on payment success, or delete it on failure/expiry.
SERVICE_ROLE = "Checkout Service"
SERVICE_READ_DOCTYPES = CASHIER_READ_DOCTYPES
SERVICE_WRITE_PERMS = {
    # Submit the draft the cashier created (success), or delete it (payment failed/expired). No create —
    # the till builds the draft; the service only finalises it.
    "POS Invoice": ("read", "write", "submit", "delete"),
    "POS Opening Entry": ("read",),  # submit-time validation reads the open POS session
}


def after_install() -> None:
    """Provision the app's bootstrap objects.

    The **cashier role and the service account are critical** — nothing in the app works without them, so
    their failure is left to propagate and abort ``install-app`` (better a clean rollback than a silently
    useless install). The remaining steps are isolated: a failure in one is logged and skipped rather than
    aborting. Every step is idempotent, so once the cause is fixed this can be re-run with
    ``bench execute pos.install.after_install``.

    Note (native permission model): the **Shop Cashier** role is self-contained — granting a user that
    one role gives the till everything it needs under the user's own identity (build the draft POS
    Invoice, open/close the POS day, End-of-Day stock). No Accounts User / Sales Manager assignment,
    no separate manager to open the day. See README.
    """
    # Critical: let these raise. An abort here rolls the app back cleanly instead of installing it broken.
    _ensure_cashier_role()
    _ensure_service_account()
    frappe.db.commit()

    steps = (
        ("cashier role permissions", _grant_cashier_permissions),
        ("service role permissions", _grant_service_permissions),
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


def _ensure_service_account() -> None:
    """Create the webhook backend identity: a **login-less System User** holding only the self-contained
    Checkout Service role. Used solely via ``frappe.set_user`` in the caller-less webhook path — never on
    a user endpoint."""
    if not frappe.db.exists("Role", SERVICE_ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": SERVICE_ROLE, "desk_access": 0}).insert(
            ignore_permissions=True
        )

    if not frappe.db.exists("User", SERVICE_USER):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": SERVICE_USER,
                "first_name": "Checkout Service",
                "user_type": "System User",
                # Random password + no API key => the account cannot be logged into interactively.
                "new_password": frappe.generate_hash(length=40),
                "send_welcome_email": 0,
            }
        )
        user.flags.no_welcome_mail = True
        user.insert(ignore_permissions=True)

    # Assign ONLY the Checkout Service role (idempotent). It is self-contained — the grants in
    # _grant_service_permissions give it every master read + POS Invoice write the webhook path needs, so
    # no Accounts User / Sales Manager / Stock User / Shop Cashier assignment is required.
    user = frappe.get_doc("User", SERVICE_USER)
    have = {r.role for r in user.roles}
    if SERVICE_ROLE not in have:
        user.append("roles", {"role": SERVICE_ROLE})
        user.save(ignore_permissions=True)


def _grant_cashier_permissions() -> None:
    """Grant the Shop Cashier role every permission the till needs under its own identity: read on the
    catalogue + pricing masters, and create/submit on the POS transaction docs it writes. Makes the role
    self-contained — no per-user Accounts User / Sales Manager assignment. Idempotent."""
    grants = [(dt, ("read",)) for dt in CASHIER_READ_DOCTYPES]
    grants += list(CASHIER_WRITE_PERMS.items())
    _grant_role_perms(CASHIER_ROLE, grants, "cashier")


def _grant_service_permissions() -> None:
    """Grant the Checkout Service role every permission the webhook path needs under its own identity:
    read on the same catalogue + pricing masters the cashier's draft reads (submitting re-runs that
    validation), plus write/submit/delete on the POS Invoice it finalises. Makes the role self-contained —
    no Accounts User / Sales Manager / Stock User / Shop Cashier assignment. Idempotent (see
    _grant_cashier_permissions for the per-doctype isolation rationale)."""
    grants = [(dt, ("read",)) for dt in SERVICE_READ_DOCTYPES]
    grants += list(SERVICE_WRITE_PERMS.items())
    _grant_role_perms(SERVICE_ROLE, grants, "service")


def _grant_role_perms(role: str, grants: list[tuple[str, tuple[str, ...]]], label: str) -> None:
    """Apply ``grants`` (``[(doctype, rights), …]``) to ``role``, each doctype **independently** (its own
    commit): a failure on one (e.g. a doctype whose name differs on this bench) is logged and skipped, so
    it can never roll back the grants that did succeed. Re-run ``bench execute pos.install.after_install``
    after fixing any skipped one."""
    skipped = []
    for doctype, rights in grants:
        try:
            _grant_role_perm(role, doctype, rights)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            skipped.append(doctype)
            frappe.log_error(frappe.get_traceback(), f"Self Checkout: {label} perm grant failed for {doctype}")
    if skipped:
        frappe.log_error(
            f"{label.capitalize()} permission grants skipped for: " + ", ".join(skipped) + ". The rest are "
            "committed; fix these and re-run `bench execute pos.install.after_install`.",
            "Self Checkout install incomplete",
        )


def _grant_role_perm(role: str, doctype: str, rights: tuple[str, ...]) -> None:
    """Additively grant ``role`` the given ``rights`` on ``doctype`` at permlevel 0.

    Uses Frappe's permission API rather than a raw ``Custom DocPerm`` insert. ``add_permission`` copies
    the doctype's *standard* perms into Custom DocPerm the first time (preserving every existing role)
    before adding ours — a raw insert would instead replace the standard perms wholesale, silently
    stripping every other role's access to that doctype site-wide. Idempotent (``add_permission`` is a
    no-op once the row exists; the property updates re-assert the flags)."""
    if not frappe.db.exists("DocType", doctype):
        return
    from frappe.permissions import add_permission, update_permission_property

    add_permission(doctype, role, 0)
    for right in rights:
        update_permission_property(doctype, role, 0, right, "1")


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
