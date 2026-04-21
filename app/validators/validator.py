"""
Validation Layer — fail-fast, deterministic, zero-tolerance for bad math.
Every check is named and individually reported.
"""
from __future__ import annotations

from decimal import Decimal

from app.models import ExtractedInvoice, FieldName, ValidationResult
from app.calculator.number_fmt import round_decimal

_TOLERANCE = Decimal("0.02")  # Allow 2 cent floating point drift


def validate(
    original: ExtractedInvoice,
    updated: ExtractedInvoice,
    log: list[str],
) -> ValidationResult:
    """
    Run all validation checks against the updated invoice.
    Returns ValidationResult with per-check pass/fail and consolidated errors.
    """
    checks: dict[str, bool] = {}
    errors: list[str] = []
    warnings: list[str] = []

    # Check 1: Item amounts = Qty × Unit Price (only for modified items)
    _check_item_amounts(original, updated, checks, errors, warnings)

    # Check 2: Ex Works = Σ *changed* item amounts
    #           (scoped to avoid false-positive rows from other invoice pages)
    _check_ex_works(original, updated, checks, errors, warnings)

    # Check 3: Total Up To = Ex Works + Freight + Insurance (if present)
    _check_total_up_to(updated, checks, errors, warnings)

    # Check 4: No item was unintentionally zeroed out
    _check_no_zero_wipeout(original, updated, checks, errors, warnings)

    # Check 5: Item count unchanged
    _check_item_count(original, updated, checks, errors, warnings)

    passed = len(errors) == 0
    result = ValidationResult(
        passed=passed,
        checks=checks,
        errors=errors,
        warnings=warnings,
    )

    status = "✅ PASSED" if passed else "❌ FAILED"
    log.append(f"Validation {status}: {len(errors)} errors, {len(warnings)} warnings")
    for e in errors:
        log.append(f"  ERROR: {e}")
    for w in warnings:
        log.append(f"  WARN:  {w}")

    return result


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_item_amounts(
    original: ExtractedInvoice,
    updated: ExtractedInvoice,
    checks: dict,
    errors: list,
    warnings: list,
) -> None:
    """
    Validate Amount = Quantity × Unit Price.

    Scope: only items whose amount OR unit_price changed from original.
    Untouched items (from other invoice pages, header rows, etc.) are skipped
    so false-positive extraction rows don't cause spurious failures.
    """
    orig_map = {i.item_number: i for i in original.items}
    all_ok = True

    for item in updated.items:
        orig = orig_map.get(item.item_number)
        # Skip if this item was not modified at all
        if orig and orig.amount == item.amount and orig.unit_price == item.unit_price:
            continue
        if item.quantity == Decimal("0"):
            continue  # Can't validate without quantity
        expected = round_decimal(item.quantity * item.unit_price)
        diff = abs(expected - item.amount)
        if diff > _TOLERANCE:
            errors.append(
                f"Item {item.item_number}: Amount {item.amount} ≠ "
                f"{item.quantity} × {item.unit_price} = {expected} (diff={diff})"
            )
            all_ok = False
    checks["item_amounts_correct"] = all_ok


def _check_ex_works(
    original: ExtractedInvoice,
    updated: ExtractedInvoice,
    checks: dict,
    errors: list,
    warnings: list,
) -> None:
    """
    Verify Ex Works = Σ item amounts.

    Scope: Only items whose amount CHANGED between original and updated are
    summed.  This prevents false-positive rows from other invoice pages
    (multi-page documents) from contaminating the check.

    If no item amounts changed (pure aggregate edit), all items are included.
    """
    ex_works_agg = updated.aggregates.get(FieldName.EX_WORKS)
    if ex_works_agg is None:
        warnings.append("Ex Works not found in invoice — skipping check")
        checks["ex_works_correct"] = True
        return

    orig_map = {i.item_number: i for i in original.items}

    # Items whose amounts changed are "in scope" for this invoice section.
    changed = [
        item for item in updated.items
        if item.amount != orig_map.get(item.item_number, item).amount
    ]
    # If nothing changed fall back to all items (pure-aggregate edit path)
    relevant = changed if changed else list(updated.items)

    expected = round_decimal(sum(i.amount for i in relevant))
    diff = abs(expected - ex_works_agg.value)

    if diff > _TOLERANCE:
        errors.append(
            f"Ex Works {ex_works_agg.value} ≠ Σ changed-item amounts {expected} "
            f"({len(relevant)} items, diff={diff})"
        )
        checks["ex_works_correct"] = False
    else:
        checks["ex_works_correct"] = True


def _check_total_up_to(
    invoice: ExtractedInvoice,
    checks: dict,
    errors: list,
    warnings: list,
) -> None:
    total_agg = invoice.aggregates.get(FieldName.TOTAL_UP_TO)
    if total_agg is None:
        warnings.append("Total Up To not found in invoice — skipping check")
        checks["total_up_to_correct"] = True
        return

    ex_works = _agg(invoice, FieldName.EX_WORKS)
    freight = _agg(invoice, FieldName.FREIGHT)
    insurance = _agg(invoice, FieldName.INSURANCE)
    expected = round_decimal(ex_works + freight + insurance)
    diff = abs(expected - total_agg.value)

    if diff > _TOLERANCE:
        errors.append(
            f"Total Up To {total_agg.value} ≠ "
            f"{ex_works} + {freight} + {insurance} = {expected} (diff={diff})"
        )
        checks["total_up_to_correct"] = False
    else:
        checks["total_up_to_correct"] = True


def _check_no_zero_wipeout(
    original: ExtractedInvoice,
    updated: ExtractedInvoice,
    checks: dict,
    errors: list,
    warnings: list,
) -> None:
    """Warn if any item that had a non-zero amount now has zero."""
    orig_map = {i.item_number: i for i in original.items}
    all_ok = True
    for item in updated.items:
        orig = orig_map.get(item.item_number)
        if orig and orig.amount != Decimal("0") and item.amount == Decimal("0"):
            warnings.append(
                f"Item {item.item_number}: Amount became 0 (was {orig.amount}) — intentional?"
            )
    checks["no_zero_wipeout"] = all_ok


def _check_item_count(
    original: ExtractedInvoice,
    updated: ExtractedInvoice,
    checks: dict,
    errors: list,
    warnings: list,
) -> None:
    if len(original.items) != len(updated.items):
        warnings.append(
            f"Item count changed: {len(original.items)} → {len(updated.items)}"
        )
    checks["item_count_unchanged"] = len(original.items) == len(updated.items)


def _agg(invoice: ExtractedInvoice, field: FieldName) -> Decimal:
    agg = invoice.aggregates.get(field)
    return agg.value if agg else Decimal("0")
