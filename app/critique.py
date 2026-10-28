"""Step 3: critique the DAG with a larger Nemotron model (MODEL_REASON).

Everything returned here is AI-suggested and kept separate from what the paper reported.
"""
from __future__ import annotations

import json
from typing import Tuple

from . import config
from .llm import chat_json
from .schemas import Critique, Extraction

SYSTEM = """You are a senior epidemiologist reviewing the causal assumptions of a published study.
You think carefully about confounding, selection and measurement bias, and you are specific and concise.
Reply with a single JSON object and nothing else."""

INSTRUCTIONS = """Below is a causal DAG extracted from a study, plus the study abstract.
Identify what the authors likely did NOT adjust for and other threats to the causal claim.

Return JSON:
{
  "missing_confounders": [
    {"label": "Short variable name, max 4 words, no parentheses",
     "rationale": "1-2 sentences: why it plausibly causes both exposure and outcome",
     "edges_to_add": [{"source": "<label or existing node id>", "target": "<existing node id>"}]}
  ],
  "bias_risks": [
    {"type": "e.g. confounding by indication | healthy user effect | selection bias | reverse causation | immortal time bias | measurement error | loss to follow-up",
     "explanation": "1-2 sentences specific to this study",
     "severity": "low" | "medium" | "high"}
  ],
  "overall_assessment": "2-3 plain-English sentences on how much to trust the causal claim"
}

Rules:
- Suggest at most 5 missing confounders, most important first. Do not repeat variables already in the DAG (even under a different name).
- In edges_to_add, use the new confounder's label as source and existing node ids ("%s" exposure, "%s" outcome) as targets.
- At most 5 bias risks. Be concrete; avoid generic boilerplate.

DAG:
%s

ABSTRACT:
%s
"""


def critique(ex: Extraction, abstract: str) -> Tuple[Critique, dict]:
    exp = next((n.id for n in ex.nodes if n.role == "exposure"), "exposure")
    out = next((n.id for n in ex.nodes if n.role == "outcome"), "outcome")
    dag = {
        "study_design": ex.study_design,
        "population": ex.population,
        "exposure": ex.exposure.model_dump(),
        "outcome": ex.outcome.model_dump(),
        "nodes": [{"id": n.id, "label": n.label, "role": n.role, "adjusted": n.adjusted} for n in ex.nodes],
        "edges": [{"source": e.source, "target": e.target} for e in ex.edges],
        "reported_effect": ex.reported_effect.model_dump() if ex.reported_effect else None,
    }
    prompt = INSTRUCTIONS % (exp, out, json.dumps(dag, indent=1), abstract[:3000])
    data, usage = chat_json(
        config.MODEL_REASON, SYSTEM, prompt,
        max_tokens=config.MAX_TOKENS_CRITIQUE, thinking=True,
    )
    data["missing_confounders"] = (data.get("missing_confounders") or [])[:5]
    data["bias_risks"] = (data.get("bias_risks") or [])[:5]
    data.pop("label", None)
    return Critique.model_validate(data), usage
