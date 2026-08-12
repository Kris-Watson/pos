# Copyright (c) 2026 and contributors
# For license information, please see license.txt

"""Client-facing whitelisted endpoints for the self-checkout till.

Surface: login context + shop gating (``get_app_context``), catalogue delta-sync
(``catalog_version`` / ``catalog_snapshot`` / ``catalog_changed_since``), checkout
(``create_session`` / ``start_payment`` / ``session_status``) and the day sessions
(``open_day`` / ``close_day``). HitPay HTTP lives in ``payments.py``; invoice/stock in ``stock.py``.
"""

from __future__ import annotations

import base64

import frappe
from frappe import _
from frappe.utils import flt, get_datetime

from pos.utils import (
    effective_rates,
    get_settings,
    item_price,
    parse_items,
    resolve_pos_profile,
    with_deadlock_retry,
)

# HitPay methods the till may offer. The device renders these; the terminal ("wifi_card_reader")
# vs online card choice is a device/Settings concern. Kept here so the context call is self-contained.
DEFAULT_HITPAY_METHODS = ["paynow_online", "grabpay_direct", "shopee_pay", "card"]

# Hard cap so one catalogue call can't ask for an unbounded scan.
_CATALOG_CAP = 5000


# --------------------------------------------------------------------------------------------------
# Login context / shop gating
# --------------------------------------------------------------------------------------------------
@frappe.whitelist()
def _pos_tax_rate(profile) -> float:
    """Effective percentage tax rate (e.g. GST) the POS Profile applies, via its Sales Taxes and Charges
    Template. Sums the ``On Net Total`` percentage rows — a simple single-GST shop has one. Returns 0.0 when
    the profile applies no template. Used only for a client-side pre-charge display; the authoritative tax is
    still what ERPNext computes on the draft invoice at ``create_session``.
    """
    template = getattr(profile, "taxes_and_charges", None)
    if not template:
        return 0.0
    rows = frappe.get_all(
        "Sales Taxes and Charges",
        filters={"parent": template, "parenttype": "Sales Taxes and Charges Template",
                 "charge_type": "On Net Total"},
        fields=["rate"],
    )
    return flt(sum(flt(r.rate) for r in rows))


def get_app_context() -> dict:
    """Resolve the logged-in cashier to their shop + config in one round-trip.

    Throws (via ``resolve_pos_profile``) if the user has no enabled POS Profile — that is the app's
    shop gating: a user who isn't assigned to a shop cannot use the till.
    """
    profile = resolve_pos_profile()
    settings = get_settings()

    bag_price = 0.0
    if settings.bag_item and profile.selling_price_list:
        try:
            bag_price = item_price(settings.bag_item, profile.selling_price_list, warehouse=profile.warehouse)
        except Exception:
            bag_price = 0.0

    return {
        "user": frappe.session.user,
        "full_name": frappe.utils.get_fullname(frappe.session.user),
        "pos_profile": profile.name,
        "shop": profile.warehouse,
        "warehouse": profile.warehouse,
        "price_list": profile.selling_price_list,
        "currency": profile.currency or settings.default_currency or "SGD",
        "stock_mode": settings.stock_mode,
        "bag_price": bag_price,
        # GST-exclusive rate (%) for the cart's pre-charge tax preview; 0 when the profile applies no tax.
        "gst_rate": _pos_tax_rate(profile),
        "payment_methods": DEFAULT_HITPAY_METHODS,
        # The card-reader id is configured per-device on the app (each kiosk owns its reader) and passed to
        # start_payment, so whether to offer card-present is a device-local decision — not reported here.
    }


# --------------------------------------------------------------------------------------------------
# Catalogue (delta-sync): version probe, full snapshot, changed-since delta
# --------------------------------------------------------------------------------------------------
def _assert_catalog_read() -> None:
    for dt in ("Item", "Item Price", "Bin"):
        frappe.has_permission(dt, "read", throw=True)


def _catalog_version_value(price_list: str, warehouse: str) -> dict:
    """Cheap probe: newest change + row count across the priced items and their bins."""
    ip = frappe.db.sql(
        "SELECT MAX(modified) m, COUNT(*) c FROM `tabItem Price` WHERE price_list=%s AND selling=1",
        price_list,
    )[0]
    bn = frappe.db.sql("SELECT MAX(modified) m FROM `tabBin` WHERE warehouse=%s", warehouse)[0]
    # Catalogue rates now reflect Pricing Rules, so a rule edit (e.g. changing the category markup) must
    # also bump the version even though no Item Price row changed — else clients wouldn't resync.
    pr = frappe.db.sql("SELECT MAX(modified) m FROM `tabPricing Rule` WHERE selling=1")[0]
    stamps = [s for s in (ip[0], bn[0], pr[0]) if s]
    max_modified = max(stamps) if stamps else None
    return {"max_modified": str(max_modified) if max_modified else None, "count": int(ip[1] or 0)}


def _catalog_rows(price_list: str, warehouse: str, item_codes: list[str] | None = None) -> list[dict]:
    """Merged product rows (price + item meta + first barcode + on-hand qty) for the shop.

    Base set = items with a selling Item Price on ``price_list`` (only sellable items). When
    ``item_codes`` is given the result is restricted to those (used by the delta endpoint).
    """
    price_filters = {"price_list": price_list, "selling": 1}
    if item_codes is not None:
        if not item_codes:
            return []
        price_filters["item_code"] = ["in", item_codes]

    prices = frappe.get_all(
        "Item Price",
        filters=price_filters,
        fields=["item_code", "price_list_rate", "modified"],
        limit_page_length=_CATALOG_CAP,
        order_by="item_code asc",
    )
    codes = [p.item_code for p in prices]
    if not codes:
        return []

    # Effective per-item rate AFTER Pricing Rules (same engine the invoice uses) so the shopper's
    # catalogue price equals what checkout charges. effective_rates overlays engine-on-raw and never
    # throws, so a per-item fallback to the raw price_list_rate is still applied below for safety.
    eff = effective_rates(codes, price_list, warehouse=warehouse)

    items = {
        i.name: i
        for i in frappe.get_all(
            "Item",
            filters={"name": ["in", codes]},
            fields=["name", "item_name", "image", "stock_uom", "disabled"],
        )
    }
    barcodes: dict[str, str] = {}
    for b in frappe.get_all("Item Barcode", filters={"parent": ["in", codes]}, fields=["parent", "barcode"]):
        barcodes.setdefault(b.parent, b.barcode)
    stock: dict[str, float] = {}
    for bn in frappe.get_all(
        "Bin", filters={"item_code": ["in", codes], "warehouse": warehouse}, fields=["item_code", "actual_qty"]
    ):
        stock[bn.item_code] = flt(bn.actual_qty)

    rows = []
    for p in prices:
        it = items.get(p.item_code)
        if not it or it.disabled:
            continue
        rows.append(
            {
                "item_code": p.item_code,
                "item_name": it.item_name,
                "rate": flt(eff.get(p.item_code, p.price_list_rate)),
                "image": it.image,
                "uom": it.stock_uom,
                "barcode": barcodes.get(p.item_code),
                "stock_qty": stock.get(p.item_code, 0.0),
                "modified": str(p.modified) if p.modified else None,
            }
        )
    return rows


@frappe.whitelist()
def catalog_version() -> dict:
    """Version probe the client polls to decide whether to pull a delta."""
    _assert_catalog_read()
    profile = resolve_pos_profile()
    return _catalog_version_value(profile.selling_price_list, profile.warehouse)


@frappe.whitelist()
def catalog_snapshot() -> dict:
    """Full catalogue for the shop — the baseline load; deltas follow via ``catalog_changed_since``."""
    _assert_catalog_read()
    profile = resolve_pos_profile()
    price_list, warehouse = profile.selling_price_list, profile.warehouse
    return {
        "price_list": price_list,
        "warehouse": warehouse,
        "currency": profile.currency,
        "version": _catalog_version_value(price_list, warehouse),
        "items": _catalog_rows(price_list, warehouse),
    }


@frappe.whitelist()
def catalog_changed_since(since: str, since_key: str | None = None) -> dict:
    """Items whose price / meta / barcode / on-hand qty changed after ``since`` (client applies as a
    delta into its Room cache — never a full reload). ``since_key`` reserved for keyset paging.
    """
    _assert_catalog_read()
    profile = resolve_pos_profile()
    price_list, warehouse = profile.selling_price_list, profile.warehouse
    cutoff = get_datetime(since)

    codes: set[str] = set()
    codes |= set(
        frappe.get_all(
            "Item Price",
            filters={"price_list": price_list, "selling": 1, "modified": [">", cutoff]},
            pluck="item_code",
        )
    )
    codes |= set(frappe.get_all("Bin", filters={"warehouse": warehouse, "modified": [">", cutoff]}, pluck="item_code"))
    codes |= set(frappe.get_all("Item", filters={"modified": [">", cutoff]}, pluck="name"))
    codes |= set(frappe.get_all("Item Barcode", filters={"modified": [">", cutoff]}, pluck="parent"))

    rows = _catalog_rows(price_list, warehouse, list(codes)[:_CATALOG_CAP])
    return {
        "version": _catalog_version_value(price_list, warehouse),
        "items": rows,
    }


# --------------------------------------------------------------------------------------------------
# Checkout: create a pending session, start payment, poll status
# --------------------------------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
@with_deadlock_retry
def create_session(items_json, bag_qty: str = "0") -> dict:
    """Create a Pending Checkout Session from the cart and build its draft POS Invoice.

    The device's own totals are never trusted. A draft (unsubmitted) POS Invoice is built immediately and
    is the pricing source of truth — ERPNext prices it with the selling price list, Pricing Rules and
    taxes, and its total becomes the authoritative amount the HitPay request is built from. The draft is
    submitted on payment success (``finalize_paid_session``) or deleted on failure (``discard_draft_invoice``).
    """
    from pos import stock

    frappe.has_permission("Checkout Session", "create", throw=True)
    profile = resolve_pos_profile()
    settings = get_settings()
    price_list = profile.selling_price_list
    lines = parse_items(items_json)
    bags = frappe.utils.cint(bag_qty)
    if bags > 0 and not settings.bag_item:
        frappe.throw(_("Plastic bags were requested but no bag item is configured in Checkout Settings."))

    # Price the lines (and the bag) through the SAME pricing engine the draft invoice uses, so the
    # provisional session rates already reflect Pricing Rules / markups. The draft built below is still
    # the authoritative total; these seed the session record so it never carries a pre-markup figure.
    codes = [line["item_code"] for line in lines]
    if bags > 0:
        codes.append(settings.bag_item)
    rates = effective_rates(codes, price_list, warehouse=profile.warehouse)

    doc = frappe.new_doc("Checkout Session")
    doc.pos_profile = profile.name
    doc.warehouse = profile.warehouse
    doc.terminal_user = frappe.session.user
    doc.customer = settings.default_customer
    doc.currency = profile.currency or settings.default_currency or "SGD"
    doc.stock_mode = settings.stock_mode
    doc.status = "Pending"

    for line in lines:
        rate = rates.get(line["item_code"])
        if not rate:
            frappe.throw(_("No selling price for {0} on price list {1}.").format(line["item_code"], price_list))
        doc.append(
            "items",
            {
                "item_code": line["item_code"],
                "item_name": frappe.db.get_value("Item", line["item_code"], "item_name"),
                "barcode": line["barcode"],
                "qty": line["qty"],
                "rate": rate,
            },
        )

    if bags > 0:
        bag_rate = rates.get(settings.bag_item)
        if not bag_rate:
            frappe.throw(_("No selling price for the bag item {0} on price list {1}.").format(settings.bag_item, price_list))
        doc.bag_qty = bags
        doc.bag_amount = bags * bag_rate

    doc.insert()  # validate() recomputes provisional net/grand totals; permissions enforced (no ignore)

    # Build the draft POS Invoice — this is the authoritative pricing (rules + tax + rounding). Overwrite
    # the session's provisional total with the draft's so HitPay is charged exactly what will be booked.
    draft = stock.build_draft_invoice(doc)
    charge = flt(draft.rounded_total) or flt(draft.grand_total)
    # Refuse a zero/negative checkout total BEFORE payment. A 0 here means the draft invoice priced to
    # nothing — an item get_item_details couldn't resolve on the shop's price list (UOM/currency/price
    # list miss), or a 100%-off rule. Fail loudly at checkout instead of storing a 0 session that would
    # ask HitPay to charge 0.00. (Everything above is uncommitted, so the throw rolls back cleanly.)
    if charge <= 0:
        frappe.throw(
            _("This checkout totals {0} — there is nothing to charge. Check that every item is priced on "
              "the shop's price list ({1}).").format(charge, price_list)
        )
    frappe.db.set_value(
        "Checkout Session", doc.name, {"pos_invoice": draft.name, "grand_total": charge}, update_modified=False
    )

    frappe.db.commit()
    return {
        "session": doc.name,
        "reference_number": doc.name,
        "currency": doc.currency,
        # Display-only breakdown for the checkout screen. `grand_total` (below) remains the sole charged
        # amount pushed to HitPay — net_total/total_taxes never feed the payment.
        "net_total": flt(draft.net_total),
        "total_taxes": flt(draft.total_taxes_and_charges),
        "grand_total": charge,
    }


@frappe.whitelist(methods=["POST"])
def start_payment(session: str, method: str, terminal_id: str = "") -> dict:
    """Create the HitPay payment request for a session (server-side, key never on the device) and
    return what the screen should show: a QR payload, a hosted URL, or a terminal-wait flag.

    ``terminal_id`` is the device's own HitPay Wi-Fi card-reader id (configured per kiosk on the app);
    for the card-present method it routes the charge to that reader.
    """
    from pos import payments

    return payments.create_payment_request(session, method, terminal_id=terminal_id)


@frappe.whitelist()
def session_status(session: str) -> dict:
    """Poll fallback for the payment result (realtime ``checkout_update`` is the primary path)."""
    doc = frappe.get_doc("Checkout Session", session)
    doc.check_permission("read")
    return {
        "session": doc.name,
        "status": doc.status,
        "hitpay_request_id": doc.hitpay_request_id,
        "hitpay_payment_id": doc.hitpay_payment_id,
        "pos_invoice": doc.pos_invoice,
        "grand_total": flt(doc.grand_total),
        "paid_on": str(doc.paid_on) if doc.paid_on else None,
    }


@frappe.whitelist()
def get_receipt(session: str, fmt: str = "html") -> dict:
    """Render the session's submitted POS Invoice as a receipt using an ERPNext Print Format.

    ``fmt`` is ``html`` (default — for the in-app viewer) or ``pdf`` (base64, for printing). The layout is
    the one configured on the POS Profile (``print_format``); falls back to ERPNext's standard POS Invoice
    format when unset — the same format the built-in web POS uses.

    Runs as the caller (the cashier) with native permission checks: read on the session and on the POS
    Invoice (the self-contained Shop Cashier role holds both). No elevation, no ``ignore_permissions``.
    """
    doc = frappe.get_doc("Checkout Session", session)
    doc.check_permission("read")
    if not doc.pos_invoice:
        frappe.throw(_("This checkout has no invoice yet."))
    frappe.has_permission("POS Invoice", "read", doc.pos_invoice, throw=True)

    # Use the format configured on the POS Profile — the admin designs their own receipt Print Format in the
    # Print Formats tab and sets it there; ERPNext falls back to the doctype default when unset.
    print_format = frappe.db.get_value("POS Profile", doc.pos_profile, "print_format") or None

    if (fmt or "html").lower() == "pdf":
        pdf = frappe.get_print("POS Invoice", doc.pos_invoice, print_format=print_format, as_pdf=True)
        return {
            "session": doc.name,
            "pos_invoice": doc.pos_invoice,
            "format": "pdf",
            "filename": f"{doc.pos_invoice}.pdf",
            "content_base64": base64.b64encode(pdf).decode(),
        }

    html = frappe.get_print("POS Invoice", doc.pos_invoice, print_format=print_format)
    return {
        "session": doc.name,
        "pos_invoice": doc.pos_invoice,
        "format": "html",
        "html": html,
    }


# --------------------------------------------------------------------------------------------------
# Day sessions (End-of-day stock arm) — thin wrappers over stock.py
# --------------------------------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def open_day(pos_profile: str | None = None) -> dict:
    from pos import stock

    return stock.open_day(pos_profile)


@frappe.whitelist(methods=["POST"])
def close_day(pos_profile: str | None = None) -> dict:
    from pos import stock

    return stock.close_day(pos_profile)
