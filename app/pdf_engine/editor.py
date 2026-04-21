"""
PDF Editor — uses PyMuPDF (fitz) to perform precise, coordinate-driven
text replacement. No overlays, no watermarks, no layout drift.

Strategy per replacement:
  1. Locate the target text in the page using search_for().
  2. Redact (white-out) the exact bounding rect.
  3. Insert new text at the same baseline position, matching font size.
  4. Apply redactions to bake changes into the page stream.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from app.models import (
    AggregateSection,
    ExtractedInvoice,
    FieldName,
    InvoiceItem,
)
from app.calculator.number_fmt import format_european

# Fallback font used when original font cannot be embedded
_FALLBACK_FONT = "helv"
# Tolerance (pts) for locating text when coordinates are slightly off
_SEARCH_TOLERANCE = 2.0


class PDFEditor:
    """
    Wraps a PyMuPDF Document and exposes targeted replacement methods.
    Usage:
        editor = PDFEditor(input_path)
        editor.apply_invoice_changes(original_invoice, updated_invoice)
        editor.save(output_path)
    """

    def __init__(self, pdf_path: str | Path) -> None:
        self._path = Path(pdf_path)
        self._doc: fitz.Document = fitz.open(str(self._path))
        self._replacements: list[tuple[int, fitz.Rect, str, str]] = []

    def apply_invoice_changes(
        self,
        original: ExtractedInvoice,
        updated: ExtractedInvoice,
        log: list[str],
    ) -> None:
        """
        Diff original vs updated invoice and schedule text replacements
        for every changed value.
        """
        orig_map = {i.item_number: i for i in original.items}
        upd_map = {i.item_number: i for i in updated.items}

        for item_no, upd_item in upd_map.items():
            orig_item = orig_map.get(item_no)
            if orig_item is None:
                continue

            self._maybe_replace_item_field(
                "Unit Price",
                orig_item.unit_price,
                upd_item.unit_price,
                orig_item.unit_price_bbox,
                log,
            )
            self._maybe_replace_item_field(
                "Amount",
                orig_item.amount,
                upd_item.amount,
                orig_item.amount_bbox,
                log,
            )
            self._maybe_replace_item_field(
                "Quantity",
                orig_item.quantity,
                upd_item.quantity,
                orig_item.quantity_bbox,
                log,
            )

        # Aggregates
        for field, upd_agg in updated.aggregates.items():
            orig_agg = original.aggregates.get(field)
            if orig_agg is None:
                log.append(f"⚠ Aggregate {field} not in original — cannot replace")
                continue
            if upd_agg.value != orig_agg.value:
                self._schedule_replacement(
                    orig_text=format_european(orig_agg.value),
                    new_text=format_european(upd_agg.value),
                    bbox=orig_agg.bbox,
                    label=str(field),
                    log=log,
                )

    def _maybe_replace_item_field(
        self,
        label: str,
        orig_val: Decimal,
        new_val: Decimal,
        bbox,
        log: list[str],
    ) -> None:
        if orig_val == new_val or bbox is None:
            return
        self._schedule_replacement(
            orig_text=format_european(orig_val),
            new_text=format_european(new_val),
            bbox=bbox,
            label=label,
            log=log,
        )

    def _schedule_replacement(
        self,
        orig_text: str,
        new_text: str,
        bbox,
        label: str,
        log: list[str],
    ) -> None:
        """
        Find orig_text near the given bbox coordinates and queue a replacement.
        """
        if bbox is None:
            log.append(f"⚠ {label}: no bbox — cannot locate '{orig_text}'")
            return

        page_idx, x0, y0, x1, y1 = bbox
        if page_idx >= len(self._doc):
            log.append(f"⚠ {label}: page {page_idx} out of range")
            return

        page = self._doc[page_idx]

        # Try exact text search first
        hits = page.search_for(orig_text)
        if not hits:
            # Try without thousands separator (sometimes PDF merges tokens)
            alt_text = orig_text.replace(".", "")
            hits = page.search_for(alt_text)
            if hits:
                orig_text = alt_text

        # Filter hits to those nearest our extracted bbox
        target_rect = fitz.Rect(x0, y0, x1, y1)
        best_rect = _closest_rect(hits, target_rect)

        if best_rect is None:
            # Fallback: use the extracted bbox directly
            log.append(
                f"⚠ {label}: could not locate '{orig_text}' via search — using raw bbox"
            )
            best_rect = target_rect

        log.append(f"✔ Replacing {label}: '{orig_text}' → '{new_text}' on page {page_idx + 1}")
        self._replacements.append((page_idx, best_rect, orig_text, new_text))

    def commit(self, log: list[str]) -> None:
        """Apply all scheduled replacements in-place on the document."""
        # Group by page for efficiency
        by_page: dict[int, list] = {}
        for page_idx, rect, orig, new in self._replacements:
            by_page.setdefault(page_idx, []).append((rect, orig, new))

        for page_idx, ops in by_page.items():
            page = self._doc[page_idx]

            for rect, orig, new in ops:
                # Detect font size from surrounding text
                font_size = _detect_fontsize(page, rect)

                # Redact the old text area (white fill, no border)
                page.add_redact_annot(
                    rect,
                    fill=(1, 1, 1),   # white
                    text="",
                )

            # Apply all redactions on this page at once
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            # Now insert the new text
            for rect, orig, new in ops:
                font_size = _detect_fontsize(page, rect)
                # Baseline: slightly above bottom of rect
                baseline_y = rect.y1 - 1.5
                # Right-align within the rect width (typical for number columns)
                text_width = fitz.get_text_length(new, fontname=_FALLBACK_FONT, fontsize=font_size)
                insert_x = max(rect.x0, rect.x1 - text_width)

                page.insert_text(
                    fitz.Point(insert_x, baseline_y),
                    new,
                    fontname=_FALLBACK_FONT,
                    fontsize=font_size,
                    color=(0, 0, 0),
                )
                log.append(f"  → Written '{new}' at ({insert_x:.1f}, {baseline_y:.1f}) page {page_idx + 1}")

    def save(self, output_path: str | Path) -> None:
        """Save the edited PDF to output_path with garbage collection."""
        output_path = Path(output_path)
        self._doc.save(
            str(output_path),
            garbage=4,          # maximum dead-object removal
            deflate=True,       # compress streams
            clean=True,         # clean content streams
        )
        self._doc.close()
        self._doc = fitz.open(str(output_path))  # reopen for integrity check

    def close(self) -> None:
        if self._doc:
            self._doc.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _closest_rect(
    candidates: list[fitz.Rect], target: fitz.Rect
) -> Optional[fitz.Rect]:
    """Return the candidate rect whose center is closest to target's center."""
    if not candidates:
        return None

    def center_dist(r: fitz.Rect) -> float:
        dx = (r.x0 + r.x1) / 2 - (target.x0 + target.x1) / 2
        dy = (r.y0 + r.y1) / 2 - (target.y0 + target.y1) / 2
        return dx * dx + dy * dy

    return min(candidates, key=center_dist)


def _detect_fontsize(page: fitz.Page, near_rect: fitz.Rect) -> float:
    """
    Estimate the font size used at near_rect by inspecting page spans.
    Falls back to 9.0 if nothing found.
    """
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    best_size = 9.0
    min_dist = float("inf")

    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                sr = fitz.Rect(span["bbox"])
                dx = abs((sr.x0 + sr.x1) / 2 - (near_rect.x0 + near_rect.x1) / 2)
                dy = abs((sr.y0 + sr.y1) / 2 - (near_rect.y0 + near_rect.y1) / 2)
                dist = dx + dy
                if dist < min_dist and span.get("size", 0) > 0:
                    min_dist = dist
                    best_size = span["size"]

    return best_size
