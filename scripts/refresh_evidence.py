"""Re-run the Tavily evidence step on existing records (no model calls).

Useful after adding TAVILY_API_KEY or improving the evidence filter. Only evidence links change;
the DAG and review decisions are never changed, so approved records stay as reviewed.
Usage: python -m scripts.refresh_evidence
"""
from app import db
from app.evidence import evidence_for

if __name__ == "__main__":
    for rec in db.list_records():
        exp, out = rec["study"]["exposure"]["name"], rec["study"]["outcome"]["name"]
        added = 0
        for mc in rec["critique"]["missing_confounders"]:
            mc["evidence"] = evidence_for(mc["label"], exp, out, rec["paper"])
            added += len(mc["evidence"])
        db.save_record(rec)
        print(f"{rec['id']} ({rec['status']}): {added} sources")
