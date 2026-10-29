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
    monkeypatch.setattr("app.config.TAVILY_API_KEY", "")   # no real Tavily usage lookups either
    monkeypatch.setattr("app.budget.SPENT_BEFORE_LEDGER_USD", 0.0)
    monkeypatch.setattr("app.budget.BUDGET_USD", 100.0)
    monkeypatch.setattr("app.budget.REDIS_URL", "")       # tests use the local ledger, never a real one
    monkeypatch.setattr("app.budget.REQUIRE_DURABLE", False)
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
    monkeypatch.setattr("app.verify.verify", lambda crit, text: (crit, [], {}))


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


def test_autoreview_labels_record_and_human_can_rereview(monkeypatch):
    from fastapi.testclient import TestClient
    from app import autoreview, main
    calls = []
    _mock_pipeline(monkeypatch, calls)
    rec = pipeline.run("PMC1")
    monkeypatch.setattr(autoreview, "chat_json", lambda *a, **k: ({"decisions": [
        {"label": "Frailty", "decision": "accept", "reason": "Common cause."},
        {"label": "Smoking", "decision": "reject", "reason": "Already a proxy."}]}, {}))
    rec = autoreview.review_and_approve(rec)
    assert rec["status"] == "approved" and rec["review"]["by"] == "ai"
    notes = {m["label"]: m["review_note"] for m in rec["critique"]["missing_confounders"]}
    assert notes == {"Frailty": "Common cause.", "Smoking": "Already a proxy."}
    assert "smoking" not in {n["id"] for n in rec["dag"]["nodes"]}

    client = TestClient(main.app)   # a person edits the AI-reviewed record, then approves it
    body = {"nodes": rec["dag"]["nodes"], "edges": rec["dag"]["edges"], "suggestion_status": {"Frailty": "rejected", "Smoking": "rejected"}}
    assert client.put(f"/api/records/{rec['id']}", json=body).json()["status"] == "draft"
    approved = client.post(f"/api/records/{rec['id']}/approve").json()
    assert approved["review"]["by"] == "human"
    assert client.put(f"/api/records/{rec['id']}", json=body).status_code == 409   # human-approved: locked


def _usage(p, c):
    from types import SimpleNamespace
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c)


def test_budget_records_real_usage_and_blocks_at_limit(monkeypatch):
    from app import budget
    monkeypatch.setattr(budget, "BUDGET_USD", 1.0)
    model = "nvidia/nemotron-3-super-120b-a12b"          # $0.30 in / $0.90 out per million
    budget.record_llm(model, _usage(1_000_000, 500_000), 1000, [])
    assert abs(budget.spent() - 0.75) < 1e-9             # 0.30 + 0.45, from the reported tokens
    budget.guard_llm(model, [{"content": "x" * 300}], 100)   # tiny call still fits
    try:
        budget.guard_llm(model, [{"content": "x"}], 300_000)  # worst case 0.27 would pass $1
        assert False, "should have refused"
    except budget.BudgetExceeded as e:
        assert "budget" in str(e).lower()


def test_unknown_model_is_priced_high(monkeypatch):
    from app import budget
    assert budget.price("some/typo-model") == budget.UNKNOWN_MODEL_PRICE
    assert budget.UNKNOWN_MODEL_PRICE[1] > budget.price("nvidia/nemotron-3-super-120b-a12b")[1]


def test_budget_fails_closed(monkeypatch):
    from app import budget
    monkeypatch.setattr(budget, "REQUIRE_DURABLE", True)   # e.g. on Render without the durable ledger
    try:
        budget.check(0.0)
        assert False
    except budget.BudgetExceeded:
        pass
    monkeypatch.setattr(budget, "REQUIRE_DURABLE", False)
    monkeypatch.setattr(budget, "REDIS_URL", "https://example.invalid")
    monkeypatch.setattr(budget, "REDIS_TOKEN", "t")
    def boom(cmd):
        raise ConnectionError("down")
    monkeypatch.setattr(budget, "_redis", boom)             # ledger unreachable: refuse, don't guess
    try:
        budget.check(0.0)
        assert False
    except budget.BudgetExceeded as e:
        assert "unavailable" in str(e)


def test_durable_ledger_and_real_tavily_spend(monkeypatch):
    from app import budget, config
    store = {"v": 0.0}
    def fake_redis(cmd):
        if cmd[0] == "INCRBYFLOAT":
            store["v"] += float(cmd[2]); return str(store["v"])
        return str(store["v"])
    monkeypatch.setattr(budget, "REDIS_URL", "https://example.invalid")
    monkeypatch.setattr(budget, "REDIS_TOKEN", "t")
    monkeypatch.setattr(budget, "_redis", fake_redis)
    budget.record_llm("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", _usage(1_000_000, 1_000_000), 10, [])
    assert abs(store["v"] - 0.30) < 1e-9                   # 0.06 + 0.24 went to the durable ledger
    monkeypatch.setattr(config, "TAVILY_API_KEY", "k")
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"account": {"paygo_usage": 250}}   # 250 paid searches this cycle
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: R())
    budget._tavily_cache.update(usd=None, at=0.0)
    assert abs(budget.spent() - (0.30 + 250 * 0.008)) < 1e-9


def test_submit_refused_when_budget_used_up(monkeypatch):
    from fastapi.testclient import TestClient
    from app import budget, main, pipeline as pl
    monkeypatch.setattr(pl, "lookup_cached", lambda i: None)
    monkeypatch.setattr(budget, "BUDGET_USD", 0.05)
    r = TestClient(main.app).post("/api/submit", json={"identifier": "PMC1"})
    assert r.status_code == 503 and "budget" in r.json()["detail"].lower()
    st = TestClient(main.app).get("/api/budget").json()
    assert st["budget_usd"] == 0.05 and st["ledger"] == "local"


def test_verify_moves_confirmed_suggestions_into_dag(monkeypatch):
    from app import verify
    from app.schemas import Critique, Node
    text = "Model 2 was adjusted for age, sex, ethnicity, education, household income and smoking status."
    crit = Critique.model_validate({"missing_confounders": [{"label": "Education level"}, {"label": "NSAID use"},
                                                            {"label": "Frailty"}]})
    monkeypatch.setattr(verify, "chat_json", lambda *a, **k: ({"checks": [
        {"label": "Education level", "adjusted": True, "quote": "adjusted for age, sex, ethnicity, education, household income"},
        {"label": "NSAID use", "adjusted": False, "quote": ""},
        {"label": "Frailty", "adjusted": True, "quote": "adjusted for frailty using the electronic index"},  # invented quote
    ]}, {}))
    kept, found, _ = verify.verify(crit, text)
    assert [m.label for m in kept.missing_confounders] == ["NSAID use", "Frailty"]   # invented quote is ignored
    assert found == [{"label": "Education level", "quote": "adjusted for age, sex, ethnicity, education, household income"}]
    nodes = [Node(id="e", label="E", role="exposure"), Node(id="o", label="O", role="outcome")]
    nodes, edges = verify.add_confirmed(nodes, [], found)
    ed = next(n for n in nodes if n.id == "education_level")
    assert ed.adjusted and not ed.ai_suggested and "education" in ed.snippet
    assert {(e.source, e.target) for e in edges} == {("education_level", "e"), ("education_level", "o")}


def test_llm_retries_when_upstream_is_overloaded(monkeypatch):
    import httpx as _httpx
    from openai import InternalServerError
    from app import llm
    calls = []
    class Resp:
        usage = None
        class _M: content = '{"ok": true}'
        choices = [type("C", (), {"message": _M})()]
    def create(**kw):
        calls.append(1)
        if len(calls) < 3:
            req = _httpx.Request("POST", "https://x"); raise InternalServerError("overloaded", response=_httpx.Response(529, request=req), body=None)
        return Resp()
    fake = type("Cl", (), {"chat": type("Ch", (), {"completions": type("Co", (), {"create": staticmethod(create)})})})
    monkeypatch.setattr(llm, "client", lambda: fake)
    monkeypatch.setattr(llm, "RETRY_WAITS", (0, 0))
    assert llm._create(model="m", messages=[{"content": "hi"}], max_tokens=5) is not None and len(calls) == 3


def test_verify_needs_an_adjustment_sentence():
    from app import verify
    text = "Participants reported regular physical activity at baseline. " + "x " * 200 + \
           "Model 2 was adjusted for ethnicity, education and household income."
    assert not verify.shows_adjustment("reported regular physical activity at baseline", text)   # passing mention
    assert verify.shows_adjustment("Model 2 was adjusted for ethnicity, education", text)


def test_settings_ignore_inline_comments(monkeypatch):
    from app import config
    monkeypatch.setenv("ADMIN_TOKEN", "                  # optional: bypasses rate limits")
    assert config._str("ADMIN_TOKEN") == ""      # a comment must never become the admin token
    monkeypatch.setenv("X_SETTING", "abc   # note")
    assert config._str("X_SETTING") == "abc"
    monkeypatch.setenv("X_URL", "https://h.example/a#frag")   # a '#' without a space before it is kept
    assert config._str("X_URL") == "https://h.example/a#frag"


def test_verify_falls_back_to_the_papers_own_sentence(monkeypatch):
    from app import verify
    from app.schemas import Critique
    text = ("We recruited adults. Model 2 was adjusted for ethnicity, education, household income and regular use of "
            "non-aspirin non-steroidal anti-inflammatory drugs [NSAIDs]. Renal function was measured at baseline.")
    crit = Critique.model_validate({"missing_confounders": [{"label": "Education level"}, {"label": "NSAID use"},
                                                            {"label": "Renal function"}]})
    monkeypatch.setattr(verify, "chat_json", lambda *a, **k: ({"checks": [
        {"label": "Education level", "adjusted": True, "quote": "education"},          # too short to check
        {"label": "NSAID use", "adjusted": True, "quote": "use of NSAID medicines"},     # paraphrased
        {"label": "Renal function", "adjusted": True, "quote": "renal function"},        # measured, not adjusted
    ]}, {}))
    kept, found, _ = verify.verify(crit, text)
    assert {f["label"] for f in found} == {"Education level", "NSAID use"}
    assert all(f["quote"].startswith("Model 2 was adjusted") for f in found)
    assert [m.label for m in kept.missing_confounders] == ["Renal function"]
