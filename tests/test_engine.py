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
