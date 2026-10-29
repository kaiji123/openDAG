"""Step 3b: check each AI-suggested confounder against the paper's own text (Nemotron Super).

Extraction can drop items from a long covariate list (often a table footnote). The critique then "finds" those
as missing, which is a false alarm. Here the model answers one narrow question per suggestion, did the authors
adjust for it, and must quote the sentence that shows it. The quote is checked in code: it must be in the paper,
next to an adjustment word, so the model can neither invent one nor cite a passing mention. Confirmed
suggestions move into the DAG as adjusted variables, with the quote.

Super, not Nano: on a real paper Nano missed "education" in a long footnote list and quoted bare variable names;
Super found all three and quoted the "Model 2 was adjusted for ..." sentence. It costs about $0.001 a paper.
"""
from __future__ import annotations

import json
import re
from typing import List, Tuple

from . import config, graph
from .llm import chat_json
from .schemas import Critique, Edge, Node, short_snippet

SYSTEM = """You check what a study's authors adjusted for, using only the study text provided.
Reply with a single JSON object and nothing else."""

INSTRUCTIONS = """For each CANDIDATE variable, decide whether the authors adjusted for it (or matched or stratified
on it) in their main analysis, according to the STUDY TEXT only.

- A direct measure counts: "household income" counts for "Income"; "years of education" counts for "Education".
- A different construct does not: "Townsend deprivation index" is NOT "Education"; "comorbidities" is NOT "Frailty".
- If the text does not clearly show it, answer false.
- When true, "quote" must be copied exactly from the STUDY TEXT (at most 25 words) and show the adjustment.

Return JSON: {"checks": [{"label": "<candidate, copied exactly>", "adjusted": true | false, "quote": "..."}]}

CANDIDATES:
%s

STUDY TEXT:
%s
"""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


ADJUST_WORDS = re.compile(r"adjust|control(?:led|ling) for|covariat|confound|matched|stratif|account(?:ed|ing) for", re.I)


def quote_in_text(quote: str, text: str) -> bool:
    """The quote must really be in the paper: compare on letters and digits only, allowing small edits at the ends."""
    return _find(quote, text) >= 0


def _find(quote: str, text: str) -> int:
    q, t = _norm(quote), _norm(text)
    if len(q) < 12:
        return -1
    i = t.find(q)
    if i >= 0:
        return i
    words = q.split()
    core = " ".join(words[1:-1]) if len(words) > 4 else q   # tolerate a clipped first or last word
    return t.find(core) if len(core) >= 12 else -1


def shows_adjustment(quote: str, text: str) -> bool:
    """The quote is in the paper, in a sentence that also says the variable was adjusted for.

    Same sentence, not merely nearby: "Renal function was measured at baseline" next to an adjustment sentence
    is a measurement, not an adjustment.
    """
    if _find(quote, text) < 0:
        return False
    q = _norm(quote)
    if ADJUST_WORDS.search(quote):   # the quote itself shows the adjustment
        return True
    words = q.split()
    core = " ".join(words[1:-1]) if len(words) > 4 else q
    for sent in re.split(r"(?<=[.;])\s+", text):
        n = _norm(sent)
        if (q in n or core in n) and ADJUST_WORDS.search(sent):
            return True
    return False


GENERIC = {"use", "level", "status", "history", "levels", "baseline", "other", "factor", "factors", "score", "total",
           "time", "with", "from", "and", "the", "for", "of", "in", "or", "index"}


def _terms(label: str) -> List[str]:
    """Stems that identify the variable in text: 'NSAID use' -> ['nsaid'], 'Education level' -> ['educat']."""
    words = [w for w in _norm(label).split() if len(w) >= 4 and w not in GENERIC]
    return [w[:6] for w in words]


def evidence_sentence(label: str, text: str) -> str:
    """A real sentence of the paper that names the variable and an adjustment, or '' if there is none."""
    terms = _terms(label)
    if not terms:
        return ""
    for sent in re.split(r"(?<=[.;])\s+", text):
        n = _norm(sent)
        if ADJUST_WORDS.search(sent) and all(t in n for t in terms):
            return re.sub(r"\s+", " ", sent).strip()
    return ""


def verify(crit: Critique, paper_text: str) -> Tuple[Critique, List[dict], dict]:
    """Returns (critique without confirmed-adjusted suggestions, the confirmed ones with quotes, usage)."""
    if not crit.missing_confounders:
        return crit, [], {}
    labels = [m.label for m in crit.missing_confounders]
    data, usage = chat_json(config.MODEL_REASON, SYSTEM, INSTRUCTIONS % (json.dumps(labels), paper_text),
                            max_tokens=2500, thinking=False)
    confirmed = {}
    for c in data.get("checks", []):
        label = next((l for l in labels if l.strip().lower() == str(c.get("label", "")).strip().lower()), None)
        if not label or c.get("adjusted") is not True:
            continue
        quote = str(c.get("quote", ""))
        if shows_adjustment(quote, paper_text):
            confirmed[label] = quote
        else:   # the model said yes but its quote doesn't match: accept only if the paper itself shows it
            sent = evidence_sentence(label, paper_text)
            if sent:
                confirmed[label] = sent
    kept = [m for m in crit.missing_confounders if m.label not in confirmed]
    found = [{"label": l, "quote": q} for l, q in confirmed.items()]
    return crit.model_copy(update={"missing_confounders": kept}), found, usage


def add_confirmed(nodes: List[Node], edges: List[Edge], found: List[dict]) -> Tuple[List[Node], List[Edge]]:
    """Put verified covariates into the DAG as adjusted confounders, from the paper (not AI-suggested)."""
    exp = next((n.id for n in nodes if n.role == "exposure"), None)
    out = next((n.id for n in nodes if n.role == "outcome"), None)
    ids = {n.id for n in nodes}
    for f in found:
        nid = graph.slug(f["label"])
        if nid in ids:
            continue
        nodes.append(Node(id=nid, label=f["label"], role="confounder", adjusted=True, snippet=short_snippet(f["quote"])))
        ids.add(nid)
        if exp and out:
            edges += [Edge(source=nid, target=exp), Edge(source=nid, target=out)]
    return nodes, edges


REWRITE = """This assessment of a study was written before a check of the paper's text found that the authors DID adjust
for: %s. Rewrite it so it no longer says or implies those were missing or unadjusted. Change nothing else: keep
every other point, the length and the tone. Return JSON: {"assessment": "..."}

ASSESSMENT:
%s
"""


def fix_assessment(assessment: str, found: List[dict]) -> str:
    """Keep the critique's assessment consistent with the verified DAG (Nano, a few hundred tokens)."""
    if not found or not assessment:
        return assessment
    names = ", ".join(f["label"] for f in found)
    try:
        data, _ = chat_json(config.MODEL_FAST, "You edit text precisely. Reply with a single JSON object.",
                            REWRITE % (names, assessment), max_tokens=800, thinking=False)
        new = str(data.get("assessment", "")).strip()
        if 0.5 * len(assessment) <= len(new) <= 1.5 * len(assessment):
            return new
    except Exception:
        pass
    return f"(Checked against the paper's text: the authors did adjust for {names}.) {assessment}"
