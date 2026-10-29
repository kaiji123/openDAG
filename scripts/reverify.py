"""Re-check every record's AI-suggested confounders against its paper text (Nemotron Super, ~$0.002 a paper).

Suggestions the paper already adjusted for (with a quote that is in the paper, next to an adjustment word) are
removed from the suggestions and added to the DAG as adjusted variables. Review decisions on the remaining
suggestions are kept. Usage: python -m scripts.reverify [--dry-run]
"""
import sys

from app import db, fetch, graph, pipeline, verify
from app.fetch import FetchError
from app.schemas import Critique, Edge, Node

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    total = 0
    for rec in db.list_records():
        crit = Critique(**rec["critique"])
        if not crit.missing_confounders:
            continue
        try:
            _, sections = fetch.fetch_paper(rec["paper"]["pmcid"])
        except FetchError as e:
            print(f"{rec['id']}: skipped ({e})")
            continue
        kept, found, _ = verify.verify(crit, fetch.build_prompt_text(sections))
        title = rec["paper"]["title"][:70]
        if not found:
            print(f"{rec['id']} ok        {title}")
            continue
        total += len(found)
        print(f"{rec['id']} FIXED {len(found)}  {title}")
        for f in found:
            print(f"     already adjusted: {f['label']}  <- \"{f['quote'][:110]}\"")
        if dry:
            continue
        gone = {graph.slug(f["label"]) for f in found}
        nodes = [Node(**n) for n in rec["dag"]["nodes"] if not (n.get("ai_suggested") and n["id"] in gone)]
        edges = [Edge(**e) for e in rec["dag"]["edges"] if e["source"] not in gone and e["target"] not in gone]
        nodes, edges = verify.add_confirmed(nodes, edges, found)
        nodes, edges, _ = graph.validate(nodes, edges)
        kept.overall_assessment = verify.fix_assessment(kept.overall_assessment, found)
        rec["critique"] = kept.model_dump()
        rec["dag"] = {"nodes": [n.model_dump() for n in nodes], "edges": [e.model_dump() for e in edges]}
        rec.setdefault("validation_issues", []).append(
            "Verified against the paper text, already adjusted for: " + ", ".join(f["label"] for f in found)
            + " (added to the DAG, removed from AI suggestions).")
        pipeline._refresh_derived(rec)
        db.save_record(rec)
    print(f"\n{total} false-alarm suggestions {'found' if dry else 'fixed'}")
