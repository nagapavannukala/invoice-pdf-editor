"""
FastAPI Application Entry Point — Invoice PDF Editor

Serves:
  - Static frontend at /
  - API endpoints at /api/*

Run with:
  uvicorn app.main:app --reload --port 8000

Secrets:
  Locally  → copy .env.example → .env, fill in your key. Never commit .env.
  Render   → set OPENAI_API_KEY in Dashboard → Environment (sync:false).
             Real env vars always win over .env (load_dotenv doesn't override).
"""
from __future__ import annotations

from pathlib import Path

# Load .env file for local development.
# On Render the env var is already set, so this is a no-op (override=False).
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)   # never clobbers real environment variables
except ImportError:
    pass  # python-dotenv not installed — fine in prod if env vars are set directly

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router

_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(
    title="Invoice PDF Editor",
    description="Edit structured invoice PDFs using natural language prompts.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — open for serverless / cross-origin deployments
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# API routes
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    """Serve the single-page frontend."""
    html_path = _TEMPLATES_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
