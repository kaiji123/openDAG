"""Shape records for the public dataset (JSONL + per-DAG files)."""
from __future__ import annotations

import json
from pathlib import Path

PUBLIC_KEYS = [
    "id", "schema_version", "status", "created_at", "approved_at", "paper", "study",
    "dag", "dagitty", "open_backdoors", "critique", "models",
]


def public_record(rec: dict) -> dict:
    out = {k: rec.get(k) for k in PUBLIC_KEYS}
    out["dataset_license"] = "CC-BY-4.0"
    return out


def summary(rec: dict) -> dict:
    s = rec.get("study", {})
    return {
        "id": rec["id"],
        "status": rec["status"],
        "title": rec["paper"].get("title"),
        "year": rec["paper"].get("year"),
        "journal": rec["paper"].get("journal"),
        "doi": rec["paper"].get("doi"),
        "design": s.get("study_design"),
        "exposure": (s.get("exposure") or {}).get("name"),
        "outcome": (s.get("outcome") or {}).get("name"),
        "n_nodes": len(rec["dag"]["nodes"]),
        "n_missing": len(rec["critique"].get("missing_confounders", [])),
        "approved_at": rec.get("approved_at"),
    }


def write_dataset(records, out_dir: str = "data") -> int:
    root = Path(out_dir)
    dags = root / "dags"
    dags.mkdir(parents=True, exist_ok=True)
    with open(root / "opendag.jsonl", "w") as f:
        for rec in records:
            pub = public_record(rec)
            f.write(json.dumps(pub) + "\n")
            (dags / f"{rec['id']}.json").write_text(json.dumps(pub, indent=2))
            (dags / f"{rec['id']}.dagitty").write_text(rec["dagitty"] + "\n")
    return len(records)


def import_dataset(path: str = "data/opendag.jsonl") -> int:
    """Load approved records from the repo's JSONL into an empty database.

    Lets the app run on hosts with ephemeral disks: the dataset in git is the source of truth.
    """
    from . import db

    p = Path(path)
    if not p.exists() or db.list_records():
        return 0
    n = 0
    for line in p.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            rec.pop("dataset_license", None)
            rec.setdefault("validation_issues", [])
            db.save_record(rec)
            n += 1
    return n


# ---------- dataset-level insights (Dataset page charts) ----------
import re as _re

# Merge the many ways a model phrases the same confounder. First match wins.
CONFOUNDER_GROUPS = [
    ("Socioeconomic status", r"socio|income|deprivation|education|poverty|insurance|occupation"),
    ("Smoking", r"smok|tobacco"),
    ("Alcohol use", r"alcohol|drinking"),
    ("BMI / obesity", r"\bbmi\b|body mass|obes|adipos|weight"),
    ("Frailty", r"frail"),
    ("Physical activity", r"physical activity|exercise|sedentary"),
    ("Diet", r"diet|nutrition|food"),
    ("Comorbidity burden", r"comorbid|multimorbid|charlson|chronic disease"),
    ("Disease severity", r"severity|sofa|lactate|stage|grade"),
    ("Medication use", r"medication|drug|polypharm|prescri|nsaid|aspirin|statin|steroid"),
    ("Healthcare access / use", r"health ?care|access to care|healthcare use|utili[sz]ation|health-seeking|screening"),
    ("Genetic / family history", r"genetic|family history|heredit"),
    ("Sleep", r"sleep"),
    ("Mental health", r"depress|anxiety|mental|psychiatric"),
    ("Kidney function", r"renal|kidney|egfr"),
]

BIAS_GROUPS = [
    ("Confounding by indication", r"indication"),
    ("Residual confounding", r"residual|unmeasured confound"),
    ("Immortal time bias", r"immortal"),
    ("Reverse causation", r"reverse"),
    ("Healthy user effect", r"healthy (user|adherer)"),
    ("Selection bias", r"selection|collider|survivor"),
    ("Measurement / misclassification", r"measurement|misclassif|recall|self-report|information bias"),
    ("Loss to follow-up", r"loss to follow|attrition|censor"),
    ("Detection bias", r"detection|surveillance"),
]

RATIO_MEASURES = {"HR", "OR", "RR", "IRR", "AHR", "AOR", "ARR", "SHR", "PR", "SIR", "SMR"}


def _group(label: str, groups) -> str:
    low = (label or "").lower()
    for name, pat in groups:
        if _re.search(pat, low):
            return name
    clean = _re.sub(r"\s*\(.*?\)", "", label or "").strip()
    return clean[:1].upper() + clean[1:] if clean else "Other"


def _ci(text):
    m = _re.search(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|—|,|to)\s*(-?\d+(?:\.\d+)?)", str(text or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def _mapping() -> dict:
    """Stored Nemotron name mappings (empty if the table doesn't exist yet)."""
    try:
        from .canonical import lookup_all
        return lookup_all()
    except Exception:
        return {}


def insights(records) -> dict:
    forest, skipped = [], 0
    missing, bias = {}, {}
    for r in records:
        s, eff = r.get("study", {}), r.get("study", {}).get("reported_effect") or {}
        ref = {"id": r["id"], "title": r["paper"].get("title", ""), "year": r["paper"].get("year")}
        measure = (eff.get("measure") or "").upper().replace(" ", "")
        try:
            est = float(str(eff.get("value")).split()[0])
        except (TypeError, ValueError, IndexError):
            est = None
        ci = _ci(eff.get("ci"))
        if est is not None and ci and measure in RATIO_MEASURES and min(est, *ci) > 0 and ci[0] <= est <= ci[1]:
            forest.append({**ref, "exposure": (s.get("exposure") or {}).get("name"),
                           "outcome": (s.get("outcome") or {}).get("name"), "design": s.get("study_design"),
                           "measure": eff.get("measure"), "value": est, "lo": ci[0], "hi": ci[1],
                           "adjusted": eff.get("adjusted", False)})
        elif eff.get("value"):
            skipped += 1
        for mc in r.get("critique", {}).get("missing_confounders", []):
            # themes, not synonyms: "income" and "deprivation" both count towards socioeconomic status
            g = missing.setdefault(_group(mc.get("label"), CONFOUNDER_GROUPS), {"count": 0, "accepted": 0, "papers": {}})
            if r["id"] not in g["papers"]:
                g["count"] += 1
                g["papers"][r["id"]] = ref["title"]
            g["accepted"] += mc.get("status") == "accepted"
        for b in r.get("critique", {}).get("bias_risks", []):
            g = bias.setdefault(_group(b.get("type"), BIAS_GROUPS), {"count": 0, "high": 0})
            g["count"] += 1
            g["high"] += b.get("severity") == "high"
    forest.sort(key=lambda f: ((f["exposure"] or "").lower(), f["value"]))
    top = sorted(missing.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:10]
    return {
        "n_papers": len(records),
        "forest": forest,
        "forest_skipped": skipped,
        "missing_confounders": [{"label": k, "count": v["count"], "accepted": v["accepted"],
                                 "papers": list(v["papers"].values())} for k, v in top],
        "bias_risks": [{"type": k, **v} for k, v in sorted(bias.items(), key=lambda kv: -kv[1]["count"])],
    }


# ---------- cross-paper network (Network page) ----------
# Specific aliases first; then the confounder groups above (only for non-exposure/outcome roles).
VARIABLE_ALIASES = [
    ("Age", r"^age\b|\bage (group|at)\b"),
    ("Sex", r"^sex\b|\bgender\b"),
    ("All-cause mortality", r"all.cause mortality|^mortality$|natural mortality|^death$|^all.cause death"),
    ("PPI use", r"proton.pump|\bppis?\b"),
    ("Ultra-processed food", r"ultra.processed|\bupf\b"),
    ("Physical activity", r"^physical activity"),
    ("Coffee / tea", r"coffee|\btea\b"),
    ("Diabetes", r"diabet"),
    ("Hypertension", r"hypertens|blood pressure"),
    ("Cardiovascular disease", r"cardiovascular disease|\bcvd\b|heart disease"),
    ("Calendar year / period", r"calendar|year of|index year|period"),
    ("Region / area", r"region|geograph|urban|rural|area of residence"),
]


def canonical_variable(label: str, role: str) -> str:
    low = _re.sub(r"\s*\(.*?\)", "", (label or "").lower()).strip()
    for name, pat in VARIABLE_ALIASES:
        if _re.search(pat, low):
            return name
    if role not in ("exposure", "outcome"):
        for name, pat in CONFOUNDER_GROUPS:
            if _re.search(pat, low):
                return name
    clean = _re.sub(r"\s*\(.*?\)", "", label or "").strip()
    return clean[:1].upper() + clean[1:] if clean else "Unknown"


def canonical_for(label: str, role: str, table: dict, rules=None) -> str:
    """Nemotron's stored mapping if there is one, otherwise the rule-based name."""
    hit = table.get(_re.sub(r"\s+", " ", (label or "").strip().lower()))
    if hit:
        return hit["canonical"]
    return (rules or canonical_variable)(label, role)


def network(records) -> dict:
    """Variables merged across papers, and the causal links papers assume between them."""
    table = _mapping()
    nodes, links = {}, {}
    for r in records:
        ref = {"id": r["id"], "title": r["paper"].get("title", ""), "year": r["paper"].get("year")}
        by_id = {}
        for n in r["dag"]["nodes"]:
            raw = n.get("label") or n.get("id")
            name = canonical_for(raw, n.get("role", "other"), table)
            by_id[n["id"]] = name
            node = nodes.setdefault(name, {"id": name, "papers": {}, "roles": {}, "adjusted": 0, "ai": 0,
                                           "aliases": set(), "mesh": None, "mesh_id": None, "by_model": False})
            node["aliases"].add(raw)
            hit = table.get(_re.sub(r"\s+", " ", raw.strip().lower()))
            if hit:
                node["by_model"] = True
                if hit.get("mesh") and not node["mesh"]:
                    node["mesh"], node["mesh_id"] = hit["mesh"], hit.get("mesh_id")
            role = "ai" if n.get("ai_suggested") else n.get("role", "other")
            if r["id"] not in node["papers"]:
                node["papers"][r["id"]] = {**ref, "role": role, "adjusted": bool(n.get("adjusted"))}
                node["roles"][role] = node["roles"].get(role, 0) + 1
                node["adjusted"] += bool(n.get("adjusted"))
                node["ai"] += bool(n.get("ai_suggested"))
        for e in r["dag"]["edges"]:
            s, t = by_id.get(e["source"]), by_id.get(e["target"])
            if not s or not t or s == t:
                continue
            link = links.setdefault((s, t), {"source": s, "target": t, "papers": set(), "ai_only": True})
            link["papers"].add(r["id"])
            link["ai_only"] = link["ai_only"] and bool(e.get("ai_suggested"))

    def main_role(n):
        roles = n["roles"]
        for role in ("exposure", "outcome"):
            if roles.get(role):
                return role
        if n["adjusted"]:
            return "confounder"
        if roles.get("ai") and len(roles) == 1:
            return "ai"
        return max(roles, key=roles.get)

    out_nodes = [{"id": k, "label": k, "group": main_role(v), "n_papers": len(v["papers"]),
                  "papers": list(v["papers"].values()), "aliases": sorted(v["aliases"], key=str.lower),
                  "mesh": v["mesh"], "mesh_id": v["mesh_id"], "merged_by": "nemotron" if v["by_model"] else "rules"}
                 for k, v in nodes.items()]
    out_links = [{"source": v["source"], "target": v["target"], "n_papers": len(v["papers"]), "ai": v["ai_only"]}
                 for v in links.values()]
    out_nodes.sort(key=lambda n: -n["n_papers"])
    return {"n_papers": len(records), "nodes": out_nodes, "links": out_links}
