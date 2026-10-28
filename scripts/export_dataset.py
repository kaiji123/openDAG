"""Export every approved DAG to data/opendag.jsonl and data/dags/<id>.{json,dagitty}.

Usage: python -m scripts.export_dataset
"""
from app import canonical, db, export

if __name__ == "__main__":
    n = export.write_dataset(db.list_records("approved"))
    print(f"Exported {n} approved DAGs to data/")
    m = canonical.export_mappings()
    print(f"Exported {m} variable-name mappings to data/variable_mappings.json")
