"""Trap-profile benchmark: our recommender vs a single-factor 'lowest skill' baseline.

    python -m eval.run_benchmark          # rules-only (no API key needed)
    python -m eval.run_benchmark --ai     # use the AI layer if OPENAI_API_KEY is set

Loads the starter kit, uploads eval/trap_*.{json,csv} exactly like the jury would, and checks each
profile's expectations in eval/expectations.json.
"""
import json
import sys
from pathlib import Path

from app import agent, engine
from app.config import DATA_DIR
from app.data import GRADES, load_store

EVAL = Path(__file__).resolve().parent


def baseline(store, emp_id: str) -> list[str]:
    """Naive rule: lowest *recorded* skill among the own-role next-grade requirements → first event that develops it.
    Ignores history, career goal, post-review completions, prerequisites and max_level caps."""
    emp = store.employees[emp_id]
    grade = GRADES[min(GRADES.index(emp.grade) + 1, len(GRADES) - 1)]
    req = store.role_profiles[(emp.role, grade)].required_skills
    skill = min(req, key=lambda s: (emp.skills.get(s, 0), s))
    for ev in store.events.values():
        if not ev.mandatory and emp.role in ev.target_roles and any(g.skill_id == skill for g in ev.develops_skills):
            return [ev.event_id]
    return []


def check(store, steps: list[str], exp: dict) -> tuple[bool, str]:
    if not steps:
        return False, "no steps"
    dev = lambda e: {g.skill_id for g in store.events[e].develops_skills}
    if "top1_in" in exp and steps[0] not in exp["top1_in"]:
        return False, f"top-1 {steps[0]} not in {exp['top1_in']}"
    if "top1_develops_any" in exp and not dev(steps[0]) & set(exp["top1_develops_any"]):
        return False, f"top-1 {steps[0]} does not develop the key skill"
    if bad := set(steps) & set(exp.get("must_exclude", [])):
        return False, f"recommended {sorted(bad)}"
    if missing := set(exp.get("must_include", [])) - set(steps):
        return False, f"missing {sorted(missing)}"
    if "must_not_target" in exp and any(dev(e) & set(exp["must_not_target"]) for e in steps[:1]):
        return False, f"top-1 targets {exp['must_not_target']}"
    return True, "ok"


def main(use_ai: bool) -> int:
    store = load_store(DATA_DIR)
    store.merge_employees(json.loads((EVAL / "trap_employees.json").read_text()))
    store.merge_history((EVAL / "trap_history.csv").read_text())
    expectations = json.loads((EVAL / "expectations.json").read_text())
    ours_pass = base_pass = 0
    print("| Profile | Trap | Baseline | Career Quest |\n|---|---|---|---|")
    for emp_id, exp in expectations.items():
        b = baseline(store, emp_id)
        b_ok, b_why = check(store, b, exp)
        r = agent.recommend(store, emp_id, use_ai=use_ai)
        ours = [s["event_id"] for s in r["steps"]]
        o_ok, o_why = check(store, ours, exp)
        ours_pass += o_ok
        base_pass += b_ok
        print(f"| {emp_id} | {exp['trap']} | {'✅' if b_ok else '❌'} {','.join(b) or '—'} ({b_why}) "
              f"| {'✅' if o_ok else '❌'} {','.join(ours)} ({o_why}; {r['mode']}) |")
    n = len(expectations)
    print(f"\nBaseline {base_pass}/{n} · Career Quest {ours_pass}/{n}")
    return 0 if ours_pass == n else 1


if __name__ == "__main__":
    sys.exit(main("--ai" in sys.argv))
