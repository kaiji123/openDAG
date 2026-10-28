"""FastAPI app: JSON API + the single-page frontend in /static."""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ask, canonical, config, db, export, pipeline
from .fetch import FetchError

@asynccontextmanager
async def lifespan(_app):
    # On hosts with ephemeral disks, rebuild the DB from the dataset committed to git.
    n = export.import_dataset()
    if n:
        print(f"Imported {n} approved DAGs from data/opendag.jsonl")
    m = canonical.import_mappings()
    if m:
        print(f"Imported {m} variable-name mappings from data/variable_mappings.json")
    yield


app = FastAPI(title="OpenDAG", version=config.SCHEMA_VERSION, lifespan=lifespan)
STATIC = Path(__file__).resolve().parent.parent / "static"

JOBS: Dict[str, dict] = {}
WORKERS = threading.Semaphore(2)  # at most 2 papers processed at once


class Submit(BaseModel):
    identifier: str


class Question(BaseModel):
    question: str
    include_drafts: bool = False


class Review(BaseModel):
    nodes: List[dict]
    edges: List[dict]
    suggestion_status: Dict[str, str] = {}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def check_limits(request: Request) -> None:
    if config.ADMIN_TOKEN and request.headers.get("x-admin-token") == config.ADMIN_TOKEN:
        return
    now = time.time()
    if db.count_runs(now - 86400) >= config.DAILY_CAP:
        raise HTTPException(429, "OpenDAG has hit its daily processing cap. Please come back tomorrow, "
                                 "or browse the dataset meanwhile.")
    if db.count_runs(now - 3600, client_ip(request)) >= config.RATE_LIMIT_PER_HOUR:
        raise HTTPException(429, f"Limit reached: {config.RATE_LIMIT_PER_HOUR} new papers per hour. "
                                 "Already-processed papers are still free to view.")


def _worker(job_id: str, identifier: str) -> None:
    job = JOBS[job_id]
    with WORKERS:
        try:
            rec = pipeline.run(identifier, progress=lambda s: job.update(step=s))
            job.update(status="done", step="done", record_id=rec["id"])
        except FetchError as e:
            job.update(status="error", error=str(e))
        except Exception as e:  # surface a readable message, keep the server alive
            job.update(status="error", error=f"Pipeline failed: {e}")


@app.get("/api/config")
def get_config():
    return {
        "model_fast": config.MODEL_FAST,
        "model_reason": config.MODEL_REASON,
        "rate_limit_per_hour": config.RATE_LIMIT_PER_HOUR,
    }


@app.post("/api/submit")
def submit(body: Submit, request: Request):
    try:
        cached = pipeline.lookup_cached(body.identifier)
    except FetchError as e:
        raise HTTPException(400, str(e))
    if cached:
        return {"record_id": cached["id"], "cached": True}
    check_limits(request)
    db.log_run(client_ip(request))
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "step": "queued", "record_id": None, "error": None}
    threading.Thread(target=_worker, args=(job_id, body.identifier), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return job


def _record(rec_id: str) -> dict:
    rec = db.get_record(rec_id)
    if not rec:
        raise HTTPException(404, "Record not found")
    return rec


@app.get("/api/records/{rec_id}")
def get_record(rec_id: str):
    return _record(rec_id)


@app.put("/api/records/{rec_id}")
def review(rec_id: str, body: Review):
    rec = _record(rec_id)
    if rec["status"] == "approved":
        raise HTTPException(409, "Approved records are locked.")
    bad = {v for v in body.suggestion_status.values()} - {"accepted", "rejected", "suggested"}
    if bad:
        raise HTTPException(400, f"Invalid status: {bad}")
    try:
        return pipeline.update(rec, body.nodes, body.edges, body.suggestion_status)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/records/{rec_id}/approve")
def approve(rec_id: str):
    rec = _record(rec_id)
    if rec["status"] == "approved":
        return rec
    try:
        return pipeline.approve(rec)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/records/{rec_id}/dagitty", response_class=PlainTextResponse)
def dagitty(rec_id: str):
    return _record(rec_id)["dagitty"]


@app.get("/api/dataset")
def dataset(include_drafts: bool = False):
    recs = db.list_records(None if include_drafts else "approved")
    return [export.summary(r) for r in recs]


@app.get("/api/dataset/insights")
def dataset_insights(include_drafts: bool = False):
    return export.insights(db.list_records(None if include_drafts else "approved"))


@app.get("/api/dataset/network")
def dataset_network(include_drafts: bool = False):
    return export.network(db.list_records(None if include_drafts else "approved"))


@app.post("/api/ask")
def ask_dataset(body: Question, request: Request):
    q = body.question.strip()
    if len(q) < 3:
        raise HTTPException(400, "Please type a question.")
    if len(q) > 300:
        raise HTTPException(400, "Please keep questions under 300 characters.")
    records = db.list_records(None if body.include_drafts else "approved")
    if not records:
        raise HTTPException(400, "There are no approved papers to ask about yet. Tick “show drafts” to include drafts.")
    ip = client_ip(request)
    is_admin = config.ADMIN_TOKEN and request.headers.get("x-admin-token") == config.ADMIN_TOKEN
    now = time.time()
    if not is_admin:
        if db.count_runs(now - 86400, kind="ask") >= config.ASK_DAILY_CAP:
            raise HTTPException(429, "The question limit for today has been reached. Please try again tomorrow.")
        if db.count_runs(now - 3600, ip, kind="ask") >= config.ASK_PER_HOUR:
            raise HTTPException(429, f"Limit reached: {config.ASK_PER_HOUR} questions per hour.")
    try:
        result, cached = ask.answer(q, records)
    except Exception as e:
        raise HTTPException(502, f"The model couldn't answer right now ({e}). Please try again.")
    if not cached:
        db.log_run(ip, kind="ask")
    result.pop("usage", None)
    return result


@app.get("/api/dataset.jsonl")
def dataset_jsonl():
    lines = [json.dumps(export.public_record(r)) for r in db.list_records("approved")]
    return Response("\n".join(lines) + "\n", media_type="application/x-ndjson",
                    headers={"Content-Disposition": "attachment; filename=opendag.jsonl"})


@app.get("/api/dataset.dagitty", response_class=PlainTextResponse)
def dataset_dagitty():
    parts = []
    for r in db.list_records("approved"):
        parts.append(f"// {r['id']}: {r['paper'].get('title','')} (doi:{r['paper'].get('doi')})\n{r['dagitty']}")
    return PlainTextResponse("\n\n".join(parts) + "\n",
                             headers={"Content-Disposition": "attachment; filename=opendag.dagitty.txt"})


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
