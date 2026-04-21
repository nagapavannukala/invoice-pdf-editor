"""
Pydantic models — the single source of truth for data shapes.
No surprises. No implicit casting shenanigans.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class FieldName(str, Enum):
    UNIT_PRICE = "unit_price"
    AMOUNT = "amount"
    EX_WORKS = "ex_works"
    FREIGHT = "freight"
    INSURANCE = "insurance"
    TOTAL_UP_TO = "total_up_to"
    TOTAL_AFTER = "total_after"


class ItemUpdate(BaseModel):
    """Represents a single item-level field update."""
    item_number: int = Field(..., ge=1, description="1-based item row number")
    field: FieldName
    new_value: Optional[Decimal] = Field(None, description="Explicit value, if provided")
    recalculate: bool = Field(False, description="If True, derive value from other fields")


class AggregateUpdate(BaseModel):
    """Represents an aggregate section update (Ex Works, Totals, etc.)."""
    field: FieldName
    new_value: Optional[Decimal] = Field(None, description="Explicit value, if provided")
    recalculate: bool = Field(True, description="Recalculate from item data if True")


class ParsedInstructions(BaseModel):
    """
    Fully parsed, structured representation of a user prompt.
    Prompt is the single source of truth — this object IS the prompt.
    """
    item_updates: list[ItemUpdate] = Field(default_factory=list)
    aggregate_updates: list[AggregateUpdate] = Field(default_factory=list)
    recalculate_amounts: bool = Field(
        False, description="Recalculate all Amount = Qty × Unit Price"
    )
    recalculate_ex_works: bool = Field(
        False, description="Recalculate Ex Works = Σ item amounts"
    )
    recalculate_totals: bool = Field(
        False, description="Recalculate Total Up To = Ex Works + Freight + Insurance"
    )
    raw_prompt: str = Field("", description="Original user prompt for audit trail")


class InvoiceItem(BaseModel):
    """Represents a single extracted invoice line item."""
    item_number: int
    description: str = ""
    quantity: Decimal = Decimal("0")
    unit_price: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")

    # Bounding box info for PDF editing (page, x0, y0, x1, y1)
    quantity_bbox: Optional[tuple[int, float, float, float, float]] = None
    unit_price_bbox: Optional[tuple[int, float, float, float, float]] = None
    amount_bbox: Optional[tuple[int, float, float, float, float]] = None

    model_config = {"arbitrary_types_allowed": True}


class AggregateSection(BaseModel):
    """Represents an extracted aggregate field (Ex Works, Freight, etc.)."""
    field: FieldName
    value: Decimal = Decimal("0")
    bbox: Optional[tuple[int, float, float, float, float]] = None
    label_text: str = ""  # raw text as it appears in the PDF

    model_config = {"arbitrary_types_allowed": True}


class ExtractedInvoice(BaseModel):
    """Full invoice data extracted from PDF."""
    items: list[InvoiceItem] = Field(default_factory=list)
    aggregates: dict[FieldName, AggregateSection] = Field(default_factory=dict)
    raw_text_blocks: list[dict] = Field(default_factory=list)  # for audit/debug

    # Page 2 "Invoice certified…for the amount of" bold words line.
    # Populated by the extractor using a fitz font-aware pass.
    # Empty string / None means the line was not found (non-fatal).
    amount_in_words_text: str = ""
    amount_in_words_bbox: Optional[tuple[int, float, float, float, float]] = None

    model_config = {"arbitrary_types_allowed": True}


class ValidationResult(BaseModel):
    """Output of the validation layer."""
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProcessingResult(BaseModel):
    """Final result returned by the processing pipeline."""
    success: bool
    download_token: str = ""
    validation: Optional[ValidationResult] = None
    errors: list[str] = Field(default_factory=list)
    log_summary: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}
