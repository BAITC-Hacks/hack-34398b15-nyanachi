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
