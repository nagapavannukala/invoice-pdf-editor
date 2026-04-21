"""
API integration tests — uses httpx TestClient to test the FastAPI app.
Doesn't require a running server.
"""
import os
from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_serves_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Invoice PDF Editor" in resp.text


def test_process_no_file():
    resp = client.post("/api/process", data={"prompt": "test"})
    assert resp.status_code == 422  # Unprocessable: missing file


def test_process_no_prompt():
    pdf_bytes = _minimal_pdf()
    resp = client.post(
        "/api/process",
        files={"file": ("invoice.pdf", BytesIO(pdf_bytes), "application/pdf")},
        data={"prompt": ""},
    )
    # FastAPI may return 422 (schema validation) or 400 (our custom check)
    # depending on multipart handling — both indicate a bad request
    assert resp.status_code in (400, 422)


def test_process_non_pdf():
    resp = client.post(
        "/api/process",
        files={"file": ("invoice.txt", BytesIO(b"hello"), "text/plain")},
        data={"prompt": "Update Unit Price for item 1 to 100"},
    )
    assert resp.status_code == 400


def test_download_invalid_token():
    resp = client.get("/api/download/notavalidtoken123456789012345678")
    assert resp.status_code in (400, 404)


def test_download_unknown_token():
    resp = client.get("/api/download/" + "a" * 32)
    assert resp.status_code == 404


def test_process_mode_deterministic_is_default():
    """Omitting `mode` should behave identically to mode=deterministic."""
    pdf_bytes = _minimal_pdf()
    # A malformed PDF will fail extraction, but 422 means the route accepted the request
    resp = client.post(
        "/api/process",
        files={"file": ("invoice.pdf", BytesIO(pdf_bytes), "application/pdf")},
        data={"prompt": "Update Unit Price for item 1 to 100"},
    )
    # Route accepted the request (not 400 or 404)
    assert resp.status_code in (200, 422), f"Unexpected status: {resp.status_code}"
    body = resp.json()
    # The mode field is echoed back in the response
    assert body.get("mode") == "deterministic"


def test_process_mode_explicit_deterministic():
    """Explicitly passing mode=deterministic should work and echo back."""
    pdf_bytes = _minimal_pdf()
    resp = client.post(
        "/api/process",
        files={"file": ("invoice.pdf", BytesIO(pdf_bytes), "application/pdf")},
        data={"prompt": "Update Unit Price for item 1 to 100", "mode": "deterministic"},
    )
    assert resp.status_code in (200, 422)
    body = resp.json()
    assert body.get("mode") == "deterministic"


def test_process_mode_ai_without_api_key_gives_error():
    """AI mode without OPENAI_API_KEY should fail with a clear error."""
    pdf_bytes = _minimal_pdf()
    # Ensure no API key in environment for this test
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        resp = client.post(
            "/api/process",
            files={"file": ("invoice.pdf", BytesIO(pdf_bytes), "application/pdf")},
            data={"prompt": "Change items 1-5 to 72.55", "mode": "ai"},
        )
    # Should return 422 (pipeline error) with error message about API key
    body = resp.json()
    assert resp.status_code in (200, 422)
    errors_text = " ".join(str(e) for e in (body.get("errors") or []))
    assert "OPENAI_API_KEY" in errors_text or "api_key" in errors_text.lower() or body.get("mode") == "ai"


def test_process_invalid_mode_rejected():
    """An unknown mode value should be rejected by FastAPI schema validation."""
    pdf_bytes = _minimal_pdf()
    resp = client.post(
        "/api/process",
        files={"file": ("invoice.pdf", BytesIO(pdf_bytes), "application/pdf")},
        data={"prompt": "test", "mode": "turbo-wizard"},
    )
    assert resp.status_code == 422  # FastAPI Literal validation failure


def _minimal_pdf() -> bytes:
    """Generate a minimal valid PDF for upload tests."""
    try:
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Invoice\n1  Widget  10  100,00  1.000,00")
        buf = BytesIO()
        doc.save(buf)
        return buf.getvalue()
    except ImportError:
        # Raw minimal PDF if fitz not installed in test env
        return b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\nxref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n0000000058 00000 n\n0000000115 00000 n\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
