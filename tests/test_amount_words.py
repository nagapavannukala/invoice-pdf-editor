"""
Tests for app.calculator.amount_words — amount-to-words converter.

Covers: ones, teens, tens, hundreds, thousands, millions,
        cents, zero-cents (omission), compound phrases, and
        the exact strings that appear in the invoice PDF.
"""
from decimal import Decimal

import pytest

from app.calculator.amount_words import amount_to_words, _int_to_words


# ---------------------------------------------------------------------------
# _int_to_words unit tests
# ---------------------------------------------------------------------------

class TestIntToWords:
    def test_zero(self):
        assert _int_to_words(0) == "ZERO"

    def test_ones(self):
        assert _int_to_words(1) == "ONE"
        assert _int_to_words(9) == "NINE"

    def test_teens(self):
        assert _int_to_words(11) == "ELEVEN"
        assert _int_to_words(15) == "FIFTEEN"
        assert _int_to_words(19) == "NINETEEN"

    def test_tens(self):
        assert _int_to_words(20) == "TWENTY"
        assert _int_to_words(80) == "EIGHTY"
        assert _int_to_words(90) == "NINETY"

    def test_tens_with_units(self):
        assert _int_to_words(21) == "TWENTY-ONE"
        assert _int_to_words(38) == "THIRTY-EIGHT"
        assert _int_to_words(78) == "SEVENTY-EIGHT"
        assert _int_to_words(99) == "NINETY-NINE"

    def test_hundreds(self):
        assert _int_to_words(100) == "ONE HUNDRED"
        assert _int_to_words(700) == "SEVEN HUNDRED"
        assert _int_to_words(738) == "SEVEN HUNDRED THIRTY-EIGHT"
        assert _int_to_words(778) == "SEVEN HUNDRED SEVENTY-EIGHT"
        assert _int_to_words(999) == "NINE HUNDRED NINETY-NINE"

    def test_thousands(self):
        assert _int_to_words(1000)   == "ONE THOUSAND"
        assert _int_to_words(1001)   == "ONE THOUSAND ONE"
        assert _int_to_words(188000) == "ONE HUNDRED EIGHTY-EIGHT THOUSAND"
        assert _int_to_words(188738) == "ONE HUNDRED EIGHTY-EIGHT THOUSAND SEVEN HUNDRED THIRTY-EIGHT"

    def test_millions(self):
        assert _int_to_words(1000000)   == "ONE MILLION"
        assert _int_to_words(5000000)   == "FIVE MILLION"
        assert _int_to_words(1188738)   == (
            "ONE MILLION ONE HUNDRED EIGHTY-EIGHT THOUSAND SEVEN HUNDRED THIRTY-EIGHT"
        )
        assert _int_to_words(1188778)   == (
            "ONE MILLION ONE HUNDRED EIGHTY-EIGHT THOUSAND SEVEN HUNDRED SEVENTY-EIGHT"
        )


# ---------------------------------------------------------------------------
# amount_to_words integration tests
# ---------------------------------------------------------------------------

class TestAmountToWords:
    """Tests against the exact format used in the invoice PDF."""

    def test_invoice_original_total_up_to(self):
        """Matches the existing words line in the sample PDF exactly."""
        result = amount_to_words(Decimal("1188738.80"))
        assert result == (
            "ONE MILLION ONE HUNDRED EIGHTY-EIGHT THOUSAND "
            "SEVEN HUNDRED THIRTY-EIGHT US DOLLAR EIGHTY"
        )

    def test_invoice_updated_total_after(self):
        """The new words line produced after running the standard prompt."""
        result = amount_to_words(Decimal("1188778.80"))
        assert result == (
            "ONE MILLION ONE HUNDRED EIGHTY-EIGHT THOUSAND "
            "SEVEN HUNDRED SEVENTY-EIGHT US DOLLAR EIGHTY"
        )

    def test_whole_dollar_omits_cents(self):
        assert amount_to_words(Decimal("1000000.00")) == "ONE MILLION US DOLLAR"
        assert amount_to_words(Decimal("1000.00"))    == "ONE THOUSAND US DOLLAR"
        assert amount_to_words(Decimal("20.00"))      == "TWENTY US DOLLAR"

    def test_cents_only(self):
        assert amount_to_words(Decimal("0.99")) == "ZERO US DOLLAR NINETY-NINE"
        assert amount_to_words(Decimal("0.01")) == "ZERO US DOLLAR ONE"
        assert amount_to_words(Decimal("0.50")) == "ZERO US DOLLAR FIFTY"

    def test_teen_cents(self):
        assert amount_to_words(Decimal("19.19")) == "NINETEEN US DOLLAR NINETEEN"
        assert amount_to_words(Decimal("11.11")) == "ELEVEN US DOLLAR ELEVEN"

    def test_single_cent(self):
        assert amount_to_words(Decimal("100.01")) == "ONE HUNDRED US DOLLAR ONE"

    def test_fifty_cents(self):
        assert amount_to_words(Decimal("42.50")) == "FORTY-TWO US DOLLAR FIFTY"

    def test_large_amount_with_cents(self):
        assert amount_to_words(Decimal("999999.99")) == (
            "NINE HUNDRED NINETY-NINE THOUSAND NINE HUNDRED NINETY-NINE "
            "US DOLLAR NINETY-NINE"
        )

    def test_five_million(self):
        assert amount_to_words(Decimal("5000000.05")) == "FIVE MILLION US DOLLAR FIVE"

    def test_rounding(self):
        """0.005 rounds up to 0.01 cent → ONE"""
        assert amount_to_words(Decimal("1.005")) == "ONE US DOLLAR ONE"

    @pytest.mark.parametrize("val,expected_contains", [
        (Decimal("1188738.80"), "THIRTY-EIGHT"),
        (Decimal("1188778.80"), "SEVENTY-EIGHT"),
        (Decimal("100000.00"),  "ONE HUNDRED THOUSAND"),
    ])
    def test_key_phrases_present(self, val, expected_contains):
        assert expected_contains in amount_to_words(val)

    def test_always_uppercase(self):
        result = amount_to_words(Decimal("12345.67"))
        assert result == result.upper()

    def test_always_contains_us_dollar(self):
        for val in ["1.00", "0.50", "1000000.00", "999.99"]:
            assert "US DOLLAR" in amount_to_words(Decimal(val))
