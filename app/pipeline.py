"""
Processing Pipeline — orchestrates the full invoice editing flow.

Flow:
  1. Extract invoice data from PDF (pdfplumber)
  2. Parse prompt → structured instructions
  3. Deep-copy invoice → apply calculations
  4. Validate updated invoice
  5. Edit PDF with PyMuPDF using coordinate diffs
  6. Return output path + validation result + log

All steps are stateless. The pipeline can be called concurrently.
"""
from __future__ import annotations

import copy
import tempfile
import uuid
from pathlib import Path

from app.models import ExtractedInvoice, ParsedInstructions, ProcessingResult
from app.parser.prompt_parser import parse_prompt
from app.calculator.invoice_calc import apply_instructions
from app.pdf_engine.extractor import extract_invoice
from app.pdf_engine.editor import PDFEditor
from app.validators.validator import validate


def run_pipeline(
    pdf_path: str | Path,
    prompt: str,
    output_dir: str | Path,
) -> tuple[ProcessingResult, Path | None]:
    """
    Execute the full invoice editing pipeline.

    Args:
        pdf_path:   Path to the uploaded PDF.
        prompt:     Raw user prompt string.
        output_dir: Directory to write the output PDF into.

    Returns:
        (ProcessingResult, output_pdf_path | None)
    """
    log: list[str] = []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # -- Step 1: Extract --------------------------------------------------
        log.append("📄 Step 1: Extracting invoice layout...")
        original_invoice: ExtractedInvoice = extract_invoice(pdf_path)
        log.append(
            f"   Found {len(original_invoice.items)} items, "
            f"{len(original_invoice.aggregates)} aggregate fields"
        )

        # -- Step 2: Parse prompt ---------------------------------------------
        log.append("🔍 Step 2: Parsing prompt...")
        instructions: ParsedInstructions = parse_prompt(prompt)
        log.append(
            f"   {len(instructions.item_updates)} item updates, "
            f"{len(instructions.aggregate_updates)} aggregate updates, "
            f"recalc_amounts={instructions.recalculate_amounts}, "
            f"recalc_ex_works={instructions.recalculate_ex_works}, "
            f"recalc_totals={instructions.recalculate_totals}"
        )

        # -- Step 3: Apply calculations ----------------------------------------
        log.append("🧮 Step 3: Applying calculations...")
        updated_invoice = copy.deepcopy(original_invoice)
        apply_instructions(updated_invoice, instructions, log)

        # -- Step 4: Validate --------------------------------------------------
        log.append("✅ Step 4: Validating...")
        validation = validate(original_invoice, updated_invoice, log)

        if not validation.passed:
            log.append("❌ Validation failed — aborting PDF write")
            return (
                ProcessingResult(
                    success=False,
                    validation=validation,
                    errors=validation.errors,
                    log_summary=log,
                ),
                None,
            )

        # -- Step 5: Edit PDF --------------------------------------------------
        log.append("✏️  Step 5: Editing PDF...")
        token = uuid.uuid4().hex
        output_path = output_dir / f"invoice_edited_{token}.pdf"

        with PDFEditor(pdf_path) as editor:
            editor.apply_invoice_changes(original_invoice, updated_invoice, log)
            editor.commit(log)
            editor.save(output_path)

        log.append(f"💾 Output written: {output_path.name}")

        return (
            ProcessingResult(
                success=True,
                download_token=token,
                validation=validation,
                errors=[],
                log_summary=log,
            ),
            output_path,
        )

    except Exception as exc:
        log.append(f"💥 Pipeline error: {exc}")
        return (
            ProcessingResult(
                success=False,
                errors=[str(exc)],
                log_summary=log,
            ),
            None,
        )
