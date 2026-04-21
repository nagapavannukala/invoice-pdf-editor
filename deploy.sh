#!/usr/bin/env bash
# ============================================================
# deploy.sh — one-time script to push invoice-pdf-editor
#             to GitHub.com and prepare it for Render deploy
# Run from:  /Users/n0n03dq/CascadeProjects/invoice-pdf-editor
# ============================================================
set -e

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Invoice PDF Editor — GitHub.com push script   ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Log in to GitHub.com (browser pop-up, 30 seconds) ──
echo "▶ Step 1/3 — GitHub.com login (browser will open)..."
gh auth login --hostname github.com --web

echo ""
echo "▶ Step 2/3 — Creating public repo + pushing all commits..."
gh repo create invoice-pdf-editor \
  --public \
  --description "Edit invoice PDFs with natural language prompts" \
  --source=. \
  --remote=origin \
  --push \
  --hostname github.com

echo ""
GH_USER=$(gh api user --hostname github.com --jq .login)
REPO_URL="https://github.com/${GH_USER}/invoice-pdf-editor"
RENDER_BLUEPRINT="https://render.com/deploy?repo=${REPO_URL}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅  Code pushed to GitHub!                                  ║"
echo "║                                                              ║"
echo "║  Repo : ${REPO_URL}"
echo "║                                                              ║"
echo "║  ▶ Step 3/3 — Now open this URL in your browser:            ║"
echo "║                                                              ║"
echo "║  ${RENDER_BLUEPRINT}"
echo "║                                                              ║"
echo "║  On Render: click  Apply  →  wait ~3 min  → get your URL    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Auto-open the Render deploy page
open "${RENDER_BLUEPRINT}" 2>/dev/null || true

echo "▶ Step 3/3 — Opening Render deploy page in browser..."
echo "   Your site will be live at:"
echo "   https://invoice-pdf-editor.onrender.com"
echo "   (exact URL shown in Render dashboard after deploy)"
echo ""
