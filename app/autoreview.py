"""AI review of draft records with Nemotron Super: accept or reject each AI-suggested confounder.

This stands in for a human reviewer when there isn't time to review every draft by hand. Records
approved this way are labelled review.by = "ai" everywhere (UI, exports), and a person can re-review
them later, which relabels them as human-reviewed.
"""
from __future__ import annotations

import json
from typing import Dict

from . import config, pipeline
from .llm import chat_json
from .schemas import Critique

SYSTEM = """You are a senior epidemiologist reviewing AI-suggested missing confounders for a published study's causal DAG.
Be rigorous and sceptical. Reply with a single JSON object and nothing else."""

INSTRUCTIONS = """The study's DAG below lists the variables the authors adjusted for. Another model suggested
confounders the authors may have missed. Decide for EACH suggestion whether to accept or reject it.

Accept only if ALL hold:
- it plausibly causes BOTH the exposure and the outcome (a common cause), given the study population and design;
- it is not already adjusted for under another name or through a close proxy in the "adjusted" list;
- it is not caused by the exposure (that would make it a mediator or collider, and adjusting for it would add bias).

Otherwise reject. Give a one-sentence reason either way, specific to this study.

Return JSON: {"decisions": [{"label": "<suggestion label, copied exactly>", "decision": "accept" | "reject", "reason": "..."}]}

STUDY:
%s

SUGGESTIONS:
%s
"""


def review(rec: dict) -> Dict[str, dict]:
    """Ask Nemotron Super for a decision on every suggestion. Returns {label: {status, reason}}."""
    s = rec["study"]
    study = {
        "design": s.get("study_design"),
        "population": s.get("population"),
        "exposure": s.get("exposure"),
        "outcome": s.get("outcome"),
        "adjusted": [n["label"] for n in rec["dag"]["nodes"] if n.get("adjusted")],
        "other_variables": [f"{n['label']} ({n['role']})" for n in rec["dag"]["nodes"]
                            if not n.get("adjusted") and not n.get("ai_suggested") and n["role"] not in ("exposure", "outcome")],
    }
    suggestions = [{"label": m["label"], "rationale": m.get("rationale", ""),
                    "evidence": [e.get("title", "") for e in m.get("evidence", [])][:3]}
                   for m in rec["critique"]["missing_confounders"]]
    if not suggestions:
        return {}
    data, _ = chat_json(config.MODEL_REASON, SYSTEM,
                        INSTRUCTIONS % (json.dumps(study, indent=1), json.dumps(suggestions, indent=1)),
                        max_tokens=4000, thinking=True)
    wanted = {m["label"].strip().lower(): m["label"] for m in rec["critique"]["missing_confounders"]}
    out = {}
    for d in data.get("decisions", []):
        label = wanted.get(str(d.get("label", "")).strip().lower())
        if label and d.get("decision") in ("accept", "reject"):
            out[label] = {"status": "accepted" if d["decision"] == "accept" else "rejected",
                          "reason": str(d.get("reason", ""))[:300]}
    return out


def review_and_approve(rec: dict) -> dict:
    """Apply the AI decisions and approve the record as AI-reviewed. Raises if any suggestion is left undecided."""
    decisions = review(rec)
    missing = [m["label"] for m in rec["critique"]["missing_confounders"] if m["label"] not in decisions]
    if missing:
        raise ValueError(f"No decision returned for: {', '.join(missing)}")
    crit = Critique(**rec["critique"])
    for mc in crit.missing_confounders:
        mc.review_note = decisions[mc.label]["reason"]
    rec["critique"] = crit.model_dump()
    rec = pipeline.update(rec, rec["dag"]["nodes"], rec["dag"]["edges"],
                          {label: d["status"] for label, d in decisions.items()})
    return pipeline.approve(rec, reviewed_by="ai")
