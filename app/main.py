"""
FastAPI application entrypoint for the commodity arbitrage multi-agent desk.
"""

from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

app = FastAPI(
    title="Rice Arbitrage Multi-Agent System",
    description="Enterprise physical commodity arbitrage desk with dynamic market grounding and autonomous negotiation.",
    version="2.1.0",
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include aggregated API routes
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the single-page testing dashboard."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard HTML not found.")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health_check():
    """Liveness probe for monitoring."""
    return {"status": "healthy", "service": "rice-arbitrage-agent"}
