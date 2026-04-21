"""
PDF Extractor — uses pdfplumber to extract structured invoice data
with precise bounding box coordinates for each text token.

Strategy:
  1. Extract all words with their bboxes (page, x0, y0, x1, y1).
  2. Cluster words into logical rows by vertical proximity.
  3. Identify item rows via item-number pattern in first column.
  4. Identify aggregate rows by label keywords.
  5. Return ExtractedInvoice with items + aggregates + raw blocks.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pdfplumber

from app.models import (
    AggregateSection,
    ExtractedInvoice,
    FieldName,
    InvoiceItem,
)
from app.calculator.number_fmt import parse_european

# Vertical tolerance (points) for grouping words into the same row
_ROW_TOLERANCE = 4.0

# Keywords that signal aggregate rows (case-insensitive).
# IMPORTANT: longer/more-specific phrases first — first match wins.
_AGG_KEYWORDS: dict[str, FieldName] = {
    "ex works amount":       FieldName.EX_WORKS,
    "ex works":              FieldName.EX_WORKS,
    "ex-works":              FieldName.EX_WORKS,
    "exworks":               FieldName.EX_WORKS,
    "freight + container":   FieldName.FREIGHT,
    "freight and container": FieldName.FREIGHT,
    "freight":               FieldName.FREIGHT,
    "insurance":             FieldName.INSURANCE,
    # Real PDF label is "Total Amount Up to" — "amount" sits between the key words
    "total amount up to":    FieldName.TOTAL_UP_TO,
    "total amount up":       FieldName.TOTAL_UP_TO,
    "total up to":           FieldName.TOTAL_UP_TO,
    "total upto":            FieldName.TOTAL_UP_TO,
    # Real PDF label is "Total Amount After"
    "total amount after":    FieldName.TOTAL_AFTER,
    "total after":           FieldName.TOTAL_AFTER,
    "grand total":           FieldName.TOTAL_AFTER,
}

# Pattern: a cell looks like a number (European or US, optional currency)
_NUM_RE = re.compile(
    r"^[€$£]?\s*[\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?$|^\d+(?:[.,]\d+)?$"
)


def extract_invoice(pdf_path: str | Path) -> ExtractedInvoice:
    """
    Main entry point — extracts structured data from an invoice PDF.
    Returns ExtractedInvoice with items, aggregates, and raw blocks.
    """
    pdf_path = Path(pdf_path)
    invoice = ExtractedInvoice()

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            words = page.extract_words(
                x_tolerance=3,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            raw_blocks = _to_raw_blocks(words, page_idx)
            invoice.raw_text_blocks.extend(raw_blocks)

            rows = _cluster_into_rows(words, page_idx)
            _parse_rows(rows, invoice)

    return invoice


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_raw_blocks(words: list[dict], page_idx: int) -> list[dict]:
    """Convert pdfplumber word dicts to our raw block format."""
    return [
        {
            "page": page_idx,
            "text": w["text"],
            "x0": w["x0"],
            "y0": w["top"],
            "x1": w["x1"],
            "y1": w["bottom"],
        }
        for w in words
    ]


def _cluster_into_rows(words: list[dict], page_idx: int) -> list[list[dict]]:
    """
    Group words into rows by vertical proximity.
    Returns sorted list of rows; each row is sorted left→right.
    """
    if not words:
        return []

    # Sort by top (y), then left (x)
    sorted_words = sorted(words, key=lambda w: (round(w["top"] / _ROW_TOLERANCE), w["x0"]))

    rows: list[list[dict]] = []
    current_row: list[dict] = [sorted_words[0]]
    current_y = sorted_words[0]["top"]

    for word in sorted_words[1:]:
        if abs(word["top"] - current_y) <= _ROW_TOLERANCE:
            current_row.append(word)
        else:
            rows.append(current_row)
            current_row = [word]
            current_y = word["top"]

    rows.append(current_row)

    # Attach page_idx to each word for bbox storage
    for row in rows:
        for w in row:
            w["_page"] = page_idx

    return rows


def _parse_rows(rows: list[list[dict]], invoice: ExtractedInvoice) -> None:
    """
    Attempt to classify each row as an item row, aggregate row, or other.
    Mutates invoice in-place.
    """
    for row in rows:
        if not row:
            continue

        row_text = " ".join(w["text"] for w in row).strip()
        row_lower = row_text.lower()

        # Check aggregate keywords first (they can appear anywhere)
        agg_field = _detect_aggregate_field(row_lower)
        if agg_field:
            _extract_aggregate_row(row, agg_field, invoice)
            continue

        # Check item row: first cell is a small integer (item number)
        first_text = row[0]["text"].strip()
        if re.match(r"^\d{1,3}$", first_text) and len(row) >= 3:
            _extract_item_row(row, invoice)


def _detect_aggregate_field(row_lower: str) -> FieldName | None:
    """Return FieldName if row contains an aggregate keyword."""
    for keyword, field in _AGG_KEYWORDS.items():
        if keyword in row_lower:
            return field
    return None


def _extract_item_row(row: list[dict], invoice: ExtractedInvoice) -> None:
    """
    Extract an item row. Expected column order:
    [item_no] [description...] [quantity] [unit_price] [amount]

    We identify numeric columns from the right side.
    """
    try:
        item_no = int(row[0]["text"].strip())
    except ValueError:
        return

    # Already seen this item number?
    if any(i.item_number == item_no for i in invoice.items):
        return

    # Collect numeric cells from right side
    numeric_cells = []
    desc_cells = []
    for w in row[1:]:
        txt = w["text"].strip().replace(" ", "")
        if _NUM_RE.match(txt):
            numeric_cells.append(w)
        else:
            desc_cells.append(w)

    # We expect [quantity, unit_price, amount] as last 3 numeric cols
    # (some invoices may have fewer — be defensive)
    quantity = Decimal("0")
    unit_price = Decimal("0")
    amount = Decimal("0")
    qty_bbox = None
    up_bbox = None
    amt_bbox = None
    page = row[0]["_page"]

    if len(numeric_cells) >= 3:
        qty_cell = numeric_cells[-3]
        up_cell = numeric_cells[-2]
        amt_cell = numeric_cells[-1]
        quantity = _safe_parse(qty_cell["text"])
        unit_price = _safe_parse(up_cell["text"])
        amount = _safe_parse(amt_cell["text"])
        qty_bbox = _bbox(page, qty_cell)
        up_bbox = _bbox(page, up_cell)
        amt_bbox = _bbox(page, amt_cell)
    elif len(numeric_cells) == 2:
        up_cell = numeric_cells[-2]
        amt_cell = numeric_cells[-1]
        unit_price = _safe_parse(up_cell["text"])
        amount = _safe_parse(amt_cell["text"])
        up_bbox = _bbox(page, up_cell)
        amt_bbox = _bbox(page, amt_cell)
    elif len(numeric_cells) == 1:
        amt_cell = numeric_cells[-1]
        amount = _safe_parse(amt_cell["text"])
        amt_bbox = _bbox(page, amt_cell)

    description = " ".join(w["text"] for w in desc_cells)

    invoice.items.append(
        InvoiceItem(
            item_number=item_no,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
            quantity_bbox=qty_bbox,
            unit_price_bbox=up_bbox,
            amount_bbox=amt_bbox,
        )
    )


def _extract_aggregate_row(
    row: list[dict], field: FieldName, invoice: ExtractedInvoice
) -> None:
    """Extract value and bbox from an aggregate row."""
    # The value is usually the last numeric token
    value = Decimal("0")
    val_bbox = None
    label_parts = []

    page = row[0]["_page"]
    numeric_candidates = []

    for w in row:
        txt = w["text"].strip().replace(" ", "")
        if _NUM_RE.match(txt):
            numeric_candidates.append(w)
        else:
            label_parts.append(w["text"])

    if numeric_candidates:
        val_word = numeric_candidates[-1]
        value = _safe_parse(val_word["text"])
        val_bbox = _bbox(page, val_word)

    label_text = " ".join(label_parts)

    # Only add if not already present (first occurrence wins)
    if field not in invoice.aggregates:
        invoice.aggregates[field] = AggregateSection(
            field=field,
            value=value,
            bbox=val_bbox,
            label_text=label_text,
        )


def _safe_parse(text: str) -> Decimal:
    """Parse number, returning 0 on failure."""
    try:
        return parse_european(text.strip())
    except (ValueError, Exception):
        return Decimal("0")


def _bbox(
    page: int, word: dict
) -> tuple[int, float, float, float, float]:
    """Build a bbox tuple from a pdfplumber word dict."""
    return (page, word["x0"], word["top"], word["x1"], word["bottom"])
