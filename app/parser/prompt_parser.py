"""
Prompt Parser — converts raw natural-language prompt text into
deterministic ParsedInstructions. Zero hardcoded business logic here;
the prompt IS the source of truth.

Design: regex-driven, no LLM dependency, fully deterministic.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.models import (
    AggregateUpdate,
    FieldName,
    ItemUpdate,
    ParsedInstructions,
)
from app.calculator.number_fmt import parse_european

# ---------------------------------------------------------------------------
# Canonical field aliases — maps user language → FieldName enum
# ---------------------------------------------------------------------------
_FIELD_ALIASES: dict[str, FieldName] = {
    "unit price": FieldName.UNIT_PRICE,
    "unitprice": FieldName.UNIT_PRICE,
    "price": FieldName.UNIT_PRICE,
    "amount": FieldName.AMOUNT,
    "ex works": FieldName.EX_WORKS,
    "exworks": FieldName.EX_WORKS,
    "ex-works": FieldName.EX_WORKS,
    "freight": FieldName.FREIGHT,
    "freight + container": FieldName.FREIGHT,
    "freight and container": FieldName.FREIGHT,
    "insurance": FieldName.INSURANCE,
    "total up to": FieldName.TOTAL_UP_TO,
    "total upto": FieldName.TOTAL_UP_TO,
    "total": FieldName.TOTAL_UP_TO,
    "total after": FieldName.TOTAL_AFTER,
    "grand total": FieldName.TOTAL_AFTER,
}

# Number pattern: European (1.234,56) or plain (1234.56 / 1234)
_NUM_RE = re.compile(
    r"(?:EUR\s*|USD\s*|€\s*|\$\s*)?(\d{1,3}(?:[.,]\d{3})*(?:[,.]\d+)?|\d+(?:[.,]\d+)?)"
)

# Item range: "items 1-5", "item 3", "items 1 to 5", "items 1,2,3"
_ITEM_RANGE_RE = re.compile(
    r"items?\s+(\d+)\s*(?:[-–to]+\s*(\d+)|(?:,\s*\d+)*)?",
    re.IGNORECASE,
)

# "Update <field> for items X-Y to <value>"
_UPDATE_RE = re.compile(
    r"update\s+([\w\s+]+?)\s+(?:for\s+)?(?:items?\s+([\d,\s\-–to]+))?\s*(?:to|=|:)\s*([^\n]+)",
    re.IGNORECASE,
)

# "Recalculate Amount = Quantity × Unit Price"
_RECALC_AMOUNT_RE = re.compile(
    r"recalculate\s+amount",
    re.IGNORECASE,
)

# "Recalculate Ex Works" / "Update Ex Works"
_RECALC_EX_WORKS_RE = re.compile(
    r"(?:recalculate|update|recompute)\s+ex[\s-]?works",
    re.IGNORECASE,
)

# "Recalculate totals" / "Update Total Up To"
_RECALC_TOTALS_RE = re.compile(
    r"(?:recalculate|update|recompute)\s+(?:total[s]?|total\s+up\s+to)",
    re.IGNORECASE,
)

# Aggregate explicit value: "Set Freight to 500,00"
_AGG_SET_RE = re.compile(
    r"(?:set|update)\s+([\w\s+]+?)\s+(?:to|=|:)\s*([^\n]+)",
    re.IGNORECASE,
)


def _parse_item_numbers(raw: str) -> list[int]:
    """Parse item references like '1', '1-5', '1,2,3', '1 to 5'."""
    raw = raw.strip()
    # Range
    m = re.match(r"(\d+)\s*[-–to]+\s*(\d+)", raw, re.IGNORECASE)
    if m:
        return list(range(int(m.group(1)), int(m.group(2)) + 1))
    # Comma-separated
    if "," in raw:
        return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    # Single
    digits = re.findall(r"\d+", raw)
    return [int(d) for d in digits] if digits else []


def _resolve_field(raw: str) -> FieldName | None:
    """Map a raw field string to a FieldName enum value."""
    clean = raw.strip().lower()
    # Direct lookup
    if clean in _FIELD_ALIASES:
        return _FIELD_ALIASES[clean]
    # Partial match
    for alias, field in _FIELD_ALIASES.items():
        if alias in clean or clean in alias:
            return field
    return None


def _extract_number(raw: str) -> Decimal | None:
    """Extract the first parseable number from a string."""
    matches = _NUM_RE.findall(raw)
    for m in matches:
        try:
            return parse_european(m)
        except ValueError:
            continue
    return None


def parse_prompt(prompt: str) -> ParsedInstructions:
    """
    Entry point — converts raw prompt text into ParsedInstructions.

    Rules:
    - Each line is processed independently.
    - Order of instructions is preserved.
    - Unknown lines are silently ignored (logged externally).
    """
    instructions = ParsedInstructions(raw_prompt=prompt)
    lines = [ln.strip() for ln in prompt.splitlines() if ln.strip()]

    for line in lines:
        _process_line(line, instructions)

    return instructions


def _process_line(line: str, instr: ParsedInstructions) -> None:
    """Mutate instructions based on a single prompt line."""

    # --- Recalculate amount ---
    if _RECALC_AMOUNT_RE.search(line):
        instr.recalculate_amounts = True

    # --- Recalculate Ex Works ---
    if _RECALC_EX_WORKS_RE.search(line):
        instr.recalculate_ex_works = True

    # --- Recalculate totals ---
    if _RECALC_TOTALS_RE.search(line):
        instr.recalculate_totals = True

    # --- "Update Unit Price for items X-Y to VALUE" ---
    m = _UPDATE_RE.search(line)
    if m:
        raw_field = m.group(1)
        raw_items = m.group(2) or ""
        raw_value = m.group(3)

        field = _resolve_field(raw_field)
        if field is None:
            return  # Unknown field — skip

        value = _extract_number(raw_value)

        # Item-level update
        if raw_items:
            item_nums = _parse_item_numbers(raw_items)
            for num in item_nums:
                instr.item_updates.append(
                    ItemUpdate(
                        item_number=num,
                        field=field,
                        new_value=value,
                        recalculate=(value is None),
                    )
                )
        else:
            # Aggregate update
            _add_aggregate(instr, field, value, recalculate=(value is None))
        return

    # --- "Set Freight to 500,00" (aggregate without items) ---
    m2 = _AGG_SET_RE.search(line)
    if m2:
        raw_field = m2.group(1)
        raw_value = m2.group(2)
        field = _resolve_field(raw_field)
        if field is None:
            return
        value = _extract_number(raw_value)
        _add_aggregate(instr, field, value, recalculate=(value is None))


def _add_aggregate(
    instr: ParsedInstructions,
    field: FieldName,
    value,
    recalculate: bool,
) -> None:
    """Add or update an aggregate instruction, deduplicating by field."""
    existing = next(
        (a for a in instr.aggregate_updates if a.field == field), None
    )
    if existing:
        existing.new_value = value
        existing.recalculate = recalculate
    else:
        instr.aggregate_updates.append(
            AggregateUpdate(field=field, new_value=value, recalculate=recalculate)
        )
