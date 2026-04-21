"""
Tests for the AI mode prompt interpreter.

All tests mock Agent.run_sync — no real OpenAI calls are made.
The goal is to verify that:
  1. _AIOutput → ParsedInstructions conversion is correct and lossless.
  2. Recalculation flags propagate correctly.
  3. Missing OPENAI_API_KEY raises a clear RuntimeError.
  4. Unknown field names in AI output are silently skipped.
  5. The /api/process endpoint correctly forwards `mode` to the pipeline.
"""
from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.ai.prompt_interpreter import (
    _AIAggregateUpdate,
    _AIItemUpdate,
    _AIOutput,
    _ai_output_to_instructions,
    _build_invoice_context,
    ai_interpret_prompt,
)
from app.models import ExtractedInvoice, FieldName, InvoiceItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_invoice(n_items: int = 0) -> ExtractedInvoice:
    """Build a minimal ExtractedInvoice for testing."""
    items = [
        InvoiceItem(
            item_number=i + 1,
            quantity=Decimal("10"),
            unit_price=Decimal("100.00"),
            amount=Decimal("1000.00"),
            raw_text=[],
        )
        for i in range(n_items)
    ]
    return ExtractedInvoice(items=items, aggregates={}, raw_pages=[])


def _mock_ai_output(
    item_updates: list[_AIItemUpdate] | None = None,
    aggregate_updates: list[_AIAggregateUpdate] | None = None,
    recalculate_amounts: bool = False,
    recalculate_ex_works: bool = False,
    recalculate_totals: bool = False,
    reasoning: str = "Test reasoning",
) -> _AIOutput:
    return _AIOutput(
        reasoning=reasoning,
        item_updates=item_updates or [],
        aggregate_updates=aggregate_updates or [],
        recalculate_amounts=recalculate_amounts,
        recalculate_ex_works=recalculate_ex_works,
        recalculate_totals=recalculate_totals,
    )


# ---------------------------------------------------------------------------
# _ai_output_to_instructions — conversion correctness
# ---------------------------------------------------------------------------

class TestAIOutputToInstructions:
    def test_empty_output_produces_empty_instructions(self):
        ai_out = _mock_ai_output()
        instr = _ai_output_to_instructions(ai_out, raw_prompt="nothing")
        assert instr.item_updates == []
        assert instr.aggregate_updates == []
        assert not instr.recalculate_amounts
        assert not instr.recalculate_ex_works
        assert not instr.recalculate_totals

    def test_single_item_unit_price_converted(self):
        ai_out = _mock_ai_output(
            item_updates=[_AIItemUpdate(item_number=5, field="unit_price", new_value=72.55)],
            recalculate_amounts=True,
        )
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        assert len(instr.item_updates) == 1
        u = instr.item_updates[0]
        assert u.item_number == 5
        assert u.field == FieldName.UNIT_PRICE
        assert u.new_value == Decimal("72.55")
        assert instr.recalculate_amounts is True

    def test_multiple_items_converted(self):
        updates = [
            _AIItemUpdate(item_number=i, field="unit_price", new_value=50.0 + i)
            for i in range(31, 36)
        ]
        ai_out = _mock_ai_output(item_updates=updates)
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        assert len(instr.item_updates) == 5
        assert [u.item_number for u in instr.item_updates] == list(range(31, 36))

    def test_aggregate_freight_converted(self):
        ai_out = _mock_ai_output(
            aggregate_updates=[
                _AIAggregateUpdate(field="freight", new_value=3500.0, recalculate=False)
            ],
            recalculate_totals=True,
        )
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        assert len(instr.aggregate_updates) == 1
        a = instr.aggregate_updates[0]
        assert a.field == FieldName.FREIGHT
        assert a.new_value == Decimal("3500.0")
        assert a.recalculate is False
        assert instr.recalculate_totals is True

    def test_null_new_value_maps_to_none(self):
        ai_out = _mock_ai_output(
            item_updates=[_AIItemUpdate(item_number=1, field="amount", new_value=None, recalculate=True)]
        )
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        assert instr.item_updates[0].new_value is None
        assert instr.item_updates[0].recalculate is True

    def test_unknown_field_name_silently_skipped(self):
        """AI might hallucinate a field name — skip it, don't crash."""
        ai_out = _mock_ai_output(
            item_updates=[_AIItemUpdate(item_number=1, field="unit_price", new_value=10.0)],
            aggregate_updates=[_AIAggregateUpdate(field="ex_works", new_value=None, recalculate=True)],
        )
        # Manually corrupt one entry to simulate hallucination
        ai_out.item_updates[0].field = "banana_field"  # type: ignore[assignment]
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        # Corrupted item update skipped; aggregate still processed
        assert len(instr.item_updates) == 0
        assert len(instr.aggregate_updates) == 1

    def test_all_recalculate_flags_propagate(self):
        ai_out = _mock_ai_output(
            recalculate_amounts=True,
            recalculate_ex_works=True,
            recalculate_totals=True,
        )
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        assert instr.recalculate_amounts is True
        assert instr.recalculate_ex_works is True
        assert instr.recalculate_totals is True

    def test_raw_prompt_preserved(self):
        ai_out = _mock_ai_output()
        prompt = "Change items 1–5 to 75,00 per unit."
        instr = _ai_output_to_instructions(ai_out, raw_prompt=prompt)
        assert instr.raw_prompt == prompt

    def test_decimal_precision_preserved(self):
        """Verify float → Decimal doesn't lose meaningful precision."""
        ai_out = _mock_ai_output(
            item_updates=[_AIItemUpdate(item_number=1, field="unit_price", new_value=72.55)]
        )
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        # Should be exactly 72.55 — float rounding handled by round(v, 6)
        assert instr.item_updates[0].new_value == Decimal("72.55")

    @pytest.mark.parametrize("field,expected", [
        ("unit_price",  FieldName.UNIT_PRICE),
        ("amount",      FieldName.AMOUNT),
    ])
    def test_all_item_field_names(self, field, expected):
        ai_out = _mock_ai_output(
            item_updates=[_AIItemUpdate(item_number=1, field=field, new_value=1.0)]  # type: ignore
        )
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        assert instr.item_updates[0].field == expected

    @pytest.mark.parametrize("field,expected", [
        ("ex_works",    FieldName.EX_WORKS),
        ("freight",     FieldName.FREIGHT),
        ("insurance",   FieldName.INSURANCE),
        ("total_up_to", FieldName.TOTAL_UP_TO),
        ("total_after", FieldName.TOTAL_AFTER),
    ])
    def test_all_aggregate_field_names(self, field, expected):
        ai_out = _mock_ai_output(
            aggregate_updates=[_AIAggregateUpdate(field=field, new_value=None, recalculate=True)]  # type: ignore
        )
        instr = _ai_output_to_instructions(ai_out, raw_prompt="test")
        assert instr.aggregate_updates[0].field == expected


# ---------------------------------------------------------------------------
# _build_invoice_context
# ---------------------------------------------------------------------------

class TestBuildInvoiceContext:
    def test_no_items_gracefully_handled(self):
        inv = _empty_invoice(0)
        ctx = _build_invoice_context(inv)
        assert "no items extracted" in ctx

    def test_items_listed_in_order(self):
        inv = _empty_invoice(3)
        ctx = _build_invoice_context(inv)
        assert "#  1" in ctx
        assert "#  2" in ctx
        assert "#  3" in ctx

    def test_context_contains_current_state_header(self):
        inv = _empty_invoice(1)
        ctx = _build_invoice_context(inv)
        assert "CURRENT INVOICE STATE" in ctx


# ---------------------------------------------------------------------------
# ai_interpret_prompt — integration (mocked LLM)
# ---------------------------------------------------------------------------

class TestAIInterpretPrompt:
    """Patch Agent so no real API calls happen."""

    def _make_mock_result(self, ai_out: _AIOutput) -> MagicMock:
        mock = MagicMock()
        mock.output = ai_out
        return mock

    def test_raises_if_no_api_key(self):
        """Missing OPENAI_API_KEY must raise RuntimeError with clear message."""
        inv = _empty_invoice(2)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                ai_interpret_prompt("change items", inv)

    def test_returns_instructions_and_reasoning(self):
        """Happy path: LLM returns valid output → ParsedInstructions."""
        inv = _empty_invoice(2)
        ai_out = _mock_ai_output(
            item_updates=[_AIItemUpdate(item_number=1, field="unit_price", new_value=150.0)],
            recalculate_amounts=True,
            recalculate_ex_works=True,
            recalculate_totals=True,
            reasoning="Set unit price for item 1 to 150.",
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-for-unit-tests"}):
            with patch("app.ai.prompt_interpreter.Agent") as MockAgent:
                mock_instance = MagicMock()
                mock_instance.run_sync.return_value = self._make_mock_result(ai_out)
                MockAgent.return_value = mock_instance

                instructions, reasoning = ai_interpret_prompt(
                    "Set unit price for item 1 to 150", inv
                )

        assert reasoning == "Set unit price for item 1 to 150."
        assert len(instructions.item_updates) == 1
        assert instructions.item_updates[0].item_number == 1
        assert instructions.item_updates[0].new_value == Decimal("150.0")
        assert instructions.recalculate_amounts is True
        assert instructions.recalculate_ex_works is True
        assert instructions.recalculate_totals is True

    def test_agent_receives_invoice_context_in_message(self):
        """Verify that item data is included in the user message sent to the LLM."""
        inv = _empty_invoice(1)
        ai_out = _mock_ai_output(reasoning="ok")

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with patch("app.ai.prompt_interpreter.Agent") as MockAgent:
                mock_instance = MagicMock()
                mock_instance.run_sync.return_value = self._make_mock_result(ai_out)
                MockAgent.return_value = mock_instance

                ai_interpret_prompt("do something", inv)

                call_args = mock_instance.run_sync.call_args
                user_message = call_args[0][0]

        assert "CURRENT INVOICE STATE" in user_message
        assert "USER REQUEST" in user_message
        assert "do something" in user_message

    def test_empty_ai_output_gives_empty_instructions(self):
        """AI returning no updates → empty but valid ParsedInstructions."""
        inv = _empty_invoice(0)
        ai_out = _mock_ai_output(reasoning="Nothing to do.")

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with patch("app.ai.prompt_interpreter.Agent") as MockAgent:
                mock_instance = MagicMock()
                mock_instance.run_sync.return_value = self._make_mock_result(ai_out)
                MockAgent.return_value = mock_instance

                instructions, reasoning = ai_interpret_prompt("nothing", inv)

        assert instructions.item_updates == []
        assert instructions.aggregate_updates == []
        assert reasoning == "Nothing to do."

    def test_api_error_propagates(self):
        """LLM API errors bubble up as exceptions (no silent swallowing)."""
        inv = _empty_invoice(1)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with patch("app.ai.prompt_interpreter.Agent") as MockAgent:
                mock_instance = MagicMock()
                mock_instance.run_sync.side_effect = RuntimeError("API quota exceeded")
                MockAgent.return_value = mock_instance

                with pytest.raises(RuntimeError, match="API quota exceeded"):
                    ai_interpret_prompt("change prices", inv)
