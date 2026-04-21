"""
Invoice Calculation Engine — deterministic, no side effects.
Takes ExtractedInvoice + ParsedInstructions → mutated ExtractedInvoice.

Rule: Every calculation is explicit. No magic. No surprises.
"""
from __future__ import annotations

from decimal import Decimal

from app.models import (
    AggregateSection,
    ExtractedInvoice,
    FieldName,
    ItemUpdate,
    ParsedInstructions,
)
from app.calculator.number_fmt import round_decimal


def apply_instructions(
    invoice: ExtractedInvoice,
    instructions: ParsedInstructions,
    log: list[str],
) -> ExtractedInvoice:
    """
    Apply ParsedInstructions to ExtractedInvoice.
    Returns the mutated invoice (in-place for efficiency).
    All mutations are logged for audit trail.
    """
    # Step 1: Apply explicit item-level unit price updates
    _apply_item_updates(invoice, instructions.item_updates, log)

    # Step 2: Recalculate Amount = Qty × Unit Price (if requested)
    if instructions.recalculate_amounts:
        _recalculate_amounts(invoice, log)

    # Step 3: Apply explicit aggregate updates first (Freight, Insurance)
    _apply_explicit_aggregates(invoice, instructions.aggregate_updates, log)

    # Step 4: Recalculate Ex Works = Σ item amounts
    if instructions.recalculate_ex_works:
        _recalculate_ex_works(invoice, log)

    # Step 5: Recalculate Total Up To = Ex Works + Freight + Insurance
    if instructions.recalculate_totals:
        _recalculate_total_up_to(invoice, log)

    return invoice


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _apply_item_updates(
    invoice: ExtractedInvoice,
    updates: list[ItemUpdate],
    log: list[str],
) -> None:
    """Apply explicit item field updates (e.g. new Unit Price)."""
    item_map = {item.item_number: item for item in invoice.items}

    for upd in updates:
        item = item_map.get(upd.item_number)
        if item is None:
            log.append(f"⚠ Item {upd.item_number} not found in invoice — skipped")
            continue

        if upd.field == FieldName.UNIT_PRICE and upd.new_value is not None:
            old = item.unit_price
            item.unit_price = round_decimal(upd.new_value)
            log.append(
                f"✔ Item {upd.item_number}: Unit Price {old} → {item.unit_price}"
            )

        elif upd.field == FieldName.AMOUNT and upd.new_value is not None:
            old = item.amount
            item.amount = round_decimal(upd.new_value)
            log.append(
                f"✔ Item {upd.item_number}: Amount {old} → {item.amount}"
            )
        else:
            log.append(
                f"ℹ Item {upd.item_number}: field={upd.field}, "
                f"value={upd.new_value} — no action taken"
            )


def _recalculate_amounts(invoice: ExtractedInvoice, log: list[str]) -> None:
    """Recalculate Amount = Quantity × Unit Price for all items."""
    for item in invoice.items:
        old = item.amount
        item.amount = round_decimal(item.quantity * item.unit_price)
        log.append(
            f"✔ Item {item.item_number}: Amount recalculated "
            f"{item.quantity} × {item.unit_price} = {item.amount} (was {old})"
        )


def _apply_explicit_aggregates(
    invoice: ExtractedInvoice,
    updates: list,
    log: list[str],
) -> None:
    """Apply explicit new values for Freight, Insurance, etc."""
    for upd in updates:
        if upd.new_value is None:
            continue  # Recalculate-only — handled elsewhere
        agg = invoice.aggregates.get(upd.field)
        old_val = agg.value if agg else Decimal("0")

        if agg is None:
            invoice.aggregates[upd.field] = AggregateSection(
                field=upd.field, value=round_decimal(upd.new_value)
            )
        else:
            agg.value = round_decimal(upd.new_value)

        log.append(
            f"✔ Aggregate {upd.field}: {old_val} → {round_decimal(upd.new_value)}"
        )


def _recalculate_ex_works(invoice: ExtractedInvoice, log: list[str]) -> None:
    """Ex Works = Σ item amounts."""
    total = sum(item.amount for item in invoice.items)
    total = round_decimal(total)
    agg = invoice.aggregates.get(FieldName.EX_WORKS)
    old = agg.value if agg else Decimal("0")

    if agg is None:
        invoice.aggregates[FieldName.EX_WORKS] = AggregateSection(
            field=FieldName.EX_WORKS, value=total
        )
    else:
        agg.value = total

    log.append(f"✔ Ex Works recalculated: {old} → {total}")


def _recalculate_total_up_to(invoice: ExtractedInvoice, log: list[str]) -> None:
    """Total Up To = Ex Works + Freight + Insurance."""
    ex_works = _agg_value(invoice, FieldName.EX_WORKS)
    freight = _agg_value(invoice, FieldName.FREIGHT)
    insurance = _agg_value(invoice, FieldName.INSURANCE)
    total = round_decimal(ex_works + freight + insurance)

    agg = invoice.aggregates.get(FieldName.TOTAL_UP_TO)
    old = agg.value if agg else Decimal("0")

    if agg is None:
        invoice.aggregates[FieldName.TOTAL_UP_TO] = AggregateSection(
            field=FieldName.TOTAL_UP_TO, value=total
        )
    else:
        agg.value = total

    log.append(
        f"✔ Total Up To recalculated: {ex_works} + {freight} + {insurance} = {total} (was {old})"
    )


def _agg_value(invoice: ExtractedInvoice, field: FieldName) -> Decimal:
    """Safely get aggregate value, defaulting to 0."""
    agg = invoice.aggregates.get(field)
    return agg.value if agg else Decimal("0")
