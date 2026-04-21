"""
API Routes — FastAPI router for the invoice PDF editor.

Endpoints:
  POST /api/process  — upload PDF + prompt → run pipeline → return result JSON
  GET  /api/download/{token} — stream the edited PDF for download
  GET  /api/health   — health check

Files are stored in a temp directory and cleaned after download.
No sensitive data is persisted beyond the request lifecycle.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from typing import Literal

from app.pipeline import run_pipeline

router = APIRouter(prefix="/api")

# Temporary output directory — auto-cleaned by OS on reboot
_OUTPUT_DIR = Path(tempfile.gettempdir()) / "invoice_pdf_editor_outputs"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory token → path registry (stateless per-process, good enough for serverless)
_TOKEN_REGISTRY: dict[str, Path] = {}

# 20 MB upload limit
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "invoice-pdf-editor"}


@router.post("/process")
async def process_invoice(
    file: UploadFile = File(..., description="Invoice PDF"),
    prompt: str = Form(..., description="Natural language editing prompt"),
    mode: Literal["deterministic", "ai"] = Form("deterministic", description="Processing mode"),
) -> JSONResponse:
    """
    Upload a PDF invoice and a natural-language prompt.
    Returns a JSON result with a download_token if successful.
    """
    # -- Validate upload ------------------------------------------------------
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum 20 MB.")

    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")

    # -- Save upload to temp file ---------------------------------------------
    with tempfile.NamedTemporaryFile(
        suffix=".pdf", delete=False, dir=tempfile.gettempdir()
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        # -- Run pipeline ------------------------------------------------------
        result, output_path = run_pipeline(
            pdf_path=tmp_path,
            prompt=prompt,
            output_dir=_OUTPUT_DIR,
            mode=mode,
        )

        if result.success and output_path:
            _TOKEN_REGISTRY[result.download_token] = output_path

        return JSONResponse(
            content={
                "success": result.success,
                "mode": mode,
                "download_token": result.download_token,
                "validation": result.validation.model_dump() if result.validation else None,
                "errors": result.errors,
                "log_summary": result.log_summary,
            },
            status_code=200 if result.success else 422,
        )

    finally:
        # Always clean up upload temp file
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@router.get("/download/{token}")
async def download_pdf(token: str) -> FileResponse:
    """
    Stream the edited PDF for a given download token.
    Deletes the file after sending (one-time download).
    """
    # Sanitize token
    if not token.isalnum() or len(token) != 32:
        raise HTTPException(status_code=400, detail="Invalid token.")

    output_path = _TOKEN_REGISTRY.get(token)
    if not output_path or not output_path.exists():
        raise HTTPException(status_code=404, detail="File not found or already downloaded.")

    # Remove from registry (one-time use)
    del _TOKEN_REGISTRY[token]

    return FileResponse(
        path=str(output_path),
        media_type="application/pdf",
        filename="invoice_edited.pdf",
        background=_cleanup_background(output_path),
    )


def _cleanup_background(path: Path):
    """Returns a background task that deletes the file after send."""
    from starlette.background import BackgroundTask

    def delete_file():
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    return BackgroundTask(delete_file)
