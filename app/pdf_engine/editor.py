"""
PDF Editor — coordinate-driven, font-preserving text replacement.

Strategy per replacement:
  1. Detect font (size + bold) from the ORIGINAL text span BEFORE redacting.
  2. Widen the redaction rect leftward so larger replacement values always fit.
  3. White-out the widened rect via redact annotation.
  4. Insert new text RIGHT-ALIGNED within the original right-boundary.
  5. Apply changes into the page content stream.

No overlays. No watermarks. No layout drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from app.models import ExtractedInvoice, InvoiceItem
from app.calculator.number_fmt import format_european

# Fallback fonts (PyMuPDF built-ins)
_FONT_REGULAR = "helv"   # Helvetica
_FONT_BOLD    = "hebo"   # Helvetica-Bold

# Extra breathing room (pts) added around replacement text when sizing the
# redaction rectangle.  Kept deliberately small so the whitened area never
# bleeds into adjacent columns (the quantity column is the closest neighbour).
_REDACT_PADDING = 3.0

# Fallback baseline offset used ONLY when span-origin detection fails entirely.
# In normal operation we use span["origin"][1] directly — the real PDF baseline.
_BASELINE_FALLBACK_OFFSET = 1.5   # pts above bbox.y1


@dataclass
class _Op:
    """One scheduled text replacement."""
    page_idx: int
    orig_text: str
    new_text: str
    # Tight rect of the ORIGINAL text (from search_for or raw bbox)
    tight_rect: fitz.Rect
    # Right edge of the column — the replacement is right-aligned to this x
    col_x1: float
    # ── Detected from source span BEFORE redacting (Phase 1) ──
    font_size: float = 9.0
    is_bold: bool = False
    # Exact PDF baseline y-coordinate extracted from span["origin"][1].
    # insert_text() places glyphs ON this coordinate, so copying it verbatim
    # gives pixel-perfect vertical alignment with the original text.
    # 0.0 means detection failed → fall back to bbox.y1 - _BASELINE_FALLBACK_OFFSET.
    origin_y: float = 0.0
    # Exact x where the original glyph run started (span["origin"][0]).
    # Used as fallback left anchor for left-anchored fields (e.g. unit prices)
    # so that same-width replacements land at the exact same x position.
    origin_x: float = 0.0


class PDFEditor:
    """
    Wraps a PyMuPDF Document and exposes targeted replacement methods.

    Usage::
        with PDFEditor(input_path) as editor:
            editor.apply_invoice_changes(original, updated, log)
            editor.commit(log)
            editor.save(output_path)
    """

    def __init__(self, pdf_path: str | Path) -> None:
        self._path = Path(pdf_path)
        self._doc: fitz.Document = fitz.open(str(self._path))
        self._ops: list[_Op] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def apply_invoice_changes(
        self,
        original: ExtractedInvoice,
        updated: ExtractedInvoice,
        log: list[str],
    ) -> None:
        """
        Diff original vs updated invoice → schedule text replacements for
        every changed value (items + aggregates).
        """
        orig_map = {i.item_number: i for i in original.items}
        upd_map  = {i.item_number: i for i in updated.items}

        for item_no, upd in upd_map.items():
            orig = orig_map.get(item_no)
            if orig is None:
                continue

            # Quantity is READ-ONLY: it is extracted for calculations only.
            # We never write it back unless the user explicitly asks for a
            # quantity change — which is a separate instruction type not yet
            # implemented.  Touching it would reformat "1.634" → "1.634,00"
            # (format_european always emits 2 decimal places), corrupting
            # the layout for integer quantities.
            self._diff_field("Unit Price", orig.unit_price, upd.unit_price,
                             orig.unit_price_bbox, log)
            self._diff_field("Amount",     orig.amount,     upd.amount,
                             orig.amount_bbox, log)

        for field, upd_agg in updated.aggregates.items():
            orig_agg = original.aggregates.get(field)
            if orig_agg is None:
                log.append(f"⚠ Aggregate {field} missing in original — skipped")
                continue
            if upd_agg.value != orig_agg.value:
                self._schedule(
                    orig_text=format_european(orig_agg.value),
                    new_text=format_european(upd_agg.value),
                    bbox=orig_agg.bbox,
                    label=str(field),
                    log=log,
                )

    def commit(self, log: list[str]) -> None:
        """Apply all scheduled replacements in-place on the document."""
        # Group by page
        by_page: dict[int, list[_Op]] = {}
        for op in self._ops:
            by_page.setdefault(op.page_idx, []).append(op)

        for page_idx, ops in by_page.items():
            page = self._doc[page_idx]

            # ── Phase 1: detect fonts + exact baseline BEFORE any redaction ─
            for op in ops:
                op.font_size, op.is_bold, op.origin_y, op.origin_x = (
                    _detect_font_info(page, op.tight_rect)
                )

            # ── Phase 2: add all redaction annotations ───────────────────
            for op in ops:
                fontname = _FONT_BOLD if op.is_bold else _FONT_REGULAR
                redact_rect = _widen_rect(
                    op.tight_rect, op.col_x1,
                    op.new_text, fontname, op.font_size,
                )
                page.add_redact_annot(redact_rect, fill=(1, 1, 1))

            # ── Phase 3: bake redactions (erase original text) ───────────
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            # ── Phase 4: insert replacement text (right-aligned) ─────────
            for op in ops:
                fontname = _FONT_BOLD if op.is_bold else _FONT_REGULAR
                text_w   = fitz.get_text_length(
                    op.new_text, fontname=fontname, fontsize=op.font_size
                )

                # ── X positioning ────────────────────────────────────────────
                # Right-align the replacement to the original text's RIGHT edge
                # (tight_rect.x1 = right edge of the matched text from search_for).
                # For same-width replacements (e.g. unit prices) this places the
                # new text at ~the same x as the original.
                # For wider replacements (long amounts) the text correctly
                # extends further left — standard right-aligned column behaviour.
                x0 = max(0.0, op.tight_rect.x1 - text_w)

                # ── Y positioning (the critical fix) ─────────────────────────
                # Use span["origin"][1] captured in Phase 1.  This is the EXACT
                # PDF baseline coordinate — the same coordinate system that
                # insert_text() uses.  No more estimation needed.
                # Fallback: bbox.y1 - offset (old behaviour, only if detection
                # failed and origin_y was left at its 0.0 sentinel).
                if op.origin_y > 0:
                    y = op.origin_y
                else:
                    y = op.tight_rect.y1 - _BASELINE_FALLBACK_OFFSET

                page.insert_text(
                    fitz.Point(x0, y),
                    op.new_text,
                    fontname=fontname,
                    fontsize=op.font_size,
                    color=(0, 0, 0),
                )
                log.append(
                    f"  ✏ '{op.orig_text}' → '{op.new_text}'  "
                    f"page {page_idx+1} @ ({x0:.1f},{y:.1f}) "
                    f"[origin_y={'detected' if op.origin_y > 0 else 'fallback'}] "
                    f"font={fontname} {op.font_size}pt"
                )

    def save(self, output_path: str | Path) -> None:
        """Save edited PDF with garbage collection and stream compression."""
        output_path = Path(output_path)
        self._doc.save(
            str(output_path),
            garbage=4,
            deflate=True,
            clean=True,
        )

    def close(self) -> None:
        if self._doc:
            self._doc.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _diff_field(
        self,
        label: str,
        orig_val: Decimal,
        new_val: Decimal,
        bbox,
        log: list[str],
    ) -> None:
        """Schedule a replacement only if the value actually changed."""
        if orig_val == new_val or bbox is None:
            return
        self._schedule(
            orig_text=format_european(orig_val),
            new_text=format_european(new_val),
            bbox=bbox,
            label=label,
            log=log,
        )

    def _schedule(
        self,
        orig_text: str,
        new_text: str,
        bbox,
        label: str,
        log: list[str],
    ) -> None:
        """
        Locate orig_text in the PDF near bbox, queue an _Op for it.

        Search strategy:
          1. search_for(orig_text)       — exact substring match
          2. search_for without dots     — handles merged EU-thousands tokens
          3. Fall back to raw bbox       — use pdfplumber coords directly
        """
        if bbox is None:
            log.append(f"⚠ {label}: no bbox — cannot locate '{orig_text}'")
            return

        page_idx, x0, y0, x1, y1 = bbox
        if page_idx >= len(self._doc):
            log.append(f"⚠ {label}: page {page_idx} out of range")
            return

        page = self._doc[page_idx]
        target = fitz.Rect(x0, y0, x1, y1)

        # Try to find the exact text string in the page
        hits = page.search_for(orig_text, quads=False)
        if not hits:
            # EU thousands dot sometimes missing in PDF stream
            alt = orig_text.replace(".", "")
            hits = page.search_for(alt, quads=False)

        tight_rect = _pick_closest(hits, target) if hits else target
        # col_x1 = right edge of the extracted bbox (column boundary)
        col_x1 = max(x1, tight_rect.x1)

        log.append(
            f"✔ Scheduled {label}: '{orig_text}' → '{new_text}'  "
            f"page {page_idx+1}  rect=({tight_rect.x0:.0f},{tight_rect.y0:.0f},"
            f"{tight_rect.x1:.0f},{tight_rect.y1:.0f})"
        )

        self._ops.append(_Op(
            page_idx=page_idx,
            orig_text=orig_text,
            new_text=new_text,
            tight_rect=tight_rect,
            col_x1=col_x1,
        ))


# ---------------------------------------------------------------------------
# Module-level utility functions
# ---------------------------------------------------------------------------

def _detect_font_info(
    page: fitz.Page, near: fitz.Rect
) -> tuple[float, bool, float, float]:
    """
    Find the nearest text span to `near` and return
    (font_size, is_bold, origin_y, origin_x).

    origin_y / origin_x are the span's ``span["origin"]`` coordinates —
    the exact PDF glyph baseline.  insert_text() expects to receive this
    y value directly, so copying it verbatim eliminates vertical drift.

    Falls back to (9.0, False, 0.0, 0.0) if no span is found.
    The 0.0 sentinel for origin_y tells the caller to use the bbox fallback.
    """
    best_size:     float = 9.0
    best_bold:     bool  = False
    best_origin_y: float = 0.0   # 0.0 = "not found" sentinel
    best_origin_x: float = 0.0
    min_dist = float("inf")

    # Pre-compute the centre of the search rect once
    cx = (near.x0 + near.x1) / 2
    cy = (near.y0 + near.y1) / 2

    for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("size", 0) <= 0:
                    continue
                sr = fitz.Rect(span["bbox"])
                # Manhattan distance between centres (fast, good enough)
                dx = abs((sr.x0 + sr.x1) / 2 - cx)
                dy = abs((sr.y0 + sr.y1) / 2 - cy)
                dist = dx + dy
                if dist < min_dist:
                    min_dist = dist
                    best_size = span["size"]
                    font_name = span.get("font", "").lower()
                    best_bold = "bold" in font_name
                    # span["origin"] = (x, y) of the glyph baseline origin
                    origin = span.get("origin", (0.0, 0.0))
                    best_origin_x = float(origin[0])
                    best_origin_y = float(origin[1])

    return best_size, best_bold, best_origin_y, best_origin_x


def _widen_rect(
    tight: fitz.Rect,
    col_x1: float,
    new_text: str,
    fontname: str,
    font_size: float,
) -> fitz.Rect:
    """
    Return a redaction rect that is exactly wide enough for new_text.

    Width = max(original text width, rendered new_text width) + _REDACT_PADDING.
    This is computed per-replacement so a short unit-price replacement like
    '72,55' does NOT widen as far left as a long amount like '1.380.916,70',
    which previously caused the whitening box to clip the adjacent quantity
    column.
    """
    rendered_w = fitz.get_text_length(new_text, fontname=fontname, fontsize=font_size)
    orig_w     = tight.x1 - tight.x0
    needed_w   = max(rendered_w, orig_w) + _REDACT_PADDING

    left  = col_x1 - needed_w
    left  = min(left, tight.x0)    # never cut right of where text started
    right = col_x1 + 1.0           # +1 pt anti-alias margin
    return fitz.Rect(left, tight.y0, right, tight.y1)


def _pick_closest(
    candidates: list[fitz.Rect], target: fitz.Rect
) -> Optional[fitz.Rect]:
    """Return the candidate whose centre is closest to target's centre."""
    if not candidates:
        return None
    tx = (target.x0 + target.x1) / 2
    ty = (target.y0 + target.y1) / 2

    def dist(r: fitz.Rect) -> float:
        dx = (r.x0 + r.x1) / 2 - tx
        dy = (r.y0 + r.y1) / 2 - ty
        return dx * dx + dy * dy

    return min(candidates, key=dist)
