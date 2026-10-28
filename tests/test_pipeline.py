"""Offline tests: the LLM and Tavily are mocked; graph logic is tested directly.

Run: pytest -q          (add -m network to also hit Europe PMC)
"""
import pytest

from app import db, graph, pipeline
from app.llm import parse_json
from app.schemas import Critique, Edge, Extraction, Node

EXTRACTION = {
    "study_design": "Retrospective cohort study",
    "population": "Adults in Taiwan's national insurance database, 2000-2013",
    "exposure": {"name": "Sepsis", "definition": "Hospitalisation with sepsis ICD codes"},
    "outcome": {"name": "Out-of-hospital cardiac arrest", "definition": "OHCA events"},
    "nodes": [
        {"id": "sepsis", "label": "Sepsis", "role": "exposure", "adjusted": True, "snippet": "sepsis survivors"},
        {"id": "ohca", "label": "Out-of-hospital cardiac arrest", "role": "outcome", "adjusted": False, "snippet": "OHCA"},
        {"id": "age", "label": "Age", "role": "confounder", "adjusted": True,
         "snippet": " ".join(["word"] * 40)},
        {"id": "diabetes", "label": "Diabetes", "role": "Confounder", "adjusted": True, "snippet": "diabetes"},
    ],
    "edges": [
        {"source": "age", "target": "sepsis"}, {"source": "age", "target": "ohca"},
        {"source": "ohca", "target": "age"},  # cycle: must be dropped
        {"source": "ghost", "target": "ohca"},  # unknown node: must be dropped
    ],
    "reported_effect": {"measure": "HR", "value": 1.6, "ci": "1.3-1.9", "adjusted": True},
}

CRITIQUE = {
    "missing_confounders": [
        {"label": "Frailty", "rationale": "Frail people get sepsis and arrest more.",
         "edges_to_add": [{"source": "Frailty", "target": "sepsis"}, {"source": "Frailty", "target": "ohca"}]},
        {"label": "Smoking", "rationale": "Not in claims data.", "edges_to_add": []},
    ],
    "bias_risks": [{"type": "selection bias", "explanation": "Survivors only.", "severity": "HIGH"}],
    "overall_assessment": "Moderate.",
}


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db.reset_for_tests(str(tmp_path / "t.db"))
    # Safety net: tests must never call the paid model API, even if a .env with keys is present.
    def no_network(*a, **k):
        raise AssertionError("test tried to call the real model API")
    monkeypatch.setattr("app.llm._call", no_network)
    yield


def test_parse_json_handles_think_and_fences():
    assert parse_json('<think>hmm {"x":0}</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}


def test_schema_normalises_messy_model_output():
    ex = Extraction.model_validate(EXTRACTION)
    assert ex.study_design == "cohort"
    assert ex.nodes[3].role == "confounder"
    assert len(ex.nodes[2].snippet.split()) <= 21  # truncated to 20 words + ellipsis
    assert ex.reported_effect.value == "1.6"


def test_validate_drops_cycles_and_unknown_edges():
    ex = Extraction.model_validate(EXTRACTION)
    nodes, edges, issues = graph.validate(ex.nodes, ex.edges)
    pairs = {(e.source, e.target) for e in edges}
    assert ("ohca", "age") not in pairs
    assert not any(e.source == "ghost" for e in edges)
    assert any("cycle" in i for i in issues) and any("unknown" in i for i in issues)
    assert not any(n.adjusted for n in nodes if n.role == "exposure")


def test_dagitty_export():
    nodes = [Node(id="e", label="E", role="exposure"), Node(id="o", label="O", role="outcome"),
             Node(id="c", label="C", role="confounder", adjusted=True)]
    edges = [Edge(source="c", target="e"), Edge(source="c", target="o"), Edge(source="e", target="o")]
    txt = graph.to_dagitty(nodes, edges)
    assert "e [exposure]" in txt and "o [outcome]" in txt and "c [adjusted]" in txt
    assert txt.startswith("dag {") and "c -> e" in txt


def test_open_backdoors_flags_unadjusted_common_cause():
    nodes = [Node(id="e", label="E", role="exposure"), Node(id="o", label="O", role="outcome"),
             Node(id="u", label="U", role="confounder", adjusted=False),
             Node(id="m", label="M", role="mediator")]
    edges = [Edge(source="u", target="e"), Edge(source="u", target="o"),
             Edge(source="e", target="m"), Edge(source="m", target="o"), Edge(source="e", target="o")]
    assert graph.open_backdoors(nodes, edges) == ["U"]


def _mock_pipeline(monkeypatch, calls):
    from app import fetch
    from app.schemas import PaperMeta

    meta = PaperMeta(title="Test paper", pmcid="PMC1", doi="10.1/x", license="cc by")
    monkeypatch.setattr(fetch, "fetch_paper", lambda i: (meta, {"abstract": "abs", "methods": "m"}))
    monkeypatch.setattr(pipeline, "extract", lambda t: (calls.append("x") or Extraction.model_validate(EXTRACTION), {}))
    monkeypatch.setattr(pipeline, "critique", lambda ex, a: (calls.append("c") or Critique.model_validate(CRITIQUE), {}))
    monkeypatch.setattr("app.evidence.search", lambda q, max_results=5: [{"title": "t", "url": "https://example.org"}])
    monkeypatch.setattr("app.canonical.resolve_record", lambda rec: 0)  # no real model calls in tests


def test_full_pipeline_review_and_approve(monkeypatch):
    calls = []
    _mock_pipeline(monkeypatch, calls)
    rec = pipeline.run("PMC1")
    ids = {n["id"] for n in rec["dag"]["nodes"]}
    assert {"sepsis", "ohca", "age", "diabetes", "frailty", "smoking"} <= ids
    assert all(n["ai_suggested"] for n in rec["dag"]["nodes"] if n["id"] in ("frailty", "smoking"))
    # diabetes had no edges from the model; core edges get added
    assert {"source": "diabetes", "target": "sepsis", "ai_suggested": False} in rec["dag"]["edges"]
    assert rec["critique"]["missing_confounders"][0]["evidence"][0]["url"] == "https://example.org"
    assert rec["critique"]["bias_risks"][0]["severity"] == "high"
    assert "Frailty" in rec["open_backdoors"]

    # cached: second run makes no model calls
    pipeline.run("PMC1")
    assert calls == ["x", "c"]

    with pytest.raises(ValueError):
        pipeline.approve(rec)
    rec = pipeline.update(rec, rec["dag"]["nodes"], rec["dag"]["edges"],
                          {"Frailty": "accepted", "Smoking": "rejected"})
    ids = {n["id"] for n in rec["dag"]["nodes"]}
    assert "smoking" not in ids and "frailty" in ids
    assert not any(e["source"] == "smoking" for e in rec["dag"]["edges"])
    rec = pipeline.approve(rec)
    assert rec["status"] == "approved" and rec["approved_at"]
    assert db.list_records("approved")[0]["id"] == rec["id"]


@pytest.mark.network
def test_fetch_real_open_access_paper():
    from app import fetch
    meta, sections = fetch.fetch_paper("10.1136/bmjopen-2021-051502")
    assert meta.pmcid == "PMC8442105" and meta.license.startswith("cc")
    assert "methods" in sections and "statistics" in sections
    with pytest.raises(fetch.FetchError):
        fetch.fetch_paper("10.1056/NEJMoa2034577")  # free to read but no open licence


def test_evidence_drops_source_paper_and_duplicates():
    from app.evidence import clean
    paper = {"title": "Risk of OHCA among sepsis survivors in Taiwan", "doi": "10.1136/bmjopen-2021-051502", "pmcid": "PMC8442105"}
    results = [
        {"title": "Risk of OHCA among sepsis survivors in Taiwan", "url": "https://bmjopen.bmj.com/x"},
        {"title": "Something", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC8442105/"},
        {"title": "Frailty and sepsis", "url": "https://a.org/1"},
        {"title": "Frailty and sepsis", "url": "https://b.org/2"},
        {"title": "Renal function after arrest", "url": "https://c.org/3"},
    ]
    assert [r["url"] for r in clean(results, paper)] == ["https://a.org/1", "https://c.org/3"]


def test_insights_groups_synonyms_and_builds_forest():
    from app.export import insights
    def rec(i, measure, value, ci, labels, risks):
        return {"id": i, "paper": {"title": f"P{i}"},
                "study": {"exposure": {"name": "PPIs"}, "outcome": {"name": "CKD"}, "study_design": "cohort",
                          "reported_effect": {"measure": measure, "value": value, "ci": ci, "adjusted": True}},
                "critique": {"missing_confounders": [{"label": l, "status": "accepted"} for l in labels],
                             "bias_risks": [{"type": t, "severity": "high"} for t in risks]}}
    out = insights([
        rec("a", "HR", "1.66", "1.27-2.16", ["Socioeconomic status (income/urbanization)", "Smoking history"], ["Immortal time bias"]),
        rec("b", "OR", 0.9, "0.7 to 1.2", ["Household income", "Frailty"], ["confounding by indication"]),
        rec("c", "beta", "-0.3", "-0.5, -0.1", ["Income"], []),          # not a ratio: skipped from forest
    ])
    assert [f["id"] for f in out["forest"]] == ["b", "a"] and out["forest_skipped"] == 1
    top = out["missing_confounders"][0]
    assert top["label"] == "Socioeconomic status" and top["count"] == 3
    assert {b["type"] for b in out["bias_risks"]} == {"Immortal time bias", "Confounding by indication"}


def test_network_merges_variables_across_papers():
    from app.export import network
    def rec(i, exp, out, conf):
        return {"id": i, "paper": {"title": i},
                "dag": {"nodes": [{"id": "e", "label": exp, "role": "exposure"},
                                  {"id": "o", "label": out, "role": "outcome"},
                                  {"id": "c", "label": conf, "role": "confounder", "adjusted": True}],
                        "edges": [{"source": "c", "target": "e"}, {"source": "c", "target": "o"},
                                  {"source": "e", "target": "o"}]}}
    net = network([rec("p1", "PPI use", "All-cause mortality", "Age (years)"),
                   rec("p2", "Proton pump inhibitors", "Hip fracture", "age at baseline")])
    nodes = {n["id"]: n for n in net["nodes"]}
    assert nodes["Age"]["n_papers"] == 2 and nodes["Age"]["group"] == "confounder"
    assert nodes["PPI use"]["n_papers"] == 2 and nodes["PPI use"]["group"] == "exposure"
    link = next(l for l in net["links"] if l["source"] == "Age" and l["target"] == "PPI use")
    assert link["n_papers"] == 2


def test_canonical_mapping_used_by_network(monkeypatch):
    from app import canonical
    from app.export import network
    calls = []

    def fake_chat(model, system, prompt, max_tokens, thinking=False):
        calls.append(prompt)
        return {"mappings": [
            {"label": "Townsend deprivation index", "canonical": "Socioeconomic status", "mesh": "Social Class"},
            {"label": "household income", "canonical": "Socioeconomic status (SES)", "mesh": None},
            {"label": "made-up label", "canonical": "Nonsense"},       # not an input: must be ignored
        ]}, {}
    monkeypatch.setattr(canonical, "chat_json", fake_chat)
    monkeypatch.setattr(canonical, "verify_mesh", lambda t: (t, "D012923") if t == "Social Class" else None)
    assert canonical.resolve(["Townsend deprivation index", "household income", "Household income "]) == 2
    assert canonical.resolve(["household income"]) == 0 and len(calls) == 1   # stored: no second call

    def rec(i, label):
        return {"id": i, "paper": {"title": i}, "dag": {"nodes": [
            {"id": "e", "label": "X", "role": "exposure"}, {"id": "c", "label": label, "role": "confounder", "adjusted": True}],
            "edges": [{"source": "c", "target": "e"}]}}
    nodes = {n["id"]: n for n in network([rec("p1", "Townsend deprivation index"), rec("p2", "household income")])["nodes"]}
    ses = nodes["Socioeconomic status"]
    assert ses["n_papers"] == 2 and ses["merged_by"] == "nemotron" and ses["mesh"] == "Social Class"
    assert ses["aliases"] == ["household income", "Townsend deprivation index"]


def _ask_records():
    def rec(i, smoking_adjusted, title):
        nodes = [{"id": "age", "label": "Age", "adjusted": True}]
        if smoking_adjusted:
            nodes.append({"id": "smk", "label": "Smoking status", "adjusted": True})
        return {"id": i, "status": "approved", "paper": {"title": title, "year": "2020"},
                "study": {"exposure": {"name": "PPI"}, "outcome": {"name": "CKD"}, "study_design": "cohort",
                          "reported_effect": {"measure": "HR", "value": "1.2", "ci": "1.0-1.4", "adjusted": True}},
                "dag": {"nodes": nodes, "edges": []},
                "critique": {"missing_confounders": [{"label": "Smoking", "status": "accepted"}], "bias_risks": []}}
    return [rec("aaaaaa1111", False, "No smoking adj"), rec("bbbbbb2222", True, "Smoking adj")]


def test_ask_filters_exactly_and_strips_invented_citations(monkeypatch):
    from app import ask
    calls = []
    def fake_chat(model, system, prompt, max_tokens, thinking=False):
        calls.append(system)
        if system == ask.PLAN_SYSTEM:   # the planner asks for "did not adjust for smoking"
            return {"mode": "filter", "aggregate": None,
                    "filters": [{"field": "not_adjusted", "terms": ["smok", "tobacco"]}]}, {}
        assert "aaaaaa1111" in prompt and "bbbbbb2222" not in prompt.split("EXACT RESULTS:")[1].split("NEAR")[0]
        return {"answer": "One paper [aaaaaa1111], not [deadbeef99].", "cited": ["deadbeef99"]}, {}
    monkeypatch.setattr(ask, "chat_json", fake_chat)
    out, cached = ask.answer("Which papers did not adjust for smoking?", _ask_records())
    assert [c["id"] for c in out["cited"]] == ["aaaaaa1111"] and "deadbeef99" not in out["answer"]
    assert "→ 1 match" in out["method"] and not cached
    _, cached = ask.answer("which papers did   not adjust for smoking?", _ask_records())  # normalised: cached
    assert cached and len(calls) == 2


def test_ask_ignores_generic_filters():
    from app import ask
    assert not ask._useful({"field": "missing", "terms": ["confounders", "missing"]})
    assert ask._useful({"field": "bias", "terms": ["confound"], "severity": "high"})  # becomes "any bias rated high"


def test_ask_endpoint_rate_limit(monkeypatch):
    from fastapi.testclient import TestClient
    from app import ask, config, main
    monkeypatch.setattr(config, "ASK_PER_HOUR", 1)
    monkeypatch.setattr(db, "list_records", lambda status=None: [{"id": "x"}])
    monkeypatch.setattr(ask, "answer", lambda q, recs: ({"answer": "ok", "cited": [], "n_papers": 1, "model": "m"}, False))
    client = TestClient(main.app)
    assert client.post("/api/ask", json={"question": "hello there"}).status_code == 200
    assert client.post("/api/ask", json={"question": "hello again"}).status_code == 429
