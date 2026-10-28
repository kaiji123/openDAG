"""Step 2: extract the paper's causal structure with Nemotron Nano (MODEL_FAST)."""
from __future__ import annotations

from typing import Tuple

from . import config
from .llm import chat_json
from .schemas import Extraction

SYSTEM = """You are an epidemiologist who turns observational and clinical studies into causal DAGs.
Read the study text and report ONLY what the authors themselves describe. Do not add variables they did not mention.
Reply with a single JSON object and nothing else."""

INSTRUCTIONS = """Extract the causal structure of the MAIN analysis in this study.

Return JSON with exactly these keys:
{
  "study_design": "cohort" | "case-control" | "cross-sectional" | "RCT" | "other",
  "population": "one sentence: who, where, when",
  "sample_size": "number analysed in the main analysis, as written in the text, e.g. '<total> (<n> exposed)'; null if not stated",
  "follow_up": "length of follow-up as written in the text; null if not stated",
  "exposure": {"name": "...", "definition": "how it was measured, one sentence"},
  "outcome": {"name": "...", "definition": "how it was measured, one sentence"},
  "nodes": [
    {"id": "short_snake_case_id", "label": "Human readable name",
     "role": "exposure" | "outcome" | "confounder" | "mediator" | "collider" | "effect_modifier" | "other",
     "adjusted": true/false,
     "snippet": "a phrase of at most 20 words copied from the text that supports this node"}
  ],
  "edges": [{"source": "node_id", "target": "node_id"}],
  "reported_effect": {"measure": "HR|OR|RR|IRR|RD|beta|other", "value": "1.23", "ci": "1.01-1.50", "adjusted": true/false} or null
}

Rules:
- Exactly one node with role "exposure" and one with role "outcome"; include an edge exposure -> outcome.
- Every covariate the authors adjusted for, matched on or stratified by is a node with "adjusted": true. Use role "confounder" unless the paper says otherwise.
- For each confounder add edges confounder -> exposure and confounder -> outcome.
- Mediators: exposure -> mediator -> outcome. Mark adjusted only if the paper adjusted for them.
- Group long lists sensibly (e.g. "Comorbidities (diabetes, hypertension, ...)") if there are more than 15 covariates.
- reported_effect is the main adjusted estimate for exposure -> outcome, if reported.
- Snippets must be short (max 20 words). Never copy long passages.
- Only report numbers that appear in the study text. Never invent or estimate them.
- The graph must be acyclic.

STUDY TEXT:
"""


def extract(paper_text: str) -> Tuple[Extraction, dict]:
    data, usage = chat_json(
        config.MODEL_FAST,
        SYSTEM,
        INSTRUCTIONS + paper_text,
        max_tokens=config.MAX_TOKENS_EXTRACT,
        thinking=False,
    )
    data.setdefault("nodes", [])
    data.setdefault("edges", [])
    data.setdefault("exposure", {"name": "unknown"})
    data.setdefault("outcome", {"name": "unknown"})
    if data.get("reported_effect") in ({}, "", "null"):
        data["reported_effect"] = None
    return Extraction.model_validate(data), usage
