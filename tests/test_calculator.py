"""
Tests for the invoice calculation engine.
Every formula must be provably correct.
"""
from decimal import Decimal

import pytest

from app.calculator.invoice_calc import apply_instructions
from app.models import (
    AggregateSection,
    ExtractedInvoice,
    FieldName,
    InvoiceItem,
    ItemUpdate,
    ParsedInstructions,
)


def _make_invoice(items: list[tuple]) -> ExtractedInvoice:
    """Helper: (item_no, qty, unit_price, amount) tuples → ExtractedInvoice."""
    inv = ExtractedInvoice()
    for no, qty, up, amt in items:
        inv.items.append(
            InvoiceItem(
                item_number=no,
                quantity=Decimal(str(qty)),
                unit_price=Decimal(str(up)),
                amount=Decimal(str(amt)),
            )
        )
    inv.aggregates[FieldName.EX_WORKS] = AggregateSection(
        field=FieldName.EX_WORKS, value=Decimal("0")
    )
    inv.aggregates[FieldName.FREIGHT] = AggregateSection(
        field=FieldName.FREIGHT, value=Decimal("500.00")
    )
    inv.aggregates[FieldName.INSURANCE] = AggregateSection(
        field=FieldName.INSURANCE, value=Decimal("100.00")
    )
    inv.aggregates[FieldName.TOTAL_UP_TO] = AggregateSection(
        field=FieldName.TOTAL_UP_TO, value=Decimal("0")
    )
    return inv


def test_apply_unit_price_update():
    inv = _make_invoice([(1, 10, 100, 1000)])
    instr = ParsedInstructions(
        item_updates=[ItemUpdate(item_number=1, field=FieldName.UNIT_PRICE, new_value=Decimal("150"))]
    )
    log = []
    apply_instructions(inv, instr, log)
    assert inv.items[0].unit_price == Decimal("150")


def test_recalculate_amounts():
    inv = _make_invoice([(1, 10, 150, 999), (2, 5, 200, 999)])
    instr = ParsedInstructions(recalculate_amounts=True)
    log = []
    apply_instructions(inv, instr, log)
    assert inv.items[0].amount == Decimal("1500.00")
    assert inv.items[1].amount == Decimal("1000.00")


def test_recalculate_ex_works():
    inv = _make_invoice([(1, 10, 150, 1500), (2, 5, 200, 1000)])
    instr = ParsedInstructions(recalculate_ex_works=True)
    log = []
    apply_instructions(inv, instr, log)
    assert inv.aggregates[FieldName.EX_WORKS].value == Decimal("2500.00")


def test_recalculate_total_up_to():
    inv = _make_invoice([(1, 10, 150, 1500)])
    inv.aggregates[FieldName.EX_WORKS].value = Decimal("1500.00")  # pre-set
    instr = ParsedInstructions(recalculate_totals=True)
    log = []
    apply_instructions(inv, instr, log)
    # 1500 + 500 + 100 = 2100
    assert inv.aggregates[FieldName.TOTAL_UP_TO].value == Decimal("2100.00")


def test_full_pipeline_calculation():
    """End-to-end calculation: update prices → recalc amounts → ex works → totals."""
    inv = _make_invoice([
        (1, 10, 100, 1000),
        (2, 5, 100, 500),
        (3, 20, 100, 2000),
    ])
    instr = ParsedInstructions(
        item_updates=[
            ItemUpdate(item_number=1, field=FieldName.UNIT_PRICE, new_value=Decimal("150")),
            ItemUpdate(item_number=2, field=FieldName.UNIT_PRICE, new_value=Decimal("150")),
            ItemUpdate(item_number=3, field=FieldName.UNIT_PRICE, new_value=Decimal("150")),
        ],
        recalculate_amounts=True,
        recalculate_ex_works=True,
        recalculate_totals=True,
    )
    log = []
    apply_instructions(inv, instr, log)

    assert inv.items[0].amount == Decimal("1500.00")
    assert inv.items[1].amount == Decimal("750.00")
    assert inv.items[2].amount == Decimal("3000.00")
    assert inv.aggregates[FieldName.EX_WORKS].value == Decimal("5250.00")
    # 5250 + 500 + 100 = 5850
    assert inv.aggregates[FieldName.TOTAL_UP_TO].value == Decimal("5850.00")


def test_missing_item_skipped():
    inv = _make_invoice([(1, 10, 100, 1000)])
    instr = ParsedInstructions(
        item_updates=[ItemUpdate(item_number=99, field=FieldName.UNIT_PRICE, new_value=Decimal("200"))]
    )
    log = []
    apply_instructions(inv, instr, log)
    assert any("not found" in l for l in log)
    assert inv.items[0].unit_price == Decimal("100")  # unchanged
