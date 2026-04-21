"""
Invoice Calculation Engine — deterministic, no side effects.
Takes ExtractedInvoice + ParsedInstructions → mutated ExtractedInvoice.

Rule: Every calculation is explicit. No magic. No surprises.

Scoping rule:
  Amount recalculation and Ex-Works summing are SCOPED to the set of item
  numbers that appear in item_updates.  This prevents false-positive items
  (e.g. items from other pages / invoice sections) from contaminating sums.
  If item_updates is empty the scope falls back to ALL items (safe default).
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
    Apply ParsedInstructions to ExtractedInvoice (mutates in-place).
    Returns the mutated invoice for convenience.
    """
    # Which item numbers were explicitly targeted by the prompt?
    # Used to scope amount-recalc and Ex-Works to avoid false-positive rows.
    scoped: set[int] = {u.item_number for u in instructions.item_updates}

    # Step 1: Apply explicit item-level unit price updates
    _apply_item_updates(invoice, instructions.item_updates, log)

    # Step 2: Recalculate Amount = Qty × Unit Price
    #         Scoped to updated items — never touches untargeted rows.
    if instructions.recalculate_amounts:
        _recalculate_amounts(invoice, scoped, log)

    # Step 3: Apply explicit aggregate updates (Freight, Insurance, etc.)
    _apply_explicit_aggregates(invoice, instructions.aggregate_updates, log)

    # Step 4: Recalculate Ex Works = Σ item amounts
    #         Also scoped so multi-page false-positive items don't pollute sum.
    if instructions.recalculate_ex_works:
        _recalculate_ex_works(invoice, scoped, log)

    # Step 5: Recalculate Total Up To AND Total After (preserving delta)
    if instructions.recalculate_totals:
        _recalculate_totals(invoice, log)

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


def _recalculate_amounts(
    invoice: ExtractedInvoice,
    scoped_items: set[int],
    log: list[str],
) -> None:
    """
    Recalculate Amount = Quantity × Unit Price.
    Only touches items whose item_number is in scoped_items
    (falls back to ALL items if scoped_items is empty).
    """
    for item in invoice.items:
        if scoped_items and item.item_number not in scoped_items:
            continue   # leave untargeted items untouched
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
            continue
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


def _recalculate_ex_works(
    invoice: ExtractedInvoice,
    scoped_items: set[int],
    log: list[str],
) -> None:
    """
    Ex Works = Σ item amounts, scoped to the same item set as amount-recalc.
    If scoped_items is empty → sum ALL items.
    """
    if scoped_items:
        total = sum(
            item.amount for item in invoice.items
            if item.item_number in scoped_items
        )
    else:
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


def _recalculate_totals(invoice: ExtractedInvoice, log: list[str]) -> None:
    """
    Recalculate:
      Total Up To = Ex Works + Freight + Insurance
      Total After  = Total Up To + original_delta
                     (delta = original_after − original_up_to preserves fixed charges)
    """
    # Snapshot originals BEFORE we overwrite — needed for delta calc
    orig_up_to = _agg_value(invoice, FieldName.TOTAL_UP_TO)
    orig_after  = _agg_value(invoice, FieldName.TOTAL_AFTER)
    delta = orig_after - orig_up_to   # e.g. +40.00 fixed fee

    # ── Total Up To ──────────────────────────────────────────────────────────
    ex_works  = _agg_value(invoice, FieldName.EX_WORKS)
    freight   = _agg_value(invoice, FieldName.FREIGHT)
    insurance = _agg_value(invoice, FieldName.INSURANCE)
    new_up_to = round_decimal(ex_works + freight + insurance)

    agg_up = invoice.aggregates.get(FieldName.TOTAL_UP_TO)
    if agg_up is None:
        invoice.aggregates[FieldName.TOTAL_UP_TO] = AggregateSection(
            field=FieldName.TOTAL_UP_TO, value=new_up_to
        )
    else:
        agg_up.value = new_up_to

    log.append(
        f"✔ Total Up To: {ex_works} + {freight} + {insurance} = {new_up_to} "
        f"(was {orig_up_to})"
    )

    # ── Total After (preserve delta) ─────────────────────────────────────────
    if orig_after != Decimal("0") or invoice.aggregates.get(FieldName.TOTAL_AFTER):
        new_after = round_decimal(new_up_to + delta)
        agg_af = invoice.aggregates.get(FieldName.TOTAL_AFTER)
        if agg_af is None:
            invoice.aggregates[FieldName.TOTAL_AFTER] = AggregateSection(
                field=FieldName.TOTAL_AFTER, value=new_after
            )
        else:
            agg_af.value = new_after
        log.append(
            f"✔ Total After: {new_up_to} + delta({delta}) = {new_after} "
            f"(was {orig_after})"
        )


def _agg_value(invoice: ExtractedInvoice, field: FieldName) -> Decimal:
    """Safely get aggregate value, defaulting to 0."""
    agg = invoice.aggregates.get(field)
    return agg.value if agg else Decimal("0")
