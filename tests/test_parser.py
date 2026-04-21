"""
Tests for the prompt parser module.
These tests are the living specification of how prompts are parsed.
"""
from decimal import Decimal

import pytest

from app.parser.prompt_parser import parse_prompt
from app.models import FieldName


def test_parse_unit_price_range():
    prompt = "Update Unit Price for items 1-3 to EUR 150,00"
    result = parse_prompt(prompt)

    assert len(result.item_updates) == 3
    for i, upd in enumerate(result.item_updates, start=1):
        assert upd.item_number == i
        assert upd.field == FieldName.UNIT_PRICE
        assert upd.new_value == Decimal("150.00")


def test_parse_single_item():
    prompt = "Update Unit Price for item 5 to 200,50"
    result = parse_prompt(prompt)

    assert len(result.item_updates) == 1
    assert result.item_updates[0].item_number == 5
    assert result.item_updates[0].new_value == Decimal("200.50")


def test_parse_recalculate_amount():
    prompt = "Recalculate Amount = Quantity × Unit Price"
    result = parse_prompt(prompt)
    assert result.recalculate_amounts is True


def test_parse_recalculate_ex_works():
    prompt = "Recalculate Ex Works"
    result = parse_prompt(prompt)
    assert result.recalculate_ex_works is True


def test_parse_recalculate_totals():
    prompt = "Recalculate Total Up To"
    result = parse_prompt(prompt)
    assert result.recalculate_totals is True


def test_parse_freight_update():
    prompt = "Update Freight to EUR 2.500,00"
    result = parse_prompt(prompt)

    agg = next((a for a in result.aggregate_updates if a.field == FieldName.FREIGHT), None)
    assert agg is not None
    assert agg.new_value == Decimal("2500.00")


def test_parse_full_prompt():
    prompt = """Update Unit Price for items 1-5 to EUR 150,00
Recalculate Amount = Quantity × Unit Price
Recalculate Ex Works
Recalculate Total Up To"""
    result = parse_prompt(prompt)

    assert len(result.item_updates) == 5
    assert result.recalculate_amounts is True
    assert result.recalculate_ex_works is True
    assert result.recalculate_totals is True


def test_parse_european_number_with_thousands():
    prompt = "Update Unit Price for item 1 to 1.234,56"
    result = parse_prompt(prompt)
    assert result.item_updates[0].new_value == Decimal("1234.56")


def test_raw_prompt_preserved():
    prompt = "Recalculate Ex Works"
    result = parse_prompt(prompt)
    assert result.raw_prompt == prompt


def test_unknown_field_ignored():
    prompt = "Update FakeField for items 1-2 to 100,00"
    result = parse_prompt(prompt)
    # Should produce no item updates (field unknown)
    assert len(result.item_updates) == 0


def test_empty_prompt():
    result = parse_prompt("")
    assert len(result.item_updates) == 0
    assert len(result.aggregate_updates) == 0
    assert result.recalculate_amounts is False
