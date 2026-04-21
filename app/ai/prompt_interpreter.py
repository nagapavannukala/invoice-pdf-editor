"""
AI Prompt Interpreter — converts free-form natural language into ParsedInstructions.

Strategy:
  1. Build invoice context (current items + aggregates) as a string.
  2. Call gpt-4o-mini via Pydantic AI with a strict system prompt.
  3. LLM returns a _AIOutput Pydantic model (plain types, no Decimal/enum).
  4. _ai_output_to_instructions() converts that into the canonical ParsedInstructions.

The rest of the pipeline (calc → validate → edit PDF) is IDENTICAL to deterministic
mode — AI only replaces the parser, nothing else.

Environment:
  OPENAI_API_KEY — required for AI mode. If absent, a clear error is raised.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.models import (
    AggregateUpdate,
    ExtractedInvoice,
    FieldName,
    ItemUpdate,
    ParsedInstructions,
)
from app.calculator.number_fmt import format_european

# ---------------------------------------------------------------------------
# Intermediate output schema — plain types so the LLM can produce valid JSON
# without needing to know about Decimal or FieldName enums.
# ---------------------------------------------------------------------------

class _AIItemUpdate(BaseModel):
    item_number: int = Field(..., description="1-based item row number")
    field: Literal["unit_price", "amount"] = "unit_price"
    new_value: Optional[float] = Field(
        None,
        description="New value as a plain decimal (e.g. 72.55). "
                    "Null means the field will be recalculated, not set.",
    )
    recalculate: bool = Field(
        False,
        description="True if this field should be derived from others (rare).",
    )


class _AIAggregateUpdate(BaseModel):
    field: Literal[
        "ex_works", "freight", "insurance", "total_up_to", "total_after"
    ]
    new_value: Optional[float] = Field(
        None,
        description="Explicit new value as a plain decimal. "
                    "Null = recalculate from components.",
    )
    recalculate: bool = Field(True)


class _AIOutput(BaseModel):
    """Structured output returned by the LLM."""
    reasoning: str = Field(
        ...,
        description="One-sentence explanation of what the AI understood from the prompt.",
    )
    item_updates: list[_AIItemUpdate] = Field(default_factory=list)
    aggregate_updates: list[_AIAggregateUpdate] = Field(default_factory=list)
    recalculate_amounts: bool = Field(
        False,
        description="Recompute Amount = Quantity × Unit Price for all updated items.",
    )
    recalculate_ex_works: bool = Field(
        False,
        description="Recompute Ex Works = sum of item amounts.",
    )
    recalculate_totals: bool = Field(
        False,
        description="Recompute Total Up To and Total After.",
    )


# ---------------------------------------------------------------------------
# System prompt — the single source of truth for what the AI knows
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are an invoice PDF editing assistant. You convert natural-language editing
requests into structured JSON instructions that a deterministic engine will execute.

═══ INVOICE STRUCTURE ═══
Each invoice has:
  • Line items (rows) — each identified by an Item Number (integer, 1-based)
    Fields per item: Quantity (READ-ONLY), Unit Price, Amount
    Amount = Quantity × Unit Price (always)

  • Aggregate sections:
    - ex_works   : Ex Works Amount = sum of all item amounts
    - freight    : Freight + Container (explicit value or unchanged)
    - insurance  : Insurance (explicit value or unchanged)
    - total_up_to: Total Up To = Ex Works + Freight + Insurance
    - total_after: Total After = Total Up To + fixed delta (preserve delta unless told otherwise)

═══ WHAT YOU CAN DO ═══
  ✔ Set a new Unit Price for one or more items (by number, range, or list)
  ✔ Set a new Freight or Insurance amount
  ✔ Trigger recalculation of derived amounts (Amount, Ex Works, Totals)
  ✔ Adjust a percentage: "increase unit price by 10%" → compute new_value yourself
  ✔ Match a target total: reason backwards to find the required unit price

═══ WHAT YOU CANNOT DO ═══
  ✘ Add or remove invoice rows
  ✘ Change text descriptions, dates, item codes
  ✘ Change Quantity (it is read-only)
  ✘ Modify layout or visual structure

═══ FIELD NAMES (use exactly these strings) ═══
  Item fields     : "unit_price"  |  "amount"
  Aggregate fields: "ex_works"  |  "freight"  |  "insurance"  |  "total_up_to"  |  "total_after"

═══ NUMBER FORMAT ═══
  Output new_value as a plain float (e.g. 72.55), NOT European format.
  The invoice display uses European format (72,55) but your JSON must use dots.

═══ RECALCULATION FLAGS ═══
  recalculate_amounts  → set true when unit prices change (triggers Amount = Qty × Price)
  recalculate_ex_works → set true when item amounts change (triggers Ex Works = Σ amounts)
  recalculate_totals   → set true when Ex Works / Freight / Insurance change (triggers Totals)

  ALWAYS chain the flags: if you change unit prices, also set all three flags true.

═══ TYPICAL PATTERNS ═══
  "Set unit price to X for items A–B"
    → item_updates for each item with new_value=X
    → recalculate_amounts=true, recalculate_ex_works=true, recalculate_totals=true

  "Increase freight to X"
    → aggregate_updates [{field:"freight", new_value:X, recalculate:false}]
    → recalculate_totals=true

  "Recalculate everything"
    → recalculate_amounts=true, recalculate_ex_works=true, recalculate_totals=true

Always include a brief reasoning field explaining your interpretation.
""".strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ai_interpret_prompt(
    prompt: str,
    invoice: ExtractedInvoice,
) -> tuple[ParsedInstructions, str]:
    """
    Use GPT-4o-mini to convert *prompt* into ParsedInstructions.

    Args:
        prompt:  Free-form user text.
        invoice: Extracted invoice data — injected as context into the LLM message.

    Returns:
        (ParsedInstructions, reasoning_string)

    Raises:
        RuntimeError: If OPENAI_API_KEY is not set or the LLM call fails.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "AI mode requires an OpenAI API key. "
            "Set the OPENAI_API_KEY environment variable in your Render dashboard."
        )

    # Build the agent (constructed per-call so the key is always fresh)
    provider = OpenAIProvider(api_key=api_key)
    model    = OpenAIChatModel("gpt-4o-mini", provider=provider)
    agent: Agent[None, _AIOutput] = Agent(
        model=model,
        output_type=_AIOutput,
        instructions=_SYSTEM_PROMPT,
    )

    context = _build_invoice_context(invoice)
    user_message = f"{context}\n\n───\nUSER REQUEST:\n{prompt.strip()}"

    result = agent.run_sync(user_message)
    ai_out: _AIOutput = result.output

    instructions = _ai_output_to_instructions(ai_out, raw_prompt=prompt)
    return instructions, ai_out.reasoning


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_invoice_context(invoice: ExtractedInvoice) -> str:
    """
    Render a compact text summary of the current invoice state so the LLM
    has the numbers it needs to perform percentage calculations, etc.
    """
    lines: list[str] = ["CURRENT INVOICE STATE:"]

    if invoice.items:
        lines.append("Items (Item# | Qty | Unit Price | Amount):")
        for item in sorted(invoice.items, key=lambda i: i.item_number):
            lines.append(
                f"  #{item.item_number:>3}  Qty:{item.quantity}  "
                f"UnitPrice:{format_european(item.unit_price)}  "
                f"Amount:{format_european(item.amount)}"
            )
    else:
        lines.append("  (no items extracted)")

    if invoice.aggregates:
        lines.append("Aggregates:")
        _AGG_LABELS = {
            FieldName.EX_WORKS:    "Ex Works Amount",
            FieldName.FREIGHT:     "Freight + Container",
            FieldName.INSURANCE:   "Insurance",
            FieldName.TOTAL_UP_TO: "Total Up To",
            FieldName.TOTAL_AFTER: "Total After",
        }
        for field, agg in invoice.aggregates.items():
            label = _AGG_LABELS.get(field, str(field))
            lines.append(f"  {label}: {format_european(agg.value)}")

    return "\n".join(lines)


def _ai_output_to_instructions(
    ai_out: _AIOutput,
    raw_prompt: str,
) -> ParsedInstructions:
    """Convert _AIOutput (plain types) → canonical ParsedInstructions."""
    instructions = ParsedInstructions(raw_prompt=raw_prompt)
    instructions.recalculate_amounts  = ai_out.recalculate_amounts
    instructions.recalculate_ex_works = ai_out.recalculate_ex_works
    instructions.recalculate_totals   = ai_out.recalculate_totals

    for u in ai_out.item_updates:
        try:
            field = FieldName(u.field)
        except ValueError:
            continue
        instructions.item_updates.append(
            ItemUpdate(
                item_number=u.item_number,
                field=field,
                new_value=Decimal(str(round(u.new_value, 6))) if u.new_value is not None else None,
                recalculate=u.recalculate,
            )
        )

    for u in ai_out.aggregate_updates:
        try:
            field = FieldName(u.field)
        except ValueError:
            continue
        instructions.aggregate_updates.append(
            AggregateUpdate(
                field=field,
                new_value=Decimal(str(round(u.new_value, 6))) if u.new_value is not None else None,
                recalculate=u.recalculate,
            )
        )

    return instructions
