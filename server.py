"""Web server for the Website Lead Finder agent.

Serves the dashboard UI and a JSON API on top of the lead pipeline.
Run:  uv run python server.py   →  http://localhost:8080
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.tools import (
    analyze_lead_pipeline,
    chat_with_ai,
    delete_lead,
    get_all_leads,
    get_lead,
    get_pitch_summary,
    get_service_types,
    get_llm_provider,
    parse_search_query,
    quick_enrich,
    search_businesses,
)

ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_env()

app = FastAPI(title="Website Lead Finder")


class SearchRequest(BaseModel):
    query: str
    max_results: int = 20


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/search")
def api_search(req: SearchRequest) -> dict:
    parsed = parse_search_query(req.query)
    if not parsed or not parsed.get("business_type"):
        raise HTTPException(status_code=400, detail="Empty search")
    if not parsed.get("location"):
        raise HTTPException(
            status_code=400,
            detail="Include a city or area — e.g. \u201cdental clinic in Wardha\u201d",
        )
    result = search_businesses(
        parsed["business_type"], parsed["location"], req.max_results
    )
    result["parsed"] = {
        "business_type": parsed["business_type"],
        "location": parsed["location"],
        "via": parsed.get("via"),
    }
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.get("/api/config")
def api_config() -> dict:
    provider = get_llm_provider()
    model = (
        os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
        if provider == "gemini"
        else os.environ.get("OLLAMA_MODEL", "gemma4")
    )
    return {"provider": provider, "model": model}


@app.get("/api/leads")
def api_leads(sort: str = "score", service: str | None = None, batch: str | None = None) -> dict:
    return {"leads": get_all_leads(sort=sort, service=service, batch=batch)}


@app.get("/api/services")
def api_services() -> dict:
    return {"services": get_service_types()}


@app.get("/api/suggestions")
def api_suggestions(min_score: int = 45, limit: int = 8) -> dict:
    return get_pitch_summary(min_score=min_score, limit=limit)


@app.get("/api/leads/{lead_id}")
def api_lead_detail(lead_id: int) -> dict:
    lead = get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"lead": lead}


@app.delete("/api/leads/{lead_id}")
def api_lead_delete(lead_id: int) -> dict:
    if not delete_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"status": "ok"}


@app.post("/api/leads/{lead_id}/enrich")
def api_lead_enrich(lead_id: int) -> dict:
    """Fast contact enrichment (directories, phones, emails, related sites)."""
    if get_lead(lead_id) is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        return quick_enrich(lead_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Enrichment failed: {exc}") from exc


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/chat")
def api_chat(req: ChatRequest) -> dict:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    try:
        return chat_with_ai(req.message.strip(), req.history)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Chat failed: {exc}") from exc


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
