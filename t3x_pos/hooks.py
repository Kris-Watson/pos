app_name = "t3x_pos"
app_title = "Self Checkout"
app_publisher = "Self Checkout"
app_description = "Self-checkout point of sale (HitPay payments) on ERPNext"
app_email = "support@selfcheckout.local"
app_license = "MIT"

# ERPNext provides the POS Profile / POS Invoice / Item / Customer / Warehouse DocTypes we build on.
required_apps = ["erpnext"]

# Bootstrap the Shop Cashier role, the HitPay Mode of Payment + clearing account, a Walk-in Customer
# and default Checkout Settings right after install. See t3x_pos/install.py.
after_install = "t3x_pos.install.after_install"

# Belt-and-suspenders realtime: when a sale's POS Invoice is submitted (by the webhook conversion or
# anyone else), push the result to the originating cashier so their screen advances even if the direct
# webhook push was missed. See t3x_pos/realtime.py. Realtime is fire-and-forget — the app also
# reconciles via session_status(), this is only a latency optimisation.
doc_events = {
    "POS Invoice": {
        "on_submit": "t3x_pos.realtime.publish_sale_change",
    }
}
