"""
Prompt Parser v2 — converts raw natural-language prompt text into
deterministic ParsedInstructions.

Design goals:
  - Stateful multi-line parsing: "Set Field = Value" + "Apply to Items: X"
    are linked across consecutive lines, regardless of what comes between.
  - Mixed item ranges: "31-35,40" → [31,32,33,34,35,40]
  - Skips KEEP SAME / no-op directives
  - Recognises formula-style recalc hints (Amount = Qty × Price)
  - Format-agnostic: strips bullets, section headers, separator lines
  - No LLM, fully deterministic, O(n) per line
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
# Field alias table  (lower-case keys → FieldName)
# ---------------------------------------------------------------------------
_FIELD_ALIASES: dict[str, FieldName] = {
    "unit price":            FieldName.UNIT_PRICE,
    "unitprice":             FieldName.UNIT_PRICE,
    "unit_price":            FieldName.UNIT_PRICE,
    "price":                 FieldName.UNIT_PRICE,
    "amount":                FieldName.AMOUNT,
    "amount in usd":         FieldName.AMOUNT,
    "amount usd":            FieldName.AMOUNT,
    "ex works":              FieldName.EX_WORKS,
    "ex-works":              FieldName.EX_WORKS,
    "exworks":               FieldName.EX_WORKS,
    "ex works amount":       FieldName.EX_WORKS,
    "freight":               FieldName.FREIGHT,
    "freight + container":   FieldName.FREIGHT,
    "freight and container": FieldName.FREIGHT,
    "freight container":     FieldName.FREIGHT,
    "insurance":             FieldName.INSURANCE,
    "total up to":           FieldName.TOTAL_UP_TO,
    "total upto":            FieldName.TOTAL_UP_TO,
    "total":                 FieldName.TOTAL_UP_TO,
    "total after":           FieldName.TOTAL_AFTER,
    "grand total":           FieldName.TOTAL_AFTER,
}

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Numbers: EU/US mixed, optional currency prefix/suffix
# Alternatives (tried left-to-right by findall):
#   1. Full EU: 1.234,56  /  1.234.567,89
#   2. EU thousands only: 1.634  /  1.234.567
#   3. Plain with decimal: 72,55  /  1234.56  /  72
_NUM_RE = re.compile(
    r"(?:[A-Z]{2,3}\s*|[€$£¥₹]\s*)?"           # optional currency prefix
    r"(\d{1,3}(?:[.,]\d{3})*[.,]\d+"            # alt 1: EU/US full
    r"|\d{1,3}(?:[.,]\d{3})+"                   # alt 2: thousands-only
    r"|\d+(?:[.,]\d+)?)"                         # alt 3: plain
)

# "Apply to Items: 31-35,40"  or  "Apply to items 31-35,40"
_APPLY_ITEMS_RE = re.compile(
    r"apply\s+to\s+items?\s*[:\s]\s*([\d,\s\-\u2013to]+)",
    re.IGNORECASE,
)

# "Items: 31-35,40"  (shorthand without "Apply to")
_ITEMS_SHORTHAND_RE = re.compile(
    r"^items?\s*[:\s]\s*([\d,\s\-\u2013to]+)$",
    re.IGNORECASE,
)

# "Update/Set <field> [for items X] [to/=] <value>"
# Works for both inline ("Update Unit Price for items 1-5 to EUR 150,00")
# and set-only ("Set Unit Price = 72,55 USD /1 EA")
_FULL_UPDATE_RE = re.compile(
    r"(?:update|set)\s+([\w\s+\-]+?)\s+"          # action + field (non-greedy)
    r"(?:for\s+)?(?:items?\s+([\d,\s\-\u2013to]+))?"  # optional inline items
    r"\s*(?:to|=|:)\s*(.+)",                       # separator + value
    re.IGNORECASE,
)

# Recalculate amount  (both "Recalculate Amount" and formula forms)
_RECALC_AMOUNT_RE = re.compile(
    r"recalculate\s+amount"
    r"|amount\s*=\s*quantity\s*[×xX*\u00d7]"
    r"|amount\s*=\s*qty\s*[×xX*\u00d7]"
    r"|recompute\s+all\s+items",
    re.IGNORECASE,
)

# Recalculate Ex Works
_RECALC_EX_WORKS_RE = re.compile(
    r"(?:recalculate|update|recompute)\s+ex[\s\-]?works"
    r"|ex[\s\-]?works\s*=\s*sum",
    re.IGNORECASE,
)

# Recalculate Total Up To
_RECALC_TOTALS_RE = re.compile(
    r"(?:recalculate|update|recompute)\s+(?:totals?|total\s+up\s+to)"
    r"|total\s+up\s+to\s*=\s*ex",
    re.IGNORECASE,
)

# "KEEP SAME" / "preserve original" — explicit no-ops
_KEEP_SAME_RE = re.compile(
    r"keep\s+same|preserve\s+original",
    re.IGNORECASE,
)

# Separator lines (--- / ===)
_SEPARATOR_RE = re.compile(r"^[-=]{3,}\s*$")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_prompt(prompt: str) -> ParsedInstructions:
    """
    Convert raw prompt text into ParsedInstructions.

    Stateful: a "Set Field = Value" line sets a pending update.
    The NEXT "Apply to Items: X" line binds that update to the item list.
    If no "Apply to Items" follows before end-of-prompt, the pending update
    is flushed as an aggregate update.

    All other instruction types (recalculate flags, KEEP SAME, etc.) are
    processed independently without affecting the pending state.
    """
    instructions = ParsedInstructions(raw_prompt=prompt)

    lines = [_clean_line(ln) for ln in prompt.splitlines()]
    lines = [ln for ln in lines if ln and not _SEPARATOR_RE.match(ln)]

    # Pending "Set Field = Value" waiting for "Apply to Items"
    pending_field: FieldName | None = None
    pending_value: Decimal | None = None

    for line in lines:

        # ── Recalculation flags (independent of pending state) ────────────
        if _RECALC_AMOUNT_RE.search(line):
            instructions.recalculate_amounts = True
        if _RECALC_EX_WORKS_RE.search(line):
            instructions.recalculate_ex_works = True
        if _RECALC_TOTALS_RE.search(line):
            instructions.recalculate_totals = True

        # ── KEEP SAME / preserve → explicit no-op, skip ───────────────────
        if _KEEP_SAME_RE.search(line):
            continue

        # ── "Apply to Items: X" → resolve pending field update ───────────
        apply_m = _APPLY_ITEMS_RE.search(line) or _ITEMS_SHORTHAND_RE.match(line)
        if apply_m and pending_field is not None:
            item_nums = _parse_item_numbers(apply_m.group(1))
            for num in item_nums:
                instructions.item_updates.append(
                    ItemUpdate(
                        item_number=num,
                        field=pending_field,
                        new_value=pending_value,
                        recalculate=(pending_value is None),
                    )
                )
            pending_field = None
            pending_value = None
            continue

        # ── "Update/Set <field> [for items X] [to/=] <value>" ───────────
        upd_m = _FULL_UPDATE_RE.search(line)
        if upd_m:
            raw_field  = upd_m.group(1).strip()
            raw_items  = (upd_m.group(2) or "").strip()
            raw_value  = (upd_m.group(3) or "").strip()

            field = _resolve_field(raw_field)
            if field is None:
                continue  # Unknown field — silently skip

            value = _extract_number(raw_value)

            if raw_items:
                # Items specified inline → apply immediately, clear pending
                for num in _parse_item_numbers(raw_items):
                    instructions.item_updates.append(
                        ItemUpdate(
                            item_number=num,
                            field=field,
                            new_value=value,
                            recalculate=(value is None),
                        )
                    )
                pending_field = None
                pending_value = None
            else:
                # No items on this line → store as pending
                # If there's already a pending, flush it first
                if pending_field is not None:
                    _add_aggregate(instructions, pending_field, pending_value,
                                   recalculate=(pending_value is None))
                pending_field = field
                pending_value = value
            continue

        # ── Unrecognised line → leave pending intact, continue ────────────

    # Flush any trailing pending as an aggregate update
    if pending_field is not None:
        _add_aggregate(instructions, pending_field, pending_value,
                       recalculate=(pending_value is None))

    return instructions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_line(line: str) -> str:
    """Strip markdown bullets, leading dashes, excess whitespace."""
    line = line.strip()
    line = re.sub(r"^[-\u2013\u2022*]\s+", "", line)  # bullets: -, –, •, *
    return line.strip()


def _parse_item_numbers(raw: str) -> list[int]:
    """
    Parse complex item references into a deduplicated ordered list.

    Handles:
      '31-35,40'       → [31,32,33,34,35,40]
      '1 to 5, 8'      → [1,2,3,4,5,8]
      '1,2,3'          → [1,2,3]
      '5'              → [5]
      '1–5'            → [1,2,3,4,5]   (en-dash)
    """
    result: list[int] = []
    seen: set[int] = set()

    # Split on comma, process each segment independently
    for part in re.split(r",\s*", raw.strip()):
        part = part.strip()
        if not part:
            continue

        # Range: "31-35", "1 to 5", "1–5"
        rng = re.match(r"(\d+)\s*(?:-|\u2013|to)\s*(\d+)", part, re.IGNORECASE)
        if rng:
            for n in range(int(rng.group(1)), int(rng.group(2)) + 1):
                if n not in seen:
                    result.append(n)
                    seen.add(n)
        else:
            # Single number(s) in this segment
            for d in re.findall(r"\d+", part):
                n = int(d)
                if n not in seen:
                    result.append(n)
                    seen.add(n)

    return result


def _resolve_field(raw: str) -> FieldName | None:
    """Map a raw field string to a FieldName, or None if unknown."""
    clean = raw.strip().lower()
    # Direct lookup
    if clean in _FIELD_ALIASES:
        return _FIELD_ALIASES[clean]
    # Partial match (both directions)
    for alias, field in _FIELD_ALIASES.items():
        if alias in clean or clean in alias:
            return field
    return None


def _extract_number(raw: str) -> Decimal | None:
    """Extract the first parseable number from a raw value string."""
    for m in _NUM_RE.findall(raw):
        try:
            return parse_european(m)
        except ValueError:
            continue
    return None


def _add_aggregate(
    instr: ParsedInstructions,
    field: FieldName,
    value: Decimal | None,
    recalculate: bool,
) -> None:
    """Upsert an aggregate update, deduplicating by field."""
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
