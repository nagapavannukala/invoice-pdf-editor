"""
Tests for number formatting utilities — European format is non-negotiable.
"""
from decimal import Decimal

import pytest

from app.calculator.number_fmt import (
    format_european,
    parse_european,
    round_decimal,
)


# ── parse_european ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("1.234,56", Decimal("1234.56")),
    ("1.234.567,89", Decimal("1234567.89")),
    ("100,00", Decimal("100.00")),
    ("0,50", Decimal("0.50")),
    ("1234.56", Decimal("1234.56")),       # US fallback
    ("1,234.56", Decimal("1234.56")),      # US thousands
    ("EUR 150,00", Decimal("150.00")),     # currency prefix
    ("€ 2.500,00", Decimal("2500.00")),    # euro symbol
])
def test_parse_european(raw, expected):
    assert parse_european(raw) == expected


def test_parse_european_invalid():
    with pytest.raises(ValueError):
        parse_european("not_a_number")


# ── format_european ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (Decimal("1234.56"), "1.234,56"),
    (Decimal("1000000"), "1.000.000,00"),
    (Decimal("100"), "100,00"),
    (Decimal("0.5"), "0,50"),
    (Decimal("9999.999"), "10.000,00"),    # rounds up
    (Decimal("1234567.89"), "1.234.567,89"),
])
def test_format_european(value, expected):
    assert format_european(value) == expected


# ── round_decimal ───────────────────────────────────────────────────────────

def test_round_decimal():
    assert round_decimal(Decimal("1.2349")) == Decimal("1.23")
    assert round_decimal(Decimal("1.2350")) == Decimal("1.24")  # ROUND_HALF_UP
    assert round_decimal(Decimal("1.005"), 2) == Decimal("1.01")


# ── round-trip ──────────────────────────────────────────────────────────────

def test_parse_format_roundtrip():
    original = "1.234,56"
    parsed = parse_european(original)
    formatted = format_european(parsed)
    assert formatted == original
