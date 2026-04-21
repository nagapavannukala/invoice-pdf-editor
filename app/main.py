"""
FastAPI Application Entry Point — Invoice PDF Editor

Serves:
  - Static frontend at /
  - API endpoints at /api/*

Run with:
  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

from pathlib import Path

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
