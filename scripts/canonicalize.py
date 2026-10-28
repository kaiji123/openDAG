"""Standardise variable names for every record (drafts included) with Nemotron Nano.

Only names never seen before are sent to the model. Usage: python -m scripts.canonicalize [--redo]
  --redo  forget all stored mappings first (e.g. after improving the prompt)
"""
import sys

from app import canonical, db

if __name__ == "__main__":
    if "--redo" in sys.argv:
        canonical._ensure_table()
        db.conn().execute("DELETE FROM canonical")
        db.conn().commit()
    labels = [l for rec in db.list_records() for l in canonical.record_labels(rec)]
    print(f"{len(set(canonical.key(l) for l, _ in labels))} distinct variable names in the dataset")
    print(f"mapped {canonical.resolve(labels)} new names with Nemotron Nano")
