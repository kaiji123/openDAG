"""Variable standardisation with Nemotron Nano: map each paper's variable names to shared names.

"Age (years)", "age at baseline" and "Age group" should all become one node, "Age", when papers
are linked into the dataset network. Nano does the mapping; results are stored in SQLite so each
original name is sent to the model only once. If the model call fails, the hand-written rules
in export.canonical_variable are used instead.
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

from . import config, db
from .llm import chat_json

BATCH = 60  # labels per model call

SYSTEM = """You standardise variable names from epidemiology studies so the same concept gets the same name across papers.
Reply with a single JSON object and nothing else."""

INSTRUCTIONS = """Map each input label to a canonical variable name.

Each input has the role it plays in its study.

Rules:
- Exposures and outcomes are the subject of a study: keep them SPECIFIC. Never map them to a broader
  category ("Gabapentin use" stays "Gabapentin use", not "Medication use"; "Opioid-related death" stays
  "Opioid-related death", not "All-cause mortality").
- Merge only TRUE SYNONYMS: the same variable measured or worded differently. Units, measurement method,
  categorisation and wording don't matter: "Age (years)", "age at baseline" and "age group" are all "Age".
  Related-but-different variables stay separate: "Education", "Income" and "Marital status" are three
  variables; "LDL cholesterol" is not "Kidney function". When unsure, keep them separate.
- Reuse a name from EXISTING when it is a true synonym.
- Do NOT merge different concepts: a disease and its treatment ("Diabetes" vs "Antidiabetic medication"),
  a specific outcome and a broader one ("Cardiovascular mortality" vs "All-cause mortality"), two different
  exposures, or severity of two different diseases.
- A label listing several things (e.g. "Comorbidities (diabetes, hypertension)") maps to a group name such as "Comorbidities".
- A label naming two outcomes (e.g. "Stroke and dementia") keeps both: "Stroke and dementia".
- A new canonical name must be short (1-4 words), sentence case, generic (not study-specific), with no parentheses.
- "mesh": the exact MeSH descriptor heading if one clearly matches, otherwise null. Do not invent headings.

Return JSON: {"mappings": [{"label": "<input label, copied exactly>", "canonical": "...", "mesh": "..." or null}]}

EXISTING canonical names:
%s

INPUT (label and role):
%s
"""

MESH_LOOKUP = "https://id.nlm.nih.gov/mesh/lookup/descriptor"


def verify_mesh(term: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (official heading, descriptor ID) if the term is a real MeSH descriptor, else None."""
    if not term:
        return None
    cached = db.cache_get("mesh", term.lower())
    if cached is not None:
        hit = json.loads(cached)
        return tuple(hit) if hit else None
    hit = None
    for candidate in dict.fromkeys([term, term[:1].upper() + term[1:].lower(), term.title()]):
        try:
            r = httpx.get(MESH_LOOKUP, params={"label": candidate, "match": "exact", "limit": 1}, timeout=15)
            rows = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return None  # lookup unavailable: don't cache, try again next time
        if rows:
            hit = (rows[0]["label"], rows[0]["resource"].rsplit("/", 1)[-1])
            break
    db.cache_set("mesh", term.lower(), json.dumps(hit))
    return hit



def key(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower())


def _ensure_table() -> None:
    db.conn().execute(
        """CREATE TABLE IF NOT EXISTS canonical (
               raw_key TEXT PRIMARY KEY, raw_label TEXT, canonical TEXT,
               mesh TEXT, source TEXT, created_at REAL)"""
    )
    cols = {r[1] for r in db.conn().execute("PRAGMA table_info(canonical)")}
    if "mesh_id" not in cols:
        db.conn().execute("ALTER TABLE canonical ADD COLUMN mesh_id TEXT")


def lookup_all() -> Dict[str, dict]:
    _ensure_table()
    rows = db.conn().execute("SELECT raw_key, raw_label, canonical, mesh, mesh_id, source FROM canonical").fetchall()
    return {r["raw_key"]: dict(r) for r in rows}


def _save(rows: List[dict]) -> None:
    _ensure_table()
    with db._lock:
        db.conn().executemany(
            "INSERT OR REPLACE INTO canonical (raw_key, raw_label, canonical, mesh, mesh_id, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(key(r["label"]), r["label"], r["canonical"], r.get("mesh"), r.get("mesh_id"), "nemotron", time.time()) for r in rows],
        )
        db.conn().commit()


def _clean_name(name: str) -> Optional[str]:
    name = re.sub(r"\s*\(.*?\)", "", str(name or "")).strip(" .;,")
    if not name or len(name) > 40:
        return None
    return name[:1].upper() + name[1:]


def existing_names() -> List[str]:
    """Canonical names already in use. Deliberately not seeded with broad buckets, which invite over-merging."""
    return sorted({r["canonical"] for r in lookup_all().values()})


def resolve(items: Iterable) -> int:
    """Map any labels not seen before with Nemotron Nano. Items are labels or (label, role) pairs.

    Returns how many were stored.
    """
    known = lookup_all()
    todo, seen = [], set()
    for item in items:
        label, role = item if isinstance(item, tuple) else (item, "confounder")
        k = key(label)
        if label and k not in known and k not in seen:
            seen.add(k)
            todo.append({"label": label.strip(), "role": role})
    stored = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        prompt = INSTRUCTIONS % (json.dumps(existing_names()), json.dumps(batch))
        data, _usage = chat_json(config.MODEL_FAST, SYSTEM, prompt, max_tokens=3000, thinking=False)
        wanted = {key(b["label"]): b["label"] for b in batch}
        rows = []
        for m in data.get("mappings", []):
            original = wanted.get(key(m.get("label", "")))
            name = _clean_name(m.get("canonical"))
            if original and name:  # ignore anything the model invented or mangled
                mesh = m.get("mesh")
                checked = verify_mesh(str(mesh)[:80]) if mesh not in (None, "", "null") else None
                rows.append({"label": original, "canonical": name,
                             "mesh": checked[0] if checked else None, "mesh_id": checked[1] if checked else None})
        _save(rows)
        stored += len(rows)
    return stored


def export_mappings(path: str = "data/variable_mappings.json") -> int:
    """Write the stored mappings to the repo so deploys keep them (part of the open dataset)."""
    from pathlib import Path

    rows = sorted(lookup_all().values(), key=lambda r: r["raw_key"])
    out = [{"label": r["raw_label"], "canonical": r["canonical"], "mesh": r.get("mesh"),
            "mesh_id": r.get("mesh_id")} for r in rows]
    Path(path).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return len(out)


def import_mappings(path: str = "data/variable_mappings.json") -> int:
    """Load mappings from the repo into an empty table (on startup)."""
    from pathlib import Path

    p = Path(path)
    if not p.exists() or lookup_all():
        return 0
    rows = [{"label": m["label"], "canonical": m["canonical"], "mesh": m.get("mesh"), "mesh_id": m.get("mesh_id")}
            for m in json.loads(p.read_text())]
    _save(rows)
    return len(rows)


def record_labels(rec: dict) -> List[Tuple[str, str]]:
    """(label, role) for every variable in a record, including AI-suggested confounders."""
    items = [(n.get("label") or n.get("id"), "AI-suggested confounder" if n.get("ai_suggested") else n.get("role", "other"))
             for n in rec["dag"]["nodes"]]
    items += [(m.get("label"), "AI-suggested confounder") for m in rec.get("critique", {}).get("missing_confounders", [])]
    return [(l, r) for l, r in items if l]


def resolve_record(rec: dict) -> int:
    """Standardise a record's variable names. Never raises: rules are the fallback."""
    try:
        return resolve(record_labels(rec))
    except Exception as e:  # model or network problem; the rules still give a usable network
        print(f"Variable standardisation skipped for {rec.get('id')}: {e}")
        return 0
