import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

ALLOWED_CURRENCIES = {"INR", "USD", "EUR", "GBP", "AED"}
DEFAULT_CURRENCY = "INR"


def to_decimal(val: Any) -> Decimal:
    """Convert any numeric/string value to Decimal safely with 2 decimal places."""
    if val is None:
        return Decimal("0.00")
    try:
        dec = Decimal(str(val))
        return dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def to_paise(val: Any) -> int:
    """Convert an amount in INR/currency to integer paise (e.g. ₹1,234.56 -> 123456)."""
    dec = to_decimal(val)
    return int((dec * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))


def from_paise(paise: Union[int, float]) -> Decimal:
    """Convert integer paise back to Decimal with 2 decimal places (e.g. 123456 -> Decimal('1234.56'))."""
    return (Decimal(int(paise)) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_currency(val: Any) -> float:
    """Round a monetary value to 2 decimal places using standard ROUND_HALF_UP."""
    dec = to_decimal(val)
    return float(dec)


def calculate_gst_amount(taxable: Any, rate_percentage: Any) -> Decimal:
    """
    Calculate GST amount from taxable value and GST percentage (e.g. 18.0).
    Uses standard half-up rounding to 2 decimal places.
    """
    t = to_decimal(taxable)
    r = Decimal(str(rate_percentage)) / Decimal("100")
    return (t * r).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_line_item(quantity: float, unit_price: float, tax_rate: float) -> Tuple[float, float, float]:
    """
    Calculate line item exclusive amount, tax amount, and total amount.
    Per CBIC GST guidelines: GST is calculated and rounded per line item to 2 decimals using ROUND_HALF_UP.
    Returns: (exclusive_amount, tax_amount, total_amount)
    """
    qty = Decimal(str(quantity))
    price = Decimal(str(unit_price))
    rate = Decimal(str(tax_rate))

    excl = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax = (excl * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tot = excl + tax

    return float(excl), float(tax), float(tot)


def calculate_invoice_totals(items: List[Dict[str, Any]]) -> Tuple[float, float, float]:
    """
    Aggregate totals across multiple line items.
    Each line item tax is calculated and rounded per line item, then summed.
    Returns: (exclusive_amount, tax_amount, total_amount)
    """
    total_excl = Decimal("0.00")
    total_tax = Decimal("0.00")

    for item in items:
        qty = Decimal(str(item.get("quantity", 1)))
        price = Decimal(str(item.get("unit_price", 0)))
        rate = Decimal(str(item.get("tax_rate", 0.18)))

        excl = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tax = (excl * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        total_excl += excl
        total_tax += tax

    tot = total_excl + total_tax
    return float(total_excl), float(total_tax), float(tot)


def calculate_net_30_due_date(invoice_date_str: Optional[str]) -> Optional[str]:
    """Calculate Net-30 due date from invoice date string (YYYY-MM-DD)."""
    if not invoice_date_str or not invoice_date_str.strip():
        return None
    try:
        d = datetime.date.fromisoformat(invoice_date_str.strip())
        return (d + datetime.timedelta(days=30)).isoformat()
    except ValueError:
        return None


def get_current_financial_year(dt: Optional[datetime.date] = None) -> str:
    """
    Compute Indian Fiscal Year code (April 1 - March 31).
    Example: 2026-10-07 -> 'FY 2026-27'
             2026-02-15 -> 'FY 2025-26'
    """
    target = dt or datetime.date.today()
    year = target.year
    month = target.month

    if month >= 4:
        start_year = year
        end_year = (year + 1) % 100
    else:
        start_year = year - 1
        end_year = year % 100

    return f"FY {start_year}-{end_year:02d}"


def is_date_in_fiscal_year(date_str: str, start_date_str: str, end_date_str: str) -> bool:
    """Validate whether an ISO date string falls within a financial year's start and end dates."""
    try:
        d = datetime.date.fromisoformat(date_str.strip())
        start_d = datetime.date.fromisoformat(start_date_str.strip())
        end_d = datetime.date.fromisoformat(end_date_str.strip())
        return start_d <= d <= end_d
    except (ValueError, AttributeError):
        return False


def sanitize_formula_injection(val: Any) -> Any:
    """
    Neutralize CSV / Spreadsheet Formula Injection (CWE-1236).
    If a text value starts with =, +, -, @, \\t, or \\r, prepend a single quote (')
    so spreadsheet software renders it strictly as literal text.
    """
    if isinstance(val, str) and val.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{val}"
    return val
