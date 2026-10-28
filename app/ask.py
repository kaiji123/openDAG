""""Ask the dataset": grounded Q&A over OpenDAG records with Nemotron Nano.

The dataset is small, so every relevant record goes into the prompt as a compact digest (no search
index to drift out of date). The model must cite record IDs; citations to IDs that don't exist are
removed before the answer reaches the user.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import List, Tuple

from . import config, db
from .llm import chat_json

DIGEST_BUDGET = 40000  # characters of dataset sent per question (~10k tokens)

SYSTEM = """You answer questions about OpenDAG, a dataset of causal diagrams (DAGs) extracted from published
epidemiology papers. Use ONLY the records provided. Never use outside knowledge about the papers or the topics.
Reply with a single JSON object and nothing else."""

INSTRUCTIONS = """Answer the question using only the RECORDS below.

Each record has:
- "adjusted": variables the authors adjusted for (reported by the paper)
- "missing": confounders an AI model suggested the authors did NOT adjust for, with the reviewer's decision
  (accepted / rejected / suggested). These are AI suggestions, not findings of the paper; say so when you use them.
- "bias": AI-assessed bias risks with severity
- "effect": the paper's main reported estimate

Rules:
- Cite every paper you rely on with its id in square brackets, e.g. [a1b2c3d4e5]. Only use ids from the RECORDS.
- If the records don't contain the answer, say that plainly instead of guessing.
- Be concise: 2-6 sentences, or a short "- " bullet list when listing papers. Use **bold** sparingly.
- Describe associations as the papers report them; don't claim causation and don't give medical advice.

Return JSON: {"answer": "...", "cited": ["<id>", ...]}

QUESTION: %s

RECORDS (%d papers):
%s
"""


def _fmt_effect(eff: dict) -> str:
    if not eff or not eff.get("value"):
        return ""
    out = f"{eff.get('measure', '')} {eff['value']}".strip()
    if eff.get("ci"):
        out += f" (95% CI {eff['ci']})"
    return out + (", adjusted" if eff.get("adjusted") else "")


def digest_record(r: dict, full: bool = True) -> dict:
    s = r.get("study", {})
    crit = r.get("critique", {})
    d = {
        "id": r["id"],
        "title": (r["paper"].get("title") or "")[:140],
        "year": r["paper"].get("year"),
        "status": r.get("status"),
        "design": s.get("study_design"),
        "exposure": (s.get("exposure") or {}).get("name"),
        "outcome": (s.get("outcome") or {}).get("name"),
        "effect": _fmt_effect(s.get("reported_effect") or {}),
        "n": s.get("sample_size"),
        "adjusted": [n["label"] for n in r["dag"]["nodes"] if n.get("adjusted")],
        "missing": [f"{m['label']} ({m.get('status', 'suggested')})" for m in crit.get("missing_confounders", [])],
        "bias": [f"{b['type']} ({b.get('severity')})" for b in crit.get("bias_risks", [])],
    }
    if full:
        d["population"] = (s.get("population") or "")[:200]
        d["assessment"] = (crit.get("overall_assessment") or "")[:300]
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def build_digest(records: List[dict]) -> str:
    """Compact JSON lines; drops the long free-text fields if the dataset outgrows the budget."""
    lines = [json.dumps(digest_record(r), ensure_ascii=False) for r in records]
    if sum(map(len, lines)) > DIGEST_BUDGET:
        lines = [json.dumps(digest_record(r, full=False), ensure_ascii=False) for r in records]
    text, used = [], 0
    for line in lines:
        if used + len(line) > DIGEST_BUDGET:
            break
        text.append(line)
        used += len(line)
    return "\n".join(text)


# ---------- step 1: plan. Nano turns the question into a structured query ----------
PLAN_SYSTEM = """You turn questions about an epidemiology DAG dataset into a structured query.
Reply with a single JSON object and nothing else."""

PLAN_INSTRUCTIONS = """Translate the question into a query over the dataset. Each record (one paper) has:
exposure, outcome, design (cohort | case-control | cross-sectional | RCT | other),
adjusted (variables the authors adjusted for), missing (confounders an AI suggested they missed),
bias (AI-assessed bias risks with severity low/medium/high), and a main effect estimate with 95%% CI.

Return JSON:
{
  "mode": "filter" | "aggregate" | "open",
  "filters": [
    {"field": "adjusted" | "not_adjusted" | "missing" | "bias" | "exposure" | "outcome" | "design" | "significant" | "direction",
     "terms": ["lowercase word stems that identify the concept, with synonyms, e.g. smok, tobacco, cigarette"],
     "severity": "low" | "medium" | "high" | null,
     "value": "for design: the design; for significant: true/false; for direction: increased/decreased"}
  ],
  "aggregate": null | "missing" | "adjusted" | "bias" | "design" | "exposure" | "outcome"
}

- "filter": the question asks WHICH papers meet conditions. Put every condition in filters (they are ANDed).
- "aggregate": the question asks what is MOST COMMON / how many / counts across papers. Optional filters narrow the papers first.
- "open": anything else (summaries, comparisons, explanations, methods). Filters may narrow the papers first.
- "significant": true when the 95%% CI excludes the null. "direction": increased = estimate above 1.
- Use short stems in terms so they match variants ("depress" matches depression and depressive).
- Only add a filter for a condition the question explicitly states. Never add filters from the examples.

Examples:
"Which papers did not adjust for smoking?" -> {"mode":"filter","filters":[{"field":"not_adjusted","terms":["smok","tobacco","cigarette"]}],"aggregate":null}
"Which studies have a high risk of selection bias?" -> {"mode":"filter","filters":[{"field":"bias","terms":["selection"],"severity":"high"}],"aggregate":null}
"What confounders are most often suggested as missing?" -> {"mode":"aggregate","filters":[],"aggregate":"missing"}
"What bias risks are most common in cohort studies?" -> {"mode":"aggregate","filters":[{"field":"design","value":"cohort"}],"aggregate":"bias"}
"Summarise the PPI studies" -> {"mode":"open","filters":[{"field":"exposure","terms":["ppi","proton pump"]}],"aggregate":null}
"Does alcohol cause liver disease?" -> {"mode":"open","filters":[{"field":"exposure","terms":["alcohol"]},{"field":"outcome","terms":["liver","hepat"]}],"aggregate":null}

QUESTION: %s
"""

FIELDS = {"adjusted", "not_adjusted", "missing", "bias", "exposure", "outcome", "design", "significant", "direction"}


def _num(x):
    try:
        return float(str(x).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _ci(text):
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|—|,|to)\s*(-?\d+(?:\.\d+)?)", str(text or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


RATIO = {"HR", "OR", "RR", "IRR", "AHR", "AOR", "ARR", "SHR", "PR"}


def _hits(texts, terms) -> List[str]:
    if terms == [""]:
        return list(texts)  # "any"
    terms = [t.lower().strip() for t in terms if t and len(t.strip()) >= 2]
    return [t for t in texts if any(term in t.lower() for term in terms)]


def matches(r: dict, f: dict) -> bool:
    """Exact, deterministic check of one filter against one record."""
    field, terms = f.get("field"), f.get("terms") or []
    s, crit = r.get("study", {}), r.get("critique", {})
    adjusted = [n["label"] for n in r["dag"]["nodes"] if n.get("adjusted")]
    if field == "adjusted":
        return bool(_hits(adjusted, terms))
    if field == "not_adjusted":
        return not _hits(adjusted, terms)
    if field == "missing":
        return bool(_hits([m["label"] for m in crit.get("missing_confounders", []) if m.get("status") != "rejected"], terms))
    if field == "bias":
        sev = f.get("severity")
        return any(_hits([b["type"]], terms) and (not sev or b.get("severity") == sev) for b in crit.get("bias_risks", []))
    if field in ("exposure", "outcome"):
        return bool(_hits([(s.get(field) or {}).get("name", "")], terms))
    if field == "design":
        return str(s.get("study_design", "")).lower() == str(f.get("value", "")).lower()
    eff = s.get("reported_effect") or {}
    est, ci = _num(eff.get("value")), _ci(eff.get("ci"))
    null = 1.0 if str(eff.get("measure", "")).upper() in RATIO else 0.0
    if field == "significant":
        if not ci:
            return False
        sig = ci[0] > null or ci[1] < null
        return sig == (str(f.get("value")).lower() in ("true", "yes", "1"))
    if field == "direction":
        if est is None:
            return False
        return (est > null) if str(f.get("value")).lower().startswith("inc") else (est < null)
    return True


def describe(f: dict) -> str:
    field, terms = f.get("field"), ", ".join(f.get("terms") or [])
    return {
        "adjusted": f"adjusted for [{terms}]",
        "not_adjusted": f"did not adjust for [{terms}]",
        "missing": f"AI flagged missing [{terms}]",
        "bias": (f"bias risk [{terms}]" if terms else "any bias risk") + (f" rated {f['severity']}" if f.get("severity") else ""),
        "exposure": f"exposure matches [{terms}]",
        "outcome": f"outcome matches [{terms}]",
        "design": f"design = {f.get('value')}",
        "significant": f"95% CI {'excludes' if str(f.get('value')).lower() in ('true', 'yes', '1') else 'includes'} the null",
        "direction": f"estimate {'above' if str(f.get('value')).lower().startswith('inc') else 'below'} the null",
    }.get(field, field)


def aggregate(records: List[dict], what: str) -> List[Tuple[str, int]]:
    from collections import Counter
    from .export import CONFOUNDER_GROUPS, BIAS_GROUPS, _group
    c = Counter()
    for r in records:
        s, crit = r.get("study", {}), r.get("critique", {})
        if what == "missing":
            items = {_group(m["label"], CONFOUNDER_GROUPS) for m in crit.get("missing_confounders", []) if m.get("status") != "rejected"}
        elif what == "adjusted":
            items = {_group(n["label"], CONFOUNDER_GROUPS) for n in r["dag"]["nodes"] if n.get("adjusted")}
        elif what == "bias":
            items = {_group(b["type"], BIAS_GROUPS) for b in crit.get("bias_risks", [])}
        elif what in ("exposure", "outcome"):
            items = {(s.get(what) or {}).get("name", "")}
        else:  # design
            items = {s.get("study_design", "")}
        c.update(i for i in items if i)
    return c.most_common(12)


# Words that describe the dataset itself; a filter on them matches nothing meaningful.
GENERIC_TERMS = {"confound", "confounder", "confounders", "variable", "variables", "paper", "papers", "study",
                 "studies", "missing", "bias", "risk", "factor", "factors", "adjust", "adjusted", "covariate"}


def _useful(f: dict) -> bool:
    if f.get("field") not in FIELDS:
        return False
    if f["field"] in ("design", "significant", "direction"):
        return f.get("value") not in (None, "", "null")
    terms = [t for t in (f.get("terms") or []) if str(t).lower().strip() not in GENERIC_TERMS]
    if f["field"] == "bias" and not terms and f.get("severity"):
        f["terms"] = [""]  # "any bias rated high"
        return True
    f["terms"] = terms
    return bool(terms)


def plan(question: str) -> dict:
    # Super writes the query: it follows the "only conditions in the question" rule far better than Nano.
    data, _ = chat_json(config.MODEL_REASON, PLAN_SYSTEM, PLAN_INSTRUCTIONS % question, max_tokens=600, thinking=False)
    mode = data.get("mode") if data.get("mode") in ("filter", "aggregate", "open") else "open"
    filters = [f for f in (data.get("filters") or []) if isinstance(f, dict) and _useful(f)]
    agg = data.get("aggregate") if data.get("aggregate") in ("missing", "adjusted", "bias", "design", "exposure", "outcome") else None
    if mode == "aggregate" and not agg:
        mode = "open"
    return {"mode": mode, "filters": filters, "aggregate": agg}


# ---------- step 2: execute exactly, step 3: Nano writes the answer from the results ----------
ANSWER_WITH_RESULTS = """Answer the question from the EXACT RESULTS below. They were computed by code over the whole
dataset of %d papers, so report them faithfully: do not add, drop or re-judge papers, and do not change counts.

- Cite each paper you mention with its id in square brackets, e.g. [a1b2c3d4e5].
- If the results are empty, say no paper in the dataset matches all the conditions, then use NEAR MATCHES
  (papers meeting some of the conditions) to say what the dataset does contain. Don't write the words "near matches".
- For counts, give the numbers (e.g. "socioeconomic status: 9 of 14 papers").
- "missing" and "bias" come from an AI critique, not from the papers; say so when you use them.
- Be concise: 1-3 sentences, then a short "- " bullet list of papers if there are any (title, then [id]).
- Don't claim causation and don't give medical advice.

Return JSON: {"answer": "...", "cited": ["<id>", ...]}

QUESTION: %s
HOW THE RESULTS WERE COMPUTED: %s
EXACT RESULTS:
%s
NEAR MATCHES:
%s
"""


def answer(question: str, records: List[dict]) -> Tuple[dict, bool]:
    """Returns ({answer, cited, method, n_papers, model}, from_cache)."""
    question = re.sub(r"\s+", " ", question).strip()[:300]
    digest = build_digest(records)
    cache_key = hashlib.sha256(f"v4|{config.MODEL_FAST}|{question.lower()}|{digest}".encode()).hexdigest()
    cached = db.cache_get("ask", cache_key)
    if cached:
        return json.loads(cached), True

    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        p = plan(question)
    except Exception as e:  # fall back to answering from the full digest, but leave a trace
        print(f"ask: planning failed, answering from full digest: {e}")
        p = {"mode": "open", "filters": [], "aggregate": None}
    subset = [r for r in records if all(matches(r, f) for f in p["filters"])]
    steps = [describe(f) for f in p["filters"]]
    method = (f"Filtered {len(records)} papers: " + "; ".join(steps) + f" → {len(subset)} match") if steps else f"Used all {len(records)} papers"

    near = "(none)"
    if p["filters"] and not subset:  # nothing meets every condition: show what meets each one alone
        lines = []
        for f in p["filters"]:
            hits = [r for r in records if matches(r, f)]
            lines.append(f"{describe(f)}: " + ("; ".join(f"{r['paper'].get('title', '')[:90]} [{r['id']}]" for r in hits[:5]) or "none"))
        near = "\n".join(lines)

    if p["mode"] == "filter" or (p["filters"] and not subset):
        rows = [json.dumps(digest_record(r, full=False), ensure_ascii=False) for r in subset]
        prompt = ANSWER_WITH_RESULTS % (len(records), question, method, "\n".join(rows) or "(no matching papers)", near)
        data, usage = chat_json(config.MODEL_FAST, SYSTEM, prompt, max_tokens=1200, thinking=False)
    elif p["mode"] == "aggregate":
        counts = aggregate(subset, p["aggregate"])
        method += f"; counted {p['aggregate']} per paper (similar names grouped)"
        table = "\n".join(f"{name}: {n} of {len(subset)} papers" for name, n in counts) or "(nothing to count)"
        prompt = ANSWER_WITH_RESULTS % (len(records), question, method, table, near)
        data, usage = chat_json(config.MODEL_FAST, SYSTEM, prompt, max_tokens=800, thinking=False)
    else:
        scope = subset or records
        prompt = INSTRUCTIONS % (question, len(scope), build_digest(scope))
        data, usage = chat_json(config.MODEL_FAST, SYSTEM, prompt, max_tokens=1200, thinking=False)

    valid = {r["id"]: r for r in records}
    text = str(data.get("answer") or "").strip()
    # strip citations to ids that aren't in the dataset (the model must not invent sources)
    text = re.sub(r"\[([0-9a-f]{6,16})\]", lambda m: m.group(0) if m.group(1) in valid else "", text)
    # drop field names the model sometimes copies from the records, e.g. "[effect]"
    text = re.sub(r"\s*\[([a-z_]{1,12})\]", lambda m: m.group(0) if m.group(1) in valid else "", text)
    cited = [i for i in dict.fromkeys(re.findall(r"\[([0-9a-f]{6,16})\]", text)) if i in valid]
    if p["mode"] == "filter" and subset:  # the exact matches are the evidence, whatever the model chose to cite
        cited += [r["id"] for r in subset if r["id"] not in cited]
    cited += [i for i in data.get("cited", []) if i in valid and i not in cited]
    result = {
        "answer": text or "Sorry, I couldn't produce an answer from the dataset.",
        "cited": [{"id": i, "title": valid[i]["paper"].get("title", ""), "year": valid[i]["paper"].get("year")}
                  for i in cited],
        "method": method,
        "mode": p["mode"],
        "n_papers": len(records),
        "model": config.MODEL_FAST,
        "planner": config.MODEL_REASON,
        "usage": usage,
    }
    db.cache_set("ask", cache_key, json.dumps(result))
    return result, False
