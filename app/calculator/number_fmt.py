"""
Number formatting utilities — European style (1.234,56) and reverse parsing.
The only place number formatting logic lives. DRY gospel.
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def parse_european(value: str, eu_first: bool = True) -> Decimal:
    """
    Parse a number string to Decimal, format-agnostic with EU-first heuristics.

    Disambiguation rules (applied in order):
      1. Both separators, comma LAST  → EU  (1.234,56  → 1234.56)
      2. Both separators, dot LAST    → US  (1,234.56  → 1234.56)
      3. Only dots, multiple          → EU thousands  (1.234.567 → 1234567)
      4. Only one dot, eu_first=True, exactly 3 digits after dot
                                      → EU thousands  (1.634 → 1634)
      5. Only one dot, other          → decimal        (62.45 → 62.45)
      6. Only comma(s), single comma  → EU decimal     (72,55 → 72.55)
      7. Multiple commas, no dot      → EU thousands   (remove all)
      8. No separators                → integer

    Examples:
        '1.634'       → Decimal('1634')      # EU thousands (rule 4)
        '3.993'       → Decimal('3993')      # EU thousands (rule 4)
        '62,45'       → Decimal('62.45')     # EU decimal   (rule 6)
        '1.234,56'    → Decimal('1234.56')   # EU mixed     (rule 1)
        '102.043,30'  → Decimal('102043.30') # EU mixed     (rule 1)
        '1,234.56'    → Decimal('1234.56')   # US mixed     (rule 2)
        '1234.56'     → Decimal('1234.56')   # plain decimal (rule 5)
    """
    v = value.strip().replace("\u00a0", "").replace(" ", "")
    # Strip 2–3 letter currency codes (EUR, USD, GBP …)
    v = re.sub(r"^[A-Z]{2,3}", "", v)
    # Strip currency symbols
    v = re.sub(r"^[€$£¥₹]", "", v)
    # Strip trailing unit info like '/1 EA', 'EA', 'pcs'
    v = re.sub(r"[/\\].*$", "", v)
    v = re.sub(r"\s*[A-Za-z]+\s*$", "", v)
    v = v.strip()

    if not v:
        raise ValueError(f"Cannot parse number: '{value}'")

    has_dot   = "." in v
    has_comma = "," in v
    dot_count   = v.count(".")
    comma_count = v.count(",")
    last_dot   = v.rfind(".")
    last_comma = v.rfind(",")

    if has_dot and has_comma:
        # Both separators present — last one is the decimal
        if last_comma > last_dot:
            # Rule 1: EU format  e.g. 1.234,56
            v = v.replace(".", "").replace(",", ".")
        else:
            # Rule 2: US format  e.g. 1,234.56
            v = v.replace(",", "")

    elif has_dot and not has_comma:
        # Only dots
        if dot_count > 1:
            # Rule 3: multiple dots → all EU thousands  e.g. 1.234.567
            v = v.replace(".", "")
        elif eu_first and (len(v) - last_dot - 1) == 3:
            # Rule 4: single dot + exactly 3 trailing digits → EU thousands
            # e.g. 1.634 → 1634,  3.993 → 3993
            v = v.replace(".", "")
        # else Rule 5: dot is decimal separator → leave as-is

    elif has_comma and not has_dot:
        # Only commas
        if comma_count == 1:
            # Rule 6: EU decimal  e.g. 72,55 → 72.55
            v = v.replace(",", ".")
        else:
            # Rule 7: multiple commas, no dot → treat all as thousands
            v = v.replace(",", "")
    # else Rule 8: no separators → pure integer, leave as-is

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
