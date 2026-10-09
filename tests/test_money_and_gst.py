from decimal import ROUND_HALF_UP, Decimal

from app.database import execute_write, query_one
from app.utils.financial import calculate_gst_amount, from_paise, to_paise


def test_paise_conversion_accuracy():
    """Verify lossless conversion between INR Decimal amounts and integer paise."""
    assert to_paise("1234.56") == 123456
    assert to_paise(Decimal("1234.56")) == 123456
    assert to_paise(0.01) == 1
    assert to_paise(0.00) == 0
    assert to_paise("0.05") == 5

    assert from_paise(123456) == Decimal("1234.56")
    assert from_paise(1) == Decimal("0.01")
    assert from_paise(0) == Decimal("0.00")
    assert from_paise(5) == Decimal("0.05")


def test_gst_line_item_rounding_rules():
    """
    Verify GST calculations per line item using ROUND_HALF_UP and line-summing:
    0%, 5%, 12%, 18%, 28%.
    """
    rates = [
        (Decimal("0.0"), Decimal("100.00"), Decimal("0.00")),
        (Decimal("5.0"), Decimal("100.00"), Decimal("5.00")),
        (Decimal("12.0"), Decimal("100.00"), Decimal("12.00")),
        (Decimal("18.0"), Decimal("100.00"), Decimal("18.00")),
        (Decimal("28.0"), Decimal("100.00"), Decimal("28.00")),
    ]
    for rate, taxable, expected_tax in rates:
        tax = calculate_gst_amount(taxable, rate)
        assert tax == expected_tax, f"Failed for rate {rate}: got {tax}, expected {expected_tax}"


def test_fractional_rate_and_quantity_half_up_rounding():
    """
    Test fractional quantities and fractional rates rounding correctly to the nearest paisa.
    e.g., 3 units @ ₹33.33 each with 18% GST:
    Taxable: 99.99
    Tax: 99.99 * 0.18 = 17.9982 -> 18.00
    Total: 117.99
    """
    qty = Decimal("3")
    unit_price = Decimal("33.33")
    taxable = (qty * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert taxable == Decimal("99.99")

    tax = calculate_gst_amount(taxable, Decimal("18.0"))
    assert tax == Decimal("18.00")
    total = taxable + tax
    assert total == Decimal("117.99")


def test_gst_multiple_line_items_independent_rounding(client, admin_token):
    """
    Line-item rounding test:
    Line 1: 1 x 10.25 @ 18% -> tax = 1.845 -> 1.85, total = 12.10
    Line 2: 1 x 10.25 @ 18% -> tax = 1.845 -> 1.85, total = 12.10
    Summed Taxable: 20.50
    Summed Tax: 3.70
    Summed Total: 24.20
    CollectFlow enforces per-line half-up rounding.
    """
    cust_res = client.post(
        "/api/customers",
        json={"billing_name": "GST Rounding Test Ltd", "gst_number": "27AAAAA0000A1Z5"},
        headers=admin_token,
    )
    assert cust_res.status_code == 200
    cust_id = cust_res.json()["customer_id"]

    inv_payload = {
        "customer_id": cust_id,
        "po_number": "PO-ROUND-GST-01",
        "po_date": "2026-05-10",
        "fy_code": "FY 2026-27",
        "exclusive_amount": 20.50,
        "items": [
            {"description": "Item A", "quantity": 1, "unit_price": 10.25, "tax_rate": 0.18},
            {"description": "Item B", "quantity": 1, "unit_price": 10.25, "tax_rate": 0.18},
        ],
    }
    res = client.post("/api/invoices", json=inv_payload, headers=admin_token)
    assert res.status_code == 200
    inv_id = res.json()["invoice_id"]

    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert Decimal(str(inv["exclusive_amount"])) == Decimal("20.50")
    assert Decimal(str(inv["tax_amount"])) == Decimal("3.70")
    assert Decimal(str(inv["total_amount"])) == Decimal("24.20")


def test_historical_tax_frozen_when_tax_master_changes(client, admin_token):
    """
    Ensure that updating a tax rate in the tax_rates master does not retroactively
    change amounts or line item taxes on previously issued invoices.
    """
    cust_res = client.post("/api/customers", json={"billing_name": "Tax Master Freeze Ltd"}, headers=admin_token)
    cust_id = cust_res.json()["customer_id"]

    inv_payload = {
        "customer_id": cust_id,
        "po_number": "PO-FREEZE-001",
        "po_date": "2026-06-01",
        "fy_code": "FY 2026-27",
        "exclusive_amount": 1000.0,
        "items": [{"description": "Service 18%", "quantity": 1, "unit_price": 1000.0, "tax_rate": 0.18}],
    }
    create_res = client.post("/api/invoices", json=inv_payload, headers=admin_token)
    assert create_res.status_code == 200
    inv_id = create_res.json()["invoice_id"]

    # Now simulate modifying tax master in DB
    execute_write("UPDATE tax_rates SET rate = 0.20 WHERE rate = 0.18")

    # Fetch the previously created invoice
    inv = query_one("SELECT * FROM v_invoice_summary WHERE id = ?", (inv_id,))
    assert Decimal(str(inv["exclusive_amount"])) == Decimal("1000.00")
    assert Decimal(str(inv["tax_amount"])) == Decimal("180.00")
    assert Decimal(str(inv["total_amount"])) == Decimal("1180.00")
