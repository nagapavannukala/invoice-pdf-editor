"""
Number formatting utilities — European style (1.234,56) and reverse parsing.
The only place number formatting logic lives. DRY gospel.
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def parse_european(value: str) -> Decimal:
    """
    Parse a European-formatted number string to Decimal.

    Examples:
        '1.234,56'  → Decimal('1234.56')
        '1.234.567,89' → Decimal('1234567.89')
        '100,00'    → Decimal('100.00')
        '1,234.56'  → Decimal('1234.56')  (US fallback)
    """
    v = value.strip().replace("\u00a0", "").replace(" ", "")
    # Strip leading currency codes (e.g. EUR, USD, GBP) and symbols
    v = re.sub(r"^[A-Z]{2,3}", "", v)  # 3-letter currency codes first
    v = re.sub(r"^[€$£¥₹]", "", v)    # symbol fallback

    # Detect format: if last separator is comma → European
    last_dot = v.rfind(".")
    last_comma = v.rfind(",")

    if last_comma > last_dot:
        # European: dots are thousands, comma is decimal
        v = v.replace(".", "").replace(",", ".")
    elif last_dot > last_comma:
        # US/standard: commas are thousands, dot is decimal
        v = v.replace(",", "")
    # else: no separator at all, or only one type — leave as is

    try:
        return Decimal(v)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse number: '{value}'") from exc


def format_european(value: Decimal, decimal_places: int = 2) -> str:
    """
    Format a Decimal as a European number string.

    Examples:
        Decimal('1234.56') → '1.234,56'
        Decimal('1000000') → '1.000.000,00'
    """
    quantized = value.quantize(
        Decimal(10) ** -decimal_places, rounding=ROUND_HALF_UP
    )
    # Split into integer and fractional parts
    sign, digits, exponent = quantized.as_tuple()
    decimal_str = f"{quantized:.{decimal_places}f}"  # e.g. '1234.56'

    int_part, dec_part = decimal_str.split(".")
    # Add thousands dots
    int_part_formatted = _add_thousands_dot(int_part)
    result = f"{int_part_formatted},{dec_part}"
    return result


def _add_thousands_dot(int_str: str) -> str:
    """Insert dot as thousands separator into an integer string."""
    negative = int_str.startswith("-")
    digits = int_str.lstrip("-")
    # Group from right
    groups = []
    while len(digits) > 3:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.append(digits)
    result = ".".join(reversed(groups))
    return f"-{result}" if negative else result


def round_decimal(value: Decimal, places: int = 2) -> Decimal:
    """Round to given decimal places using ROUND_HALF_UP."""
    return value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)
