# 🧾 Invoice PDF Editor

> Edit structured invoice PDFs using natural language prompts.  
> Non-technical users upload → describe edits → download — no coding required.

---

## Architecture

```
invoice-pdf-editor/
├── app/
│   ├── main.py               # FastAPI app entry point
│   ├── pipeline.py           # Orchestrates the full processing flow
│   ├── models.py             # Pydantic data models (single source of truth)
│   ├── api/
│   │   └── routes.py         # POST /api/process, GET /api/download/{token}
│   ├── parser/
│   │   └── prompt_parser.py  # Natural language → ParsedInstructions
│   ├── calculator/
│   │   ├── invoice_calc.py   # Deterministic calculation engine
│   │   └── number_fmt.py     # European number formatting (1.234,56)
│   ├── pdf_engine/
│   │   ├── extractor.py      # pdfplumber: text + coordinate extraction
│   │   └── editor.py         # PyMuPDF: precise text replacement
│   ├── validators/
│   │   └── validator.py      # Fail-fast validation layer
│   └── templates/
│       └── index.html        # Single-page HTMX + Tailwind frontend
└── tests/
    ├── test_parser.py
    ├── test_number_fmt.py
    ├── test_calculator.py
    └── test_api.py
```

---

## Processing Flow

```
Upload PDF + Prompt
        │
        ▼
1. Extract  ─── pdfplumber ──► ExtractedInvoice (items + aggregates + bboxes)
        │
        ▼
2. Parse    ─── prompt_parser ──► ParsedInstructions (deterministic)
        │
        ▼
3. Calculate ── invoice_calc ──► Updated ExtractedInvoice
        │
        ▼
4. Validate ─── validator ────► ValidationResult (pass/fail + checks)
        │
        ▼
5. Edit PDF ─── PyMuPDF ──────► Output PDF (coordinate-precise replacement)
        │
        ▼
6. Download ─── FileResponse ──► invoice_edited.pdf
```

---

## Prompt Format

The prompt is the single source of truth. Supported instructions:

```
Update Unit Price for items 1-5 to EUR 150,00
Recalculate Amount = Quantity × Unit Price
Recalculate Ex Works
Update Freight to EUR 2.500,00
Recalculate Total Up To
```

**Number format**: European (dot = thousands, comma = decimal): `1.234,56`  
**Item ranges**: `items 1-5`, `item 3`, `items 1 to 5`

---

## Domain Logic

| Step | Formula |
|---|---|
| Item Amount | `Quantity × Unit Price` |
| Ex Works | `Σ item amounts` |
| Total Up To | `Ex Works + Freight + Insurance` |
| Freight / Insurance | Preserved from original unless prompt specifies |

---

## Running Locally

```bash
cd invoice-pdf-editor

# Create virtualenv
uv venv
source .venv/bin/activate  # Mac/Linux

# Install dependencies
uv pip install -e ".[dev]" \
  --index-url https://pypi.ci.artifacts.walmart.com/artifactory/api/pypi/external-pypi/simple \
  --allow-insecure-host pypi.ci.artifacts.walmart.com

# Run server
uvicorn app.main:app --reload --port 8000

# Open browser
open http://localhost:8000
```

---

## Running Tests

```bash
pytest -v
```

---

## API Reference

### `POST /api/process`

| Field | Type | Description |
|---|---|---|
| `file` | `multipart/form-data` | Invoice PDF (≤20 MB) |
| `prompt` | `string` | Natural language editing instructions |

**Response (success)**:
```json
{
  "success": true,
  "download_token": "abc123...",
  "validation": { "passed": true, "checks": {...}, "errors": [], "warnings": [] },
  "errors": [],
  "log_summary": ["📄 Step 1: Extracting...", "..."]
}
```

### `GET /api/download/{token}`

Returns the edited PDF as a binary download. One-time use — file is deleted after download.

---

## Security

- Uploaded files are stored in OS temp directory only during processing
- Output files are deleted immediately after download
- No user data is persisted to disk beyond the request lifecycle
- Token-gated download (random 32-char hex, one-time use)
- 20 MB upload limit enforced server-side

---

## Extensibility

| Module | To extend |
|---|---|
| `prompt_parser.py` | Add new field aliases / instruction patterns |
| `invoice_calc.py` | Add new calculation rules |
| `extractor.py` | Support different invoice table layouts |
| `editor.py` | Support multi-page edits, row insertions |
| `validator.py` | Add new business-rule checks |
