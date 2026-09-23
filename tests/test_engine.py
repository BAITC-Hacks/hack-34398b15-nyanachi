from pathlib import Path

from app import engine
from app.data import load_store

S = load_store(Path(__file__).resolve().parent.parent / "data")


def test_loads_full_dataset():
    assert (len(S.employees), len(S.events), len(S.history)) == (200, 40, 2743)


def test_effective_skills_include_post_review_completions():
    e = S.employees["E0028"]
    levels, applied = engine.effective_skills(S, e)
    assert applied and all(levels[a["skill_id"]] >= a["to"] for a in applied)


def test_never_recommends_mandatory_or_completed():
    for emp_id in list(S.employees)[:50]:
        c = engine.candidates(S, emp_id)
        done = {h.event_id for h in S.history_of(emp_id) if h.status == "completed"}
        for x in c["candidates"]:
            assert not S.events[x["event_id"]].mandatory
            assert x["event_id"] not in done or x["event_id"] in engine.REPEATABLE


def test_gain_capped_by_max_level():
    for emp_id in list(S.employees)[:50]:
        for x in engine.candidates(S, emp_id)["candidates"]:
            ev = S.events[x["event_id"]]
            caps = {g.skill_id: g.max_level for g in ev.develops_skills}
            assert all(g["to"] <= max(caps[g["skill_id"]], g["from"]) for g in x["gains"])


def test_prerequisite_chain_unlocks_blocked_event():
    c = engine.candidates(S, "E0001")
    step = next(x for x in c["candidates"] if x["event_id"] == "EV_005")
    assert step["unlocks"]["event_id"] == "EV_006"
    assert step["factors"]["unlock_bonus"] > 0
    assert any(b["event_id"] == "EV_006" for b in c["blocked"])


def test_path_simulation_moves_readiness_and_leaves_store_untouched():
    n = len(S.history)
    p = engine.simulate_path(S, "E0001")
    assert len(S.history) == n
    readiness = [p["readiness_now"]] + [s["readiness_after"] for s in p["steps"]]
    assert all(b > a for a, b in zip(readiness, readiness[1:]))
    assert len({s["event_id"] for s in p["steps"]}) == len(p["steps"])


def test_upload_keeps_rows_whose_record_ids_restart_from_one():
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    e = s.employees["E0028"].model_dump()
    e["employee_id"] = "E9999"
    s.merge_employees({"employees": [e]})
    csv_text = ("record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\n"
                "R000001,E9999,EV_036,2026-05-01,,no_show,0,,,self\n")
    assert s.merge_history(csv_text) == 1
    assert s.merge_history(csv_text) == 0  # exact duplicate is ignored


def test_mentors_are_senior_colleagues_in_same_department_without_leaking_levels():
    emp = S.employees["E0028"]
    res = engine.find_mentors(S, "E0028")
    for m in res["mentors"]:
        mentor = S.employees[m["employee_id"]]
        assert mentor.department == emp.department and mentor.grade in ("Senior", "Lead")
        assert "skills" not in m and "level" not in m


def test_points_only_for_voluntary_and_challenge_bonus():
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    w0 = engine.wallet(s, "E0001")
    assert all(not s.events[x["event_id"]].mandatory for x in w0["recent"] if x.get("event_id"))
    offer = w0["challenge_offer"]
    assert offer and offer["skill_id"]
    engine.accept_challenge(s, "E0001")
    # walk the path until the challenge skill reaches its target
    for step in engine.simulate_path(s, "E0001")["steps"]:
        res = engine.complete_event(s, "E0001", step["event_id"])
        engine.award_completion(s, "E0001", step["event_id"], res["changed"])
        if s.challenges["E0001"][0]["status"] == "completed":
            break
    w1 = engine.wallet(s, "E0001")
    assert s.challenges["E0001"][0]["status"] == "completed"
    assert w1["balance"] > w0["balance"] + engine.CHALLENGE_BONUS
    engine.redeem(s, "E0001", "RW_BOOK")
    assert engine.wallet(s, "E0001")["balance"] == w1["balance"] - 60


def test_garden_stage_matches_skill_and_sharing_is_mutual_opt_in():
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    g = engine.garden(s, "E0028")
    levels, _ = engine.effective_skills(s, s.employees["E0028"])
    assert all(p["stage"] == min(5, levels.get(p["skill_id"], 0)) for p in g["plants"])
    colleague = next(e.employee_id for e in s.employees.values()
                     if e.department == s.employees["E0028"].department and e.employee_id != "E0028")
    engine.set_share(s, colleague, True)
    assert engine.garden(s, "E0028")["neighbours"] == []          # I have not opted in yet
    engine.set_share(s, "E0028", True)
    assert [n["employee_id"] for n in engine.garden(s, "E0028")["neighbours"]] == [colleague]
    assert "plants" in engine.garden(s, "E0028")["neighbours"][0] and "skills" not in engine.garden(s, "E0028")["neighbours"][0]


def test_navigator_tools_answer_from_engine_for_one_employee():
    from app.chat import Tools, TOOLS
    t = Tools(S, "E0028")
    assert {x["name"] for x in TOOLS} == {"get_overview", "list_recommendations", "skill_options",
                                          "explain_activity", "simulate_path", "find_mentors"}
    assert t.get_overview()["target"].endswith("Senior")
    ps = t.skill_options("Public Speaking")
    assert ps["skill"] == "Public Speaking" and ps["activities"]
    assert t.explain_activity("Kubernetes in Practice")["activity"] == "Kubernetes in Practice"
    assert "error" in t.skill_options("Underwater Basket Weaving")


def test_not_now_hides_activity_and_format_feedback_lowers_that_format():
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    before = engine.candidates(s, "E0028")
    first = before["candidates"][0]
    fmt_before = before["signals"]["format"][first["format"]]["rate"]
    engine.dismiss(s, "E0028", first["event_id"], "format")
    after = engine.candidates(s, "E0028")
    assert first["event_id"] not in {x["event_id"] for x in after["candidates"]}
    assert after["signals"]["format"][first["format"]]["rate"] < fmt_before


def test_api_requires_signed_tokens_and_enforces_roles():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/hr/summary").status_code == 401
    assert c.get("/api/hr/summary", headers={"X-Role": "hr"}).status_code == 401
    assert c.post("/api/login", json={"role": "hr", "password": "wrong"}).status_code == 401
    emp = c.post("/api/login", json={"role": "employee", "employee_id": "E0028"}).json()["token"]
    hdr = {"Authorization": f"Bearer {emp}"}
    assert c.get("/api/employees/E0028", headers=hdr).status_code == 200
    assert c.get("/api/employees/E0001", headers=hdr).status_code == 403
    assert c.get("/api/hr/summary", headers=hdr).status_code == 403
    forged = emp.split(".")[0].replace("RTAwMjg", "RTAwMDE") + "." + emp.split(".")[1]
    assert c.get("/api/employees/E0001", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    hr = c.post("/api/login", json={"role": "hr", "password": "hr-demo"}).json()["token"]
    assert c.get("/api/hr/summary", headers={"Authorization": f"Bearer {hr}"}).status_code == 200


def test_ai_cache_is_invalidated_when_the_profile_changes(monkeypatch):
    from app import agent, config
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    calls = []
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test")
    def fake_call(model, payload, lang):
        calls.append(model)
        c = payload["candidates"][0]["event_id"]
        return {"steps": [{"event_id": c, "rationale": "x", "factors_used": ["skill_gap", "expected_gain", "participation_history"]}],
                "why_not": "", "summary": ""}
    monkeypatch.setattr(agent, "_call", fake_call)
    first = agent.recommend(s, "E0001", True)
    n = len(calls)
    assert agent.recommend(s, "E0001", True).get("cached") and len(calls) == n
    engine.complete_event(s, "E0001", first["steps"][0]["event_id"])
    assert not agent.recommend(s, "E0001", True).get("cached")


def test_hr_event_builder_closes_a_catalogue_gap():
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    gap = engine.hr_summary(s)["catalog_gaps"][0]
    draft = engine.event_draft(s, gap["skill_id"])
    assert draft["blocked_employees"] == gap["employees"]
    res = engine.create_event(s, draft)
    assert res["now_recommendable_for"] > 0
    after = {g["skill_id"]: g["employees"] for g in engine.hr_summary(s)["catalog_gaps"]}
    assert after.get(gap["skill_id"], 0) < gap["employees"]


def test_support_signals_are_explained_and_hr_only():
    rows = engine.support_signals(S)
    assert rows and all(len(r["reasons"]) >= 2 and r["suggested_actions"] for r in rows)
    assert all("score" not in r for r in rows)


def test_kudos_same_department_limited_and_private_points():
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    me = s.employees["E0028"]
    mates = [e.employee_id for e in s.employees.values() if e.department == me.department and e.employee_id != "E0028"]
    other = next(e.employee_id for e in s.employees.values() if e.department != me.department)
    before = engine.wallet(s, mates[0])["balance"]
    engine.send_kudos(s, "E0028", mates[0], "thanks")
    assert engine.wallet(s, mates[0])["balance"] == before + engine.KUDOS_POINTS
    import pytest
    with pytest.raises(ValueError):
        engine.send_kudos(s, "E0028", other, "x")
    with pytest.raises(ValueError):
        engine.send_kudos(s, "E0028", "E0028", "x")
    engine.send_kudos(s, "E0028", mates[1], "x"); engine.send_kudos(s, "E0028", mates[2], "x")
    with pytest.raises(ValueError):
        engine.send_kudos(s, "E0028", mates[3], "x")
    t = engine.team_goal(s, me.department)
    assert t["goal"] == len(mates) + 1 and t["done"] >= 0


def test_every_employee_gets_valid_explainable_deterministic_steps():
    from app import agent
    from app.data import load_store as _load
    import json as _json
    s = _load(Path(__file__).resolve().parent.parent / "data")
    ev_dir = Path(__file__).resolve().parent.parent / "eval"
    s.merge_employees(_json.loads((ev_dir / "trap_employees.json").read_text()))
    s.merge_history((ev_dir / "trap_history.csv").read_text())
    for emp_id in s.employees:
        c = engine.candidates(s, emp_id)
        r1 = agent.recommend(s, emp_id, use_ai=False)
        r2 = agent.recommend(s, emp_id, use_ai=False)
        assert [x["event_id"] for x in r1["steps"]] == [x["event_id"] for x in r2["steps"]]   # deterministic
        assert len(r1["steps"]) <= 3
        done = {h.event_id for h in s.history_of(emp_id) if h.status == "completed"}
        useful_exists = any(engine.is_useful(x) for x in c["candidates"])
        for st in r1["steps"]:
            ev = s.events[st["event_id"]]
            assert not ev.mandatory
            assert st["event_id"] not in done or st["event_id"] in engine.REPEATABLE
            assert engine.eligibility(s, c["employee"], ev, c["levels"], c["target"]) is None
            assert not useful_exists or engine.is_useful(st)
            assert len(st["factors_used"]) >= 3, (emp_id, st["event_id"], st["factors_used"])
            assert set(st["factors_used"]) <= engine.supported_factors(st, c)


def test_ai_validator_drops_false_factor_claims(monkeypatch):
    from app import agent, config
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test")
    c = engine.candidates(s, "E0028")
    non_crit = next(x for x in c["candidates"] if engine.is_useful(x) and not x["factors"]["closes_critical_gap"])
    def fake_call(model, payload, lang):   # model claims a critical gap that isn't there, plus 3 true factors
        return {"steps": [{"event_id": non_crit["event_id"], "rationale": "x",
                           "factors_used": ["critical_for_next_grade", "skill_gap", "expected_gain", "participation_history"]}],
                "why_not": "", "summary": ""}
    monkeypatch.setattr(agent, "_call", fake_call)
    r = agent.recommend(s, "E0028", True)
    assert r["mode"] == "ai" and "critical_for_next_grade" not in r["steps"][0]["factors_used"]
    def liar(model, payload, lang):        # only one true factor left after dropping false ones -> fallback to rules
        return {"steps": [{"event_id": non_crit["event_id"], "rationale": "x",
                           "factors_used": ["critical_for_next_grade", "career_goal", "skill_gap"]}], "why_not": "", "summary": ""}
    s.ai_cache.clear()
    monkeypatch.setattr(agent, "_call", liar)
    assert agent.recommend(s, "E0028", True)["mode"] == "rules"


def test_ui_token_header_works_alongside_proxy_basic_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    hr = c.post("/api/login", json={"role": "hr", "password": "hr-demo"}).json()["token"]
    # the reverse proxy keeps its own Basic credentials in Authorization; the app token travels in X-Auth-Token
    r = c.get("/api/hr/summary", headers={"Authorization": "Basic anVkZ2U6eA==", "X-Auth-Token": hr})
    assert r.status_code == 200


def test_ai_cost_uses_measured_usage_and_scales_linearly():
    from app.data import load_store as _load
    s = _load(Path(__file__).resolve().parent.parent / "data")
    base = engine.ai_cost(s)
    assert base["measured"]["calls"] == 0 and base["per_employee_month_usd"]["rules"] == 0
    assert base["per_employee_month_usd"]["luna_only"] < base["per_employee_month_usd"]["normal"] < base["per_employee_month_usd"]["heavy"]
    engine.record_usage(s, "recommendation", "gpt-6-sol", 4000, 600)
    after = engine.ai_cost(s)
    assert after["measured"]["calls"] == 1 and after["avg_tokens"]["recommendation"] == [4000, 600]
    assert after["per_call_usd"]["recommendation"] > base["per_call_usd"]["recommendation"]
