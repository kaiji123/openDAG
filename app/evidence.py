"""Step 4: find supporting sources for each AI-suggested confounder with Tavily."""
from __future__ import annotations

import json
import re
from typing import List, Optional

import httpx

from . import config, db
from .schemas import Critique

TAVILY_URL = "https://api.tavily.com/search"


def search(query: str, max_results: int = 5) -> List[dict]:
    """Tavily search, cached in SQLite so repeated queries cost nothing."""
    cache_key = f"{max_results}|{query}"
    cached = db.cache_get("tavily", cache_key)
    if cached is not None:
        return json.loads(cached)
    if not config.TAVILY_API_KEY:
        return []
    r = httpx.post(
        TAVILY_URL,
        headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
        json={"query": query, "max_results": max_results, "search_depth": "basic", "topic": "general"},
        timeout=30,
    )
    r.raise_for_status()
    results = [
        {"title": x.get("title", "")[:200], "url": x.get("url", "")}
        for x in r.json().get("results", [])[:max_results]
        if x.get("url")
    ]
    db.cache_set("tavily", cache_key, json.dumps(results))
    return results


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def clean(results: List[dict], paper: Optional[dict] = None) -> List[dict]:
    """Drop duplicates and the source paper itself (a paper can't be evidence for its own critique)."""
    paper = paper or {}
    own_ids = [x.lower() for x in (paper.get("doi"), paper.get("pmcid"), paper.get("pmid")) if x]
    own_title = _norm(paper.get("title", ""))[:60]
    seen, out = set(), []
    for r in results:
        url, title = (r.get("url") or "").lower(), _norm(r.get("title", ""))
        same_title = len(title) >= 20 and len(own_title) >= 20 and (title[:60] in own_title or own_title in title)
        if any(i in url for i in own_ids) or same_title:
            continue
        key = title[:60] or url
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def evidence_for(label: str, exposure: str, outcome: str, paper: Optional[dict] = None) -> List[dict]:
    try:
        # ask for a couple of extra results so filtering still leaves up to 3
        return clean(search(f"{label} {exposure} {outcome} confounding", max_results=5), paper)[:3]
    except httpx.HTTPError:
        return []  # evidence is a bonus; never fail the pipeline on it


def attach_evidence(crit: Critique, exposure: str, outcome: str, paper: Optional[dict] = None) -> Critique:
    for mc in crit.missing_confounders:
        mc.evidence = evidence_for(mc.label, exposure, outcome, paper)
    return crit
