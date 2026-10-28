"""Run the pipeline over every identifier in seed_dois.txt (one per line, # for comments).

Usage: python -m scripts.seed [--approve]
  --approve  auto-approve, accepting all AI suggestions (only for quick demos;
             reviewing in the UI is the intended workflow)
"""
import sys

from app import pipeline
from app.fetch import FetchError

if __name__ == "__main__":
    auto = "--approve" in sys.argv
    ids = [l.split("#")[0].strip() for l in open("seed_dois.txt")]
    for ident in filter(None, ids):
        print(f"→ {ident}", flush=True)
        try:
            rec = pipeline.run(ident, progress=lambda s: print(f"   {s}", flush=True))
        except FetchError as e:
            print(f"   skipped: {e}")
            continue
        except Exception as e:
            print(f"   FAILED: {e}")
            continue
        print(f"   {rec['id']}: {rec['paper']['title'][:80]}")
        if auto and rec["status"] != "approved":
            status = {mc["label"]: "accepted" for mc in rec["critique"]["missing_confounders"]}
            rec = pipeline.update(rec, rec["dag"]["nodes"], rec["dag"]["edges"], status)
            pipeline.approve(rec)
            print("   approved")
