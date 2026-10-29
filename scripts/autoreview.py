"""Review every draft with Nemotron Super and approve it, labelled as AI-reviewed.

Usage: python -m scripts.autoreview [--dry-run]
  --dry-run  print the decisions without saving anything
"""
import sys

from app import autoreview, db

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    for rec in db.list_records("draft"):
        print(f"→ {rec['id']}: {rec['paper']['title'][:80]}", flush=True)
        try:
            if dry:
                for label, d in autoreview.review(rec).items():
                    print(f"   {d['status']:8} {label} — {d['reason']}")
            else:
                rec = autoreview.review_and_approve(rec)
                for m in rec["critique"]["missing_confounders"]:
                    print(f"   {m['status']:8} {m['label']} — {m['review_note']}")
        except Exception as e:
            print(f"   skipped: {e}")
