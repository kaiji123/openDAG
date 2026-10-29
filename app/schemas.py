"""Pydantic models for everything the pipeline produces."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Role = Literal[
    "exposure", "outcome", "confounder", "mediator",
    "collider", "effect_modifier", "other",
]
Design = Literal["cohort", "case-control", "cross-sectional", "RCT", "other"]
Severity = Literal["low", "medium", "high"]


def short_snippet(text: Optional[str], max_words: int = 20) -> str:
    """Keep supporting snippets short (copyright: never store long quotes)."""
    if not text:
        return ""
    words = str(text).split()
    return " ".join(words[:max_words]) + (" …" if len(words) > max_words else "")


class Variable(BaseModel):
    name: str
    definition: str = ""


class Node(BaseModel):
    id: str
    label: str
    role: Role = "other"
    adjusted: bool = False
    snippet: str = ""
    ai_suggested: bool = False  # True for confounders proposed by the critique step

    @field_validator("snippet", mode="before")
    @classmethod
    def _short(cls, v):
        return short_snippet(v)

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v):
        v = str(v or "other").strip().lower().replace(" ", "_").replace("-", "_")
        allowed = {"exposure", "outcome", "confounder", "mediator", "collider", "effect_modifier"}
        return v if v in allowed else "other"


class Edge(BaseModel):
    source: str
    target: str
    ai_suggested: bool = False


class ReportedEffect(BaseModel):
    measure: str = ""
    value: Optional[str] = None
    ci: Optional[str] = None
    adjusted: bool = False

    @field_validator("value", "ci", mode="before")
    @classmethod
    def _str(cls, v):
        return None if v is None else str(v)


class Extraction(BaseModel):
    @field_validator("sample_size", "follow_up", mode="before")
    @classmethod
    def _opt_str(cls, v):
        return None if v in (None, "", "null", "unknown") else str(v)[:120]

    study_design: Design = "other"
    population: str = ""
    sample_size: Optional[str] = None   # e.g. "12,345 (6,000 exposed)"
    follow_up: Optional[str] = None     # e.g. "median 5.2 years"
    exposure: Variable
    outcome: Variable
    nodes: List[Node]
    edges: List[Edge]
    reported_effect: Optional[ReportedEffect] = None

    @field_validator("study_design", mode="before")
    @classmethod
    def _design(cls, v):
        s = str(v or "").strip().lower()
        if "case" in s and "control" in s:
            return "case-control"
        if "cross" in s:
            return "cross-sectional"
        if "random" in s or s == "rct" or "trial" in s:
            return "RCT"
        if "cohort" in s or "longitudinal" in s or "prospective" in s or "retrospective" in s:
            return "cohort"
        return "other"


class MissingConfounder(BaseModel):
    label: str
    rationale: str = ""
    edges_to_add: List[Edge] = Field(default_factory=list)
    evidence: List[dict] = Field(default_factory=list)  # [{title, url}] from Tavily
    status: Literal["suggested", "accepted", "rejected"] = "suggested"
    review_note: str = ""  # reason given by the AI reviewer, when the record was AI-reviewed


class BiasRisk(BaseModel):
    type: str
    explanation: str = ""
    severity: Severity = "medium"

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        v = str(v or "medium").lower().strip()
        return v if v in ("low", "medium", "high") else "medium"


class Critique(BaseModel):
    missing_confounders: List[MissingConfounder] = Field(default_factory=list)
    bias_risks: List[BiasRisk] = Field(default_factory=list)
    overall_assessment: str = ""
    label: str = "AI-suggested"  # always marked as separate from what the paper reported


class PaperMeta(BaseModel):
    title: str = ""
    authors: str = ""
    year: Optional[str] = None
    journal: str = ""
    doi: Optional[str] = None
    pmcid: Optional[str] = None
    pmid: Optional[str] = None
    license: Optional[str] = None
