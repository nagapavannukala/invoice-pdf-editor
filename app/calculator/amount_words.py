"""
Amount-to-words converter — invoice style, uppercase English.

Converts a Decimal monetary amount into the uppercase phrase used on invoices:
  Decimal("1188778.80")
  → "ONE MILLION ONE HUNDRED EIGHTY-EIGHT THOUSAND SEVEN HUNDRED SEVENTY-EIGHT
     US DOLLAR EIGHTY"

Rules:
  - Integer part → English words, space-separated groups
  - Hyphen between tens and units (e.g. THIRTY-EIGHT, SEVENTY-EIGHT)
  - Currency separator: "US DOLLAR"
  - Cents: English words for the cent integer (0–99).
           Omitted entirely when cents == 0.
  - Everything UPPERCASE.

No third-party dependencies — standard library only.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Lookup tables — indexed by integer value
_ONES: list[str] = [
    "", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
    "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN",
    "SEVENTEEN", "EIGHTEEN", "NINETEEN",
]
_TENS: list[str] = [
    "", "", "TWENTY", "THIRTY", "FORTY", "FIFTY",
    "SIXTY", "SEVENTY", "EIGHTY", "NINETY",
]


def _chunk_to_words(n: int) -> str:
    """
    Convert an integer 1–999 to words.
    Caller guarantees n > 0 and n < 1000.
    """
    parts: list[str] = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} HUNDRED")
        n %= 100
    if n >= 20:
        word = _TENS[n // 10]
        if n % 10:
            word += f"-{_ONES[n % 10]}"
        parts.append(word)
    elif n > 0:
        parts.append(_ONES[n])
    return " ".join(parts)


def _int_to_words(n: int) -> str:
    """
    Convert a non-negative integer to uppercase English words.
    Supports 0 through 999,999,999 (sufficient for all invoice amounts).
    """
    if n == 0:
        return "ZERO"
    if n < 0:
        return f"MINUS {_int_to_words(-n)}"

    parts: list[str] = []
    if n >= 1_000_000:
        parts.append(f"{_chunk_to_words(n // 1_000_000)} MILLION")
        n %= 1_000_000
    if n >= 1_000:
        parts.append(f"{_chunk_to_words(n // 1_000)} THOUSAND")
        n %= 1_000
    if n > 0:
        parts.append(_chunk_to_words(n))

    return " ".join(parts)


def amount_to_words(value: Decimal) -> str:
    """
    Convert a Decimal monetary amount to invoice-style uppercase English words.

    Examples::
        amount_to_words(Decimal("1188778.80"))
        → "ONE MILLION ONE HUNDRED EIGHTY-EIGHT THOUSAND SEVEN HUNDRED
           SEVENTY-EIGHT US DOLLAR EIGHTY"

        amount_to_words(Decimal("1000.00"))
        → "ONE THOUSAND US DOLLAR"

        amount_to_words(Decimal("42.50"))
        → "FORTY-TWO US DOLLAR FIFTY"
    """
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Work entirely in integer cents to avoid any float/rounding noise.
    total_cents = int(quantized * 100)
    dollars = total_cents // 100
    cents   = total_cents % 100

    dollar_words = _int_to_words(abs(dollars))
    result = f"{dollar_words} US DOLLAR"

    if cents:
        result += f" {_int_to_words(cents)}"

    return result
