"""Runs the full pipeline for one paper and handles review/approval."""
from __future__ import annotations

import time
import uuid
from typing import Callable, List, Optional

from . import budget, canonical, config, db, fetch, graph, verify
from .critique import critique
from .evidence import attach_evidence
from .extract import extract
from .schemas import Critique, Edge, Node

Progress = Callable[[str], None]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def lookup_cached(identifier: str) -> Optional[dict]:
    """Return an existing record for this paper, without any model calls.

    Also rejects non-open-access papers up front, before any job or rate-limit slot is used.
    """
    meta = fetch.check_open_access(fetch.resolve(identifier))
    return db.find_by_pmcid(meta.pmcid)


def run(identifier: str, progress: Progress = lambda step: None) -> dict:
    progress("fetching")
    meta, sections = fetch.fetch_paper(identifier)
    existing = db.find_by_pmcid(meta.pmcid)
    if existing:
        return existing

    paper_text = fetch.build_prompt_text(sections)  # used in memory only, never stored

    progress("extracting")
    ex, usage_ex = extract(paper_text)
    nodes, edges, issues = graph.validate(ex.nodes, ex.edges)
    ex.nodes, ex.edges = nodes, edges
    ex = graph.ensure_core_edges(ex)
    nodes, edges, issues2 = graph.validate(ex.nodes, ex.edges)
    issues += issues2

    progress("critiquing")
    abstract = sections.get("abstract", "") or paper_text[:3000]
    try:
        ex.nodes, ex.edges = nodes, edges
        crit, usage_cr = critique(ex, abstract)
    except Exception as e:  # keep the extraction even if the critique fails
        crit, usage_cr = Critique(overall_assessment=f"Critique unavailable: {e}"), {}
        issues.append("Critique step failed; you can still review the extracted DAG.")

    # Check each suggestion against the paper itself: extraction can drop items from long covariate lists.
    usage_vf = {}
    try:
        crit, found, usage_vf = verify.verify(crit, paper_text)
        if found:
            nodes, edges = verify.add_confirmed(nodes, edges, found)
            crit.overall_assessment = verify.fix_assessment(crit.overall_assessment, found)
            issues.append("Verified against the paper text, already adjusted for: "
                          + ", ".join(f["label"] for f in found) + " (added to the DAG, removed from AI suggestions).")
    except budget.BudgetExceeded:
        raise
    except Exception as e:  # verification is a safety net; never lose the paper over it
        issues.append(f"Suggestion check against the paper text was skipped ({e}).")

    progress("evidence")
    crit = attach_evidence(crit, ex.exposure.name, ex.outcome.name, meta.model_dump())

    all_nodes, all_edges = graph.merge_suggestions(nodes, edges, crit)
    all_nodes, all_edges, issues3 = graph.validate(all_nodes, all_edges)
    issues += issues3

    rec = {
        "id": uuid.uuid4().hex[:10],
        "schema_version": config.SCHEMA_VERSION,
        "status": "draft",
        "created_at": _now(),
        "approved_at": None,
        "paper": meta.model_dump(),
        "study": {
            "study_design": ex.study_design,
            "population": ex.population,
            "sample_size": ex.sample_size,
            "follow_up": ex.follow_up,
            "exposure": ex.exposure.model_dump(),
            "outcome": ex.outcome.model_dump(),
            "reported_effect": ex.reported_effect.model_dump() if ex.reported_effect else None,
        },
        "dag": {
            "nodes": [n.model_dump() for n in all_nodes],
            "edges": [e.model_dump() for e in all_edges],
        },
        "critique": crit.model_dump(),
        "validation_issues": issues,
        "models": {
            "extraction": config.MODEL_FAST,
            "critique": config.MODEL_REASON,
            "evidence_search": "tavily",
            "provider": "Nebius Token Factory",
        },
        "usage": {"extraction": usage_ex, "critique": usage_cr, "verification": usage_vf},
    }
    _refresh_derived(rec)
    progress("done")
    db.save_record(rec)
    return rec


def _refresh_derived(rec: dict) -> None:
    nodes = [Node(**n) for n in rec["dag"]["nodes"]]
    edges = [Edge(**e) for e in rec["dag"]["edges"]]
    rec["dagitty"] = graph.to_dagitty(nodes, edges)
    rec["open_backdoors"] = graph.open_backdoors(nodes, edges)


def update(rec: dict, nodes: List[dict], edges: List[dict], suggestion_status: dict) -> dict:
    """Apply reviewer edits. suggestion_status maps confounder label -> accepted|rejected|suggested."""
    crit = Critique(**rec["critique"])
    for mc in crit.missing_confounders:
        if mc.label in suggestion_status:
            mc.status = suggestion_status[mc.label]
    rejected = {graph.slug(mc.label) for mc in crit.missing_confounders if mc.status == "rejected"}

    n_objs = [Node(**n) for n in nodes if not (n.get("ai_suggested") and graph.slug(n.get("id", "")) in rejected)]
    e_objs = [Edge(**e) for e in edges]
    n_objs, e_objs, issues = graph.validate(n_objs, e_objs)

    rec["critique"] = crit.model_dump()
    rec["dag"] = {"nodes": [n.model_dump() for n in n_objs], "edges": [e.model_dump() for e in e_objs]}
    rec["validation_issues"] = issues
    _refresh_derived(rec)
    db.save_record(rec)
    return rec


def approve(rec: dict, reviewed_by: str = "human") -> dict:
    """reviewed_by is "human" (approved in the UI) or "ai" (scripts/autoreview.py, Nemotron Super)."""
    crit = Critique(**rec["critique"])
    pending = [mc.label for mc in crit.missing_confounders if mc.status == "suggested"]
    if pending:
        raise ValueError("Accept or reject every AI-suggested confounder before approving: " + ", ".join(pending))
    rec["status"] = "approved"
    rec["approved_at"] = _now()
    rec["review"] = {"by": reviewed_by, "model": config.MODEL_REASON if reviewed_by == "ai" else None}
    _refresh_derived(rec)
    db.save_record(rec)
    # Link the new paper into the dataset network: Nemotron Nano maps its variable names to shared names.
    canonical.resolve_record(rec)
    return rec
