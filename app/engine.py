"""Deterministic core: effective skills, target profile, eligibility, factor scoring, progress, HR stats.

Everything the AI layer says is grounded in numbers computed here.
"""
from collections import Counter, defaultdict

from app.data import GRADES, SKIP_STATUSES, Employee, Event, Store

REPEATABLE = {"EV_036"}
# eligibility reasons that still allow "mark as done" (finishing, attending a past session, changing your mind)
COMPLETABLE = {None, "no_skill_gain_left", "no_upcoming_session", "in_progress", "dismissed_by_employee",
               "declined_repeatedly"}
CRITICAL_WEIGHT = 2.0
CHAIN_WEIGHT = 0.5
AVOID_SKIPS = 3     # this many skips with zero completions in a format = the employee avoids it
AVOID_FACTOR = 0.6  # share of a blocked event's value credited to the step that unlocks it


# ---------- skills & target ----------
def effective_skills(store: Store, emp: Employee) -> tuple[dict[str, int], list[dict]]:
    """Recorded levels + gains from events completed after the last review (not yet assessed)."""
    levels = dict(emp.skills)
    applied = []
    for h in store.history_of(emp.employee_id):
        # completions recorded in the app ("RT…") are always newer than the review; imported ones only if after it
        if h.status != "completed" or (h.date <= emp.last_review_date and not h.record_id.startswith("RT")):
            continue
        for g in store.events[h.event_id].develops_skills:
            cur = levels.get(g.skill_id, 0)
            new = max(cur, min(cur + g.gain, g.max_level))
            if new > cur:
                levels[g.skill_id] = new
                applied.append({"event_id": h.event_id, "date": h.date, "skill_id": g.skill_id, "from": cur, "to": new})
    return levels, applied


def target_profile(store: Store, emp: Employee) -> dict:
    """Next grade of current role, or the career goal if set. Leads without a goal keep their grade."""
    if emp.career_goal and (emp.career_goal.target_role, emp.career_goal.target_grade) in store.role_profiles:
        role, grade, kind = emp.career_goal.target_role, emp.career_goal.target_grade, "career_goal"
    elif GRADES.index(emp.grade) < len(GRADES) - 1:
        role, grade, kind = emp.role, GRADES[GRADES.index(emp.grade) + 1], "next_grade"
    else:
        role, grade, kind = emp.role, emp.grade, "hold_grade"
    rp = store.role_profiles[(role, grade)]
    return {"role": role, "grade": grade, "kind": kind, "required": rp.required_skills, "critical": rp.critical_skills}


def gaps(levels: dict[str, int], target: dict) -> list[dict]:
    out = []
    for sid, req in target["required"].items():
        cur = levels.get(sid, 0)
        if cur < req:
            out.append({"skill_id": sid, "current": cur, "required": req, "gap": req - cur,
                        "critical": sid in target["critical"]})
    return sorted(out, key=lambda g: (not g["critical"], -g["gap"]))


def readiness_breakdown(levels: dict[str, int], target: dict) -> dict:
    rows = [{"skill_id": s, "level": levels.get(s, 0), "required": r, "counted": min(levels.get(s, 0), r),
             "missing": max(0, r - levels.get(s, 0)), "critical": s in target["critical"]}
            for s, r in target["required"].items()]
    got, need = sum(x["counted"] for x in rows), sum(x["required"] for x in rows)
    return {"counted": got, "required": need, "pct": round(100 * got / max(1, need), 1),
            "missing": sorted([x for x in rows if x["missing"]], key=lambda x: (not x["critical"], -x["missing"]))}


def readiness(levels: dict[str, int], target: dict) -> float:
    req = target["required"]
    got = sum(min(levels.get(s, 0), r) for s, r in req.items())
    return round(100 * got / max(1, sum(req.values())), 1)


# ---------- history signals ----------
def history_signals(store: Store, emp_id: str) -> dict:
    rows = store.history_of(emp_id)
    by_format, by_type = defaultdict(Counter), defaultdict(Counter)
    skip_by_skill, feedback_by_type = Counter(), defaultdict(list)
    overdue = sum(1 for h in rows if h.status == "overdue")
    for h in rows:
        ev = store.events[h.event_id]
        if ev.mandatory:
            continue
        by_format[ev.format][h.status] += 1
        by_type[ev.type][h.status] += 1
        if h.status in SKIP_STATUSES:
            for g in ev.develops_skills:
                skip_by_skill[g.skill_id] += 1
        if h.feedback_rating:
            feedback_by_type[ev.type].append(h.feedback_rating)

    # "Not now — wrong format" feedback counts as a skip of that format
    for ev_id, reason in store.dismissed.get(emp_id, {}).items():
        if reason == "format":
            by_format[store.events[ev_id].format]["declined"] += 1

    def rel(c: Counter) -> dict:
        done = c["completed"]
        skipped = sum(c[s] for s in SKIP_STATUSES)
        return {"completed": done, "skipped": skipped, "rate": round((done + 1) / (done + skipped + 2), 2)}

    return {
        "format": {f: rel(c) for f, c in by_format.items()},
        "type": {t: rel(c) for t, c in by_type.items()},
        "skips_by_skill": dict(skip_by_skill),
        "feedback_by_type": {t: round(sum(v) / len(v), 1) for t, v in feedback_by_type.items()},
        "overdue_mandatory": overdue,
    }


# ---------- eligibility ----------
def eligibility(store: Store, emp: Employee, ev: Event, levels: dict[str, int], target: dict,
                ignore_prereq: bool = False) -> str | None:
    """Return a reason code if the event can't be recommended, else None."""
    if ev.mandatory:
        return "mandatory"
    if ev.type == "onboarding" and emp.tenure_months > 1:
        return "onboarding_only_for_new_hires"
    rows = [h for h in store.history_of(emp.employee_id) if h.event_id == ev.event_id]
    statuses = {h.status for h in rows}
    if "completed" in statuses and ev.event_id not in REPEATABLE:
        return "already_completed"
    if any(h.status == "completed" and h.date >= store.as_of for h in rows):
        return "already_completed_today"          # repeatable club: once per day
    if sum(h.status == "declined" for h in rows) >= 2:
        return "declined_repeatedly"              # voluntariness: stop re-offering what was refused twice
    if "in_progress" in statuses:
        return "in_progress"
    if ev.event_id in store.dismissed.get(emp.employee_id, {}):
        return "dismissed_by_employee"
    own_fit = emp.role in ev.target_roles and (emp.grade in ev.target_grades or target["grade"] in ev.target_grades)
    # Career switchers need the new role's fundamentals: any grade up to the target grade in the goal role fits.
    switch_fit = (target["role"] != emp.role and target["role"] in ev.target_roles
                  and any(GRADES.index(g) <= GRADES.index(target["grade"]) for g in ev.target_grades))
    goal_fit = target["role"] in ev.target_roles and target["grade"] in ev.target_grades
    if not (own_fit or switch_fit or goal_fit):
        return "audience_mismatch"
    for sid, need in ev.prerequisites.items():
        if levels.get(sid, 0) < need and not ignore_prereq:
            return f"prerequisite_{sid}"
    if ev.format != "self_paced" and not any(d >= store.as_of for d in ev.upcoming_sessions):
        return "no_upcoming_session"
    if not any(min(levels.get(g.skill_id, 0) + g.gain, g.max_level) > levels.get(g.skill_id, 0) for g in ev.develops_skills):
        return "no_skill_gain_left"
    return None


# ---------- scoring ----------
def _days(a: str, b: str) -> int:
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def score_event(store: Store, ev: Event, levels: dict[str, int], target: dict, sig: dict) -> dict:
    gains, gap_points, beyond = [], 0.0, 0.0
    for g in ev.develops_skills:
        cur = levels.get(g.skill_id, 0)
        new = max(cur, min(cur + g.gain, g.max_level))
        if new <= cur:
            continue
        req = target["required"].get(g.skill_id, 0)
        useful = max(0, min(new, req) - cur)
        crit = g.skill_id in target["critical"]
        gap_points += useful * (CRITICAL_WEIGHT if crit else 1.0)
        beyond += (new - cur - useful) * 0.2
        gains.append({"skill_id": g.skill_id, "from": cur, "to": new, "required": req, "critical": crit,
                      "closes_gap": useful})
    fmt = sig["format"].get(ev.format, {"completed": 0, "skipped": 0, "rate": 0.5})
    typ = sig["type"].get(ev.type, {"completed": 0, "skipped": 0, "rate": 0.5})
    reliability = round(0.7 * fmt["rate"] + 0.3 * typ["rate"], 2)
    format_avoided = fmt["skipped"] >= AVOID_SKIPS and fmt["completed"] == 0
    similar_skips = max((sig["skips_by_skill"].get(g["skill_id"], 0) for g in gains), default=0)
    feedback = sig["feedback_by_type"].get(ev.type)
    sessions = [d for d in ev.upcoming_sessions if d >= store.as_of]
    wait_days = 0 if ev.format == "self_paced" or not sessions else _days(store.as_of, sessions[0])
    parts = {
        "gap_points": round(gap_points, 2),                        # levels closed toward the target, x2 if critical
        "beyond_target": round(beyond, 2),                         # growth above the requirement counts a little
        "reliability_mult": round(0.2 + 0.8 * reliability, 2),     # completes this format/type?
        "avoidance_mult": AVOID_FACTOR if format_avoided else 1.0,  # 3+ skips, 0 completions in this format
        "feedback_mult": 0.9 if feedback is not None and feedback <= 2.5 else 1.05 if feedback is not None and feedback >= 4.5 else 1.0,
        "skip_penalty": round(0.25 * max(0, similar_skips - 1), 2),  # repeatedly skipped this skill area
        "duration_penalty": round(0.01 * ev.duration_hours, 2),
        "wait_penalty": 0.15 if wait_days > 90 else 0.0,           # next session more than 3 months away
    }
    score = ((parts["gap_points"] + parts["beyond_target"]) * parts["reliability_mult"] * parts["avoidance_mult"]
             * parts["feedback_mult"] - parts["skip_penalty"] - parts["duration_penalty"] - parts["wait_penalty"])
    return {
        "event_id": ev.event_id, "title": ev.title, "type": ev.type, "format": ev.format,
        "duration_hours": ev.duration_hours, "next_session": sessions[0] if sessions else None,
        "score": round(score, 3), "score_parts": parts,
        "factors": {
            "gap_points": round(gap_points, 2),
            "closes_critical_gap": any(g["critical"] and g["closes_gap"] > 0 for g in gains),
            "format_reliability": fmt, "type_reliability": typ, "reliability": reliability,
            "format_avoided": format_avoided,
            "similar_skips": similar_skips,
            "feedback_on_type": feedback,
            "wait_days": wait_days,
        },
        "gains": gains,
    }


def candidates(store: Store, emp_id: str) -> dict:
    emp = store.employees[emp_id]
    levels, applied = effective_skills(store, emp)
    target = target_profile(store, emp)
    sig = history_signals(store, emp_id)
    scored, excluded = [], Counter()
    for ev in store.events.values():
        reason = eligibility(store, emp, ev, levels, target)
        if reason:
            excluded[reason.split("_SK_")[0]] += 1
            continue
        scored.append(score_event(store, ev, levels, target, sig))
    # Prerequisite chains: a valuable event blocked only by prerequisites boosts an eligible step that unlocks it.
    blocked = []
    for ev in store.events.values():
        reason = eligibility(store, emp, ev, levels, target)
        if not (reason and reason.startswith("prerequisite_")):
            continue
        if eligibility(store, emp, ev, levels, target, ignore_prereq=True):
            continue
        b = score_event(store, ev, levels, target, sig)
        if b["score"] <= 0:
            continue
        missing = {sid: need for sid, need in ev.prerequisites.items() if levels.get(sid, 0) < need}
        blocked.append({"event_id": ev.event_id, "title": ev.title, "score": b["score"], "missing": missing,
                        "closes_critical_gap": b["factors"]["closes_critical_gap"]})
    for c_ in scored:
        ev = store.events[c_["event_id"]]
        after = {g.skill_id: max(levels.get(g.skill_id, 0), min(levels.get(g.skill_id, 0) + g.gain, g.max_level))
                 for g in ev.develops_skills}
        for b in blocked:
            # unlocks b if after this step every missing prerequisite is met
            if all(after.get(sid, levels.get(sid, 0)) >= need for sid, need in b["missing"].items()):
                bonus = round(CHAIN_WEIGHT * b["score"], 3)
                if bonus > c_["factors"].get("unlock_bonus", 0):
                    c_["unlocks"] = {k: b[k] for k in ("event_id", "title", "missing", "closes_critical_gap")}
                    c_["factors"]["unlock_bonus"] = bonus
        c_["score_parts"]["unlock_bonus"] = c_["factors"].get("unlock_bonus", 0)
        c_["score"] = round(c_["score"] + c_["factors"].get("unlock_bonus", 0), 3)
    scored.sort(key=lambda c: -c["score"])
    g = gaps(levels, target)
    covered = {x["skill_id"] for c in scored for x in c["gains"] if x["closes_gap"] > 0}
    for b in blocked:
        if any("unlocks" in c_ and c_["unlocks"]["event_id"] == b["event_id"] for c_ in scored):
            covered |= {g_.skill_id for g_ in store.events[b["event_id"]].develops_skills}
    uncovered = [x for x in g if x["skill_id"] not in covered]
    in_progress = [{"event_id": h.event_id, "title": store.events[h.event_id].title, "completion_pct": h.completion_pct}
                   for h in store.history_of(emp_id)
                   if h.status == "in_progress" and not store.events[h.event_id].mandatory]
    return {"in_progress": in_progress, "employee": emp, "levels": levels, "applied_after_review": applied, "target": target,
            "gaps": g, "uncovered_gaps": uncovered, "readiness": readiness(levels, target), "signals": sig,
            "candidates": [c for c in scored if c["score"] > 0], "excluded": dict(excluded), "blocked": blocked}


def is_useful(c: dict) -> bool:
    """Moves the employee toward the target: closes a gap or unlocks an activity that does."""
    return c["factors"]["gap_points"] > 0 or bool(c.get("unlocks"))


def pick_diverse(cands: list[dict], k: int = 3) -> list[dict]:
    """Top-k useful steps (fallback: best available), avoiding two picks that mainly target the same skill."""
    cands = [c for c in cands if is_useful(c)]   # nothing useful -> no steps (mentoring is suggested instead)
    picked, seen = [], set()
    for c in cands:
        main = max(c["gains"], key=lambda g: (g["closes_gap"], g["critical"]), default=None)
        key = main["skill_id"] if main else c["event_id"]
        if key in seen and len(cands) > k:
            continue
        picked.append(c)
        seen.add(key)
        if len(picked) == k:
            break
    return picked


def baseline_lowest_skill(store: Store, emp_id: str) -> str | None:
    """Single-factor baseline for the benchmark: the skill with the lowest recorded level among target requirements."""
    emp = store.employees[emp_id]
    t = target_profile(store, emp)
    return min(t["required"], key=lambda s: (emp.skills.get(s, 0), s)) if t["required"] else None


# ---------- progress ----------
def complete_event(store: Store, emp_id: str, event_id: str) -> dict:
    from app.data import HistoryRow
    emp = store.employees[emp_id]
    before, _ = effective_skills(store, emp)
    t = target_profile(store, emp)
    r_before = readiness(before, t)
    store.history.append(HistoryRow(record_id="RT" + store.next_record_id()[1:], employee_id=emp_id, event_id=event_id,
                                    date=store.as_of, status="completed", completion_pct=100, assigned_by="self"))
    after, _ = effective_skills(store, emp)
    changed = {s: {"from": before.get(s, 0), "to": v} for s, v in after.items() if v != before.get(s, 0)}
    return {"changed": changed, "readiness_before": r_before, "readiness_after": readiness(after, t)}


# ---------- HR ----------
def hr_summary(store: Store, department: str | None = None) -> dict:
    key = (department, store.data_version, len(store.employees), len(store.events), len(store.history),
           store.history[-1].record_id if store.history else "",
           sum(len(v) for v in store.dismissed.values()))
    if key not in store.hr_cache:
        store.hr_cache.clear()
        store.hr_cache[key] = _hr_summary(store, department)
    return store.hr_cache[key]


def _hr_summary(store: Store, department: str | None = None) -> dict:
    people = [e for e in store.employees.values() if not department or e.department == department]
    ids = {e.employee_id for e in people}
    lagging, no_step, gap_any, gap_crit, ready = Counter(), [], Counter(), Counter(), []
    for emp in people:
        levels, _ = effective_skills(store, emp)
        own = store.role_profiles[(emp.role, emp.grade)]
        for sid, req in own.required_skills.items():
            if levels.get(sid, 0) < req:
                lagging[sid] += 1
        c = candidates(store, emp.employee_id)
        ready.append(readiness(c["levels"], c["target"]))
        for g in c["uncovered_gaps"]:
            gap_any[g["skill_id"]] += 1
            gap_crit[g["skill_id"]] += g["critical"]
        if not any(is_useful(x) for x in c["candidates"]):
            no_step.append({"employee_id": emp.employee_id, "role": emp.role, "grade": emp.grade,
                            "target": f'{c["target"]["role"]} {c["target"]["grade"]}',
                            "top_exclusion": ("no_useful_gain" if c["candidates"] else
                                              max(c["excluded"], key=c["excluded"].get) if c["excluded"] else None)})
    participation = defaultdict(Counter)
    for h in store.history:
        if h.employee_id in ids:
            participation[h.event_id][h.status] += 1
    name = lambda s: store.skills[s].name
    return {
        "department": department,
        "departments": sorted({e.department for e in store.employees.values()}),
        "employees": len(people),
        "avg_readiness": round(sum(ready) / len(ready), 1) if ready else 0.0,
        "ready_80": sum(r >= 80 for r in ready),
        "lagging_skills": [{"skill_id": s, "name": name(s), "employees": n} for s, n in lagging.most_common(10)],
        "no_recommended_step": no_step,
        # Skills people need for their target but no available activity can raise: a signal to extend the catalogue.
        "catalog_gaps": [{"skill_id": s, "name": name(s), "employees": gap_any[s], "critical_for": gap_crit[s]}
                         for s, _ in sorted(gap_any.items(), key=lambda kv: (-gap_crit[kv[0]], -kv[1]))[:8]],
        "participation": [{"event_id": e, "title": store.events[e].title, **dict(c)} for e, c in sorted(participation.items())],
        "support": support_signals(store, department),
    }


# ---------- template rationales (rules-only mode, no API key) ----------
FORMAT_NAMES = {"en": {"online": "online", "offline": "in-person", "self_paced": "self-paced"},
                "ru": {"online": "онлайн", "offline": "очно", "self_paced": "самостоятельно"},
                "kk": {"online": "онлайн", "offline": "бетпе-бет", "self_paced": "өз бетімен"}}
_T = {
    "en": {"gap": "{name}: {cur} → {to} (target {req})", "crit": "critical for {grade}",
           "rel": "you completed {done} of {tot} {fmt} activities", "sess": "next session {d}",
           "why": "{name} is your lowest skill ({lvl}), but {reason}.",
           "r_skip": "you skipped {n} similar activities", "r_notcrit": "it is not critical for {grade}",
           "r_none": "no eligible activity develops it now", "unlock": "unlocks {title}"},
    "ru": {"gap": "{name}: {cur} → {to} (нужно {req})", "crit": "критично для {grade}",
           "rel": "вы завершили {done} из {tot} активностей в формате «{fmt}»", "sess": "ближайшая сессия {d}",
           "why": "{name} — ваш самый низкий навык ({lvl}), но {reason}.",
           "r_skip": "вы пропустили {n} похожих активностей", "r_notcrit": "он не критичен для {grade}",
           "r_none": "сейчас нет подходящей активности для него", "unlock": "открывает доступ к «{title}»"},
    "kk": {"gap": "{name}: {cur} → {to} (қажет {req})", "crit": "{grade} үшін маңызды",
           "rel": "сіз {fmt} форматындағы {tot} белсенділіктің {done}-ін аяқтадыңыз", "sess": "келесі сессия {d}",
           "why": "{name} — ең төмен дағдыңыз ({lvl}), бірақ {reason}.",
           "r_skip": "ұқсас {n} белсенділікті өткізіп алдыңыз", "r_notcrit": "ол {grade} үшін маңызды емес",
           "r_none": "қазір оны дамытатын қолжетімді белсенділік жоқ", "unlock": "«{title}» курсына жол ашады"},
}


def template_factors(x: dict, c: dict) -> list[str]:
    return [f for f in ["skill_gap", "critical_for_next_grade", "participation_history", "expected_gain",
                        "next_level_requirement", "career_goal", "session_timing"] if f in supported_factors(x, c)]


def template_rationale(store: Store, x: dict, c: dict, lang: str) -> str:
    t = _T.get(lang, _T["en"])
    parts = []
    for g in sorted(x["gains"], key=lambda g: (-g["closes_gap"], not g["critical"]))[:2]:
        name = store.skills[g["skill_id"]].name
        s = (t["gap"].format(name=name, cur=g["from"], to=g["to"], req=g["required"]) if g["required"]
             else f'{name}: {g["from"]} → {g["to"]}')
        if g["critical"] and g["closes_gap"] > 0:
            s += f' — {t["crit"].format(grade=c["target"]["grade"])}'
        parts.append(s)
    fr = x["factors"]["format_reliability"]
    if fr["completed"] + fr["skipped"] > 0:
        fmt = FORMAT_NAMES.get(lang, FORMAT_NAMES["en"]).get(x["format"], x["format"])
        parts.append(t["rel"].format(done=fr["completed"], tot=fr["completed"] + fr["skipped"], fmt=fmt))
    if x["next_session"]:
        parts.append(t["sess"].format(d=x["next_session"]))
    if u := x.get("unlocks"):
        parts.append(t["unlock"].format(title=u["title"]))
    return "; ".join(parts) + "."


def template_why_not(store: Store, c: dict, picked: list[dict], lang: str) -> str:
    base = baseline_lowest_skill(store, c["employee"].employee_id)
    if not base or any(g["skill_id"] == base for x in picked for g in x["gains"]):
        return ""
    t = _T.get(lang, _T["en"])
    skips = c["signals"]["skips_by_skill"].get(base, 0)
    if skips >= 2:
        reason = t["r_skip"].format(n=skips)
    elif base not in c["target"]["critical"]:
        reason = t["r_notcrit"].format(grade=c["target"]["grade"])
    else:
        reason = t["r_none"]
    return t["why"].format(name=store.skills[base].name, lvl=c["employee"].skills.get(base, 0), reason=reason)


# ---------- what-if: path to the target ----------
def simulate_path(store: Store, emp_id: str, max_steps: int = 5) -> dict:
    """Greedy simulation: take the best-scoring step, apply its gains, re-plan, repeat.

    Runs on a copy of the store, so nothing is saved. Dates come from each event's next session.
    """
    import copy
    sim = copy.deepcopy(store)
    emp = sim.employees[emp_id]
    first = candidates(sim, emp_id)
    target = first["target"]
    path, when, crit_done = [], store.as_of, False
    for _ in range(max_steps):
        c = candidates(sim, emp_id)
        taken = {p["event_id"] for p in path}
        useful = [x for x in c["candidates"] if x["factors"]["gap_points"] > 0 and x["event_id"] not in taken]
        if not useful or c["readiness"] >= 100:
            break
        step = useful[0]
        ev = sim.events[step["event_id"]]
        # chronological: first session on/after the previous step; self-paced starts right away
        date = when if ev.format == "self_paced" else next((d for d in sorted(ev.upcoming_sessions) if d >= when), None)
        if date:
            when = date
        res = complete_event(sim, emp_id, step["event_id"])
        lv, _ = effective_skills(sim, emp)
        met = all(lv.get(s, 0) >= target["required"].get(s, 0) for s in target["critical"])
        path.append({"event_id": step["event_id"], "title": step["title"], "format": step["format"],
                     "date": date, "readiness_after": res["readiness_after"], "skills_changed": res["changed"],
                     "critical_met": met and not crit_done})
        crit_done = crit_done or met
    final_levels, _ = effective_skills(sim, emp)
    return {"target": {"role": target["role"], "grade": target["grade"], "kind": target["kind"]},
            "readiness_now": first["readiness"], "readiness_after": path[-1]["readiness_after"] if path else first["readiness"],
            "estimated_by": when if path else None, "steps": path,
            "remaining_gaps": gaps(final_levels, target)}


# ---------- mentoring ----------
def find_mentors(store: Store, emp_id: str, k: int = 3) -> dict:
    """Colleagues who can help with the employee's biggest gaps, gaps no activity can close first.

    Mentor = same department, Senior/Lead, effective level >= max(required + 1, 4) in the gap skill,
    and mentoring readiness (Mentoring skill >= 3 or completed the Mentor Track). Only name, role and the
    skill are returned; other people's skill levels stay private.
    """
    c = candidates(store, emp_id)
    emp = c["employee"]
    uncovered = {g["skill_id"] for g in c["uncovered_gaps"]}
    gap_list = sorted(c["gaps"], key=lambda g: (g["skill_id"] not in uncovered, not g["critical"], -g["gap"]))[:4]
    mentor_track = {h.employee_id for h in store.history if h.event_id == "EV_037" and h.status == "completed"}
    out, used = [], set()
    for g in gap_list:
        need = max(g["required"] + 1, 4)
        pool = []
        for m in store.employees.values():
            if m.employee_id == emp_id or m.department != emp.department or m.grade not in ("Senior", "Lead"):
                continue
            lv, _ = effective_skills(store, m)
            if lv.get(g["skill_id"], 0) < need:
                continue
            ready = lv.get("SK_MENTORING", 0) >= 3 or m.employee_id in mentor_track
            if ready:
                pool.append((m.grade == "Lead", lv.get("SK_MENTORING", 0), m))
        pool.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for _, _, m in pool:
            if m.employee_id in used:
                continue
            used.add(m.employee_id)
            out.append({"employee_id": m.employee_id, "full_name": m.full_name, "role": m.role, "grade": m.grade,
                        "skill_id": g["skill_id"], "skill": store.skills[g["skill_id"]].name,
                        "no_course_available": g["skill_id"] in uncovered, "critical": g["critical"]})
            break
        if len(out) == k:
            break
    return {"mentors": out}


# ---------- gamification: points, rewards, personal challenges ----------
POINTS_PER_ACTIVITY = 10
POINTS_PER_LEVEL = 10
CHALLENGE_BONUS = 50
REWARDS = [
    {"reward_id": "RW_BOOK", "title": "Book from the professional library", "cost": 60},
    {"reward_id": "RW_LUNCH", "title": "Lunch with a Lead of your choice", "cost": 100},
    {"reward_id": "RW_CONF", "title": "Seat at the internal tech conference", "cost": 150},
    {"reward_id": "RW_DAY", "title": "Extra learning day", "cost": 250},
]


def wallet(store: Store, emp_id: str) -> dict:
    """Private balance. Only voluntary completions earn points; mandatory trainings never do."""
    past = [h for h in store.history if h.employee_id == emp_id and h.status == "completed"
            and not store.events[h.event_id].mandatory and not h.record_id.startswith("RT")]
    entries = [{"kind": "history", "event_id": h.event_id, "title": store.events[h.event_id].title,
                "date": h.date, "points": POINTS_PER_ACTIVITY} for h in past]
    entries += [x for x in store.ledger if x["employee_id"] == emp_id]
    earned = sum(x["points"] for x in entries if x["points"] > 0)
    spent = -sum(x["points"] for x in entries if x["points"] < 0)
    balance = earned - spent
    return {"balance": balance, "earned": earned, "spent": spent,
            "rules": {"per_activity": POINTS_PER_ACTIVITY, "per_level": POINTS_PER_LEVEL, "challenge_bonus": CHALLENGE_BONUS},
            "recent": sorted(entries, key=lambda x: x.get("date") or "", reverse=True)[:6],
            "rewards": [{**r, "affordable": balance >= r["cost"]} for r in REWARDS],
            "challenge_offer": challenge_offer(store, emp_id),
            "challenges": store.challenges.get(emp_id, [])}


def challenge_offer(store: Store, emp_id: str) -> dict | None:
    """Suggest one opt-in challenge: reach the target level of the first critical gap the path can close."""
    if any(c["status"] == "active" for c in store.challenges.get(emp_id, [])):
        return None
    p = simulate_path(store, emp_id, max_steps=5)
    c = candidates(store, emp_id)
    for g in sorted(c["gaps"], key=lambda g: not g["critical"]):  # critical gaps first, then any gap the path closes
        for s in p["steps"]:
            if g["skill_id"] in s["skills_changed"] and s["skills_changed"][g["skill_id"]]["to"] >= g["required"]:
                return {"skill_id": g["skill_id"], "skill": store.skills[g["skill_id"]].name, "target_level": g["required"],
                        "current": g["current"], "by": s["date"] or p["estimated_by"], "bonus": CHALLENGE_BONUS}
    return None


def accept_challenge(store: Store, emp_id: str) -> dict:
    offer = challenge_offer(store, emp_id)
    if not offer:
        raise ValueError("No challenge available right now")
    ch = {**offer, "status": "active", "accepted_on": store.as_of}
    store.challenges.setdefault(emp_id, []).append(ch)
    return ch


def award_completion(store: Store, emp_id: str, event_id: str, changed: dict) -> list[dict]:
    """Ledger entries for a runtime completion + any challenge it finishes."""
    if store.events[event_id].mandatory:
        return []
    levels = sum(v["to"] - v["from"] for v in changed.values())
    new = [{"employee_id": emp_id, "kind": "activity", "event_id": event_id, "title": store.events[event_id].title,
            "date": store.as_of, "points": POINTS_PER_ACTIVITY + POINTS_PER_LEVEL * levels}]
    lv, _ = effective_skills(store, store.employees[emp_id])
    for ch in store.challenges.get(emp_id, []):
        if ch["status"] == "active" and lv.get(ch["skill_id"], 0) >= ch["target_level"]:
            ch["status"] = "completed"
            new.append({"employee_id": emp_id, "kind": "challenge", "title": f'Challenge: {ch["skill"]} {ch["target_level"]}',
                        "date": store.as_of, "points": CHALLENGE_BONUS})
    store.ledger.extend(new)
    return new


def redeem(store: Store, emp_id: str, reward_id: str) -> dict:
    reward = next((r for r in REWARDS if r["reward_id"] == reward_id), None)
    if not reward:
        raise KeyError(reward_id)
    if wallet(store, emp_id)["balance"] < reward["cost"]:
        raise ValueError("Not enough points")
    entry = {"employee_id": emp_id, "kind": "redeem", "title": reward["title"], "date": store.as_of, "points": -reward["cost"]}
    store.ledger.append(entry)
    return entry


# ---------- skill garden and personal level ----------
LEVELS = [(0, "Seedling"), (50, "Sprout"), (120, "Grower"), (220, "Gardener"), (350, "Arborist"), (520, "Forest keeper")]


def level_for(points: int) -> dict:
    idx = max(i for i, (t, _) in enumerate(LEVELS) if points >= t)
    nxt = LEVELS[idx + 1][0] if idx + 1 < len(LEVELS) else None
    return {"level": idx + 1, "title": LEVELS[idx][1], "points": points, "floor": LEVELS[idx][0], "next_at": nxt}


def _plants(store: Store, emp: Employee) -> list[dict]:
    levels, _ = effective_skills(store, emp)
    t = target_profile(store, emp)
    plants = [{"skill_id": s, "skill": store.skills[s].name, "stage": min(5, levels.get(s, 0)), "target": req,
               "critical": s in t["critical"], "grown": levels.get(s, 0) >= req}
              for s, req in t["required"].items()]
    return sorted(plants, key=lambda p: (not p["critical"], p["grown"], p["skill"]))


def garden(store: Store, emp_id: str) -> dict:
    """Each skill the target needs is a plant; its stage is the real skill level (0-5).
    Colleagues' gardens are visible only if both sides opted in and share a department."""
    emp = store.employees[emp_id]
    w = wallet(store, emp_id)
    plants = _plants(store, emp)
    shared = emp_id in store.shared_gardens
    neighbours = []
    if shared:
        for other_id in sorted(store.shared_gardens - {emp_id}):
            o = store.employees[other_id]
            if o.department != emp.department:
                continue
            op = _plants(store, o)
            neighbours.append({"employee_id": other_id, "full_name": o.full_name, "role": o.role,
                               "level": level_for(wallet(store, other_id)["earned"])["title"],
                               "plants": len(op), "blooming": sum(p["stage"] >= 4 for p in op)})
    return {"level": level_for(w["earned"]), "plants": plants,
            "grown": sum(p["grown"] for p in plants), "total": len(plants),
            "shared": shared, "neighbours": neighbours, "team": team_goal(store, emp.department)}


def set_share(store: Store, emp_id: str, on: bool) -> bool:
    (store.shared_gardens.add if on else store.shared_gardens.discard)(emp_id)
    return on


FEEDBACK_REASONS = {"format", "time", "not_interested"}


def dismiss(store: Store, emp_id: str, event_id: str, reason: str) -> dict:
    """Voluntary 'Not now'. Hides the activity; 'format' also lowers that format for future picks. No penalties."""
    if reason not in FEEDBACK_REASONS:
        raise ValueError(f"reason must be one of {sorted(FEEDBACK_REASONS)}")
    store.dismissed.setdefault(emp_id, {})[event_id] = reason
    return {"event_id": event_id, "reason": reason}


# ---------- HR event builder ----------
def event_draft(store: Store, skill_id: str) -> dict:
    """Pre-filled activity for a catalogue gap: aimed at the roles/grades of the people it blocks."""
    from datetime import date, timedelta
    blocked = []
    for emp in store.employees.values():
        c = candidates(store, emp.employee_id)
        g = next((x for x in c["uncovered_gaps"] if x["skill_id"] == skill_id), None)
        if g:
            blocked.append((emp, g))
    if not blocked:
        raise ValueError(f"No catalogue gap for {skill_id}")
    roles = sorted({e.role for e, _ in blocked} | {e.career_goal.target_role for e, _ in blocked if e.career_goal})
    grades = sorted({e.grade for e, _ in blocked}, key=GRADES.index)
    top = max(g["required"] for _, g in blocked)
    start = (date.fromisoformat(store.as_of) + timedelta(days=14)).isoformat()
    name = store.skills[skill_id].name
    return {"title": f"{name} Practicum", "description": f"Hands-on programme to build {name} to the level required for promotion.",
            "type": "workshop", "format": "online", "duration_hours": 8, "mandatory": False,
            "target_roles": roles, "target_grades": grades,
            "develops_skills": [{"skill_id": skill_id, "gain": 1, "max_level": min(5, top)}],
            "prerequisites": {}, "upcoming_sessions": [start],
            "blocked_employees": len(blocked)}


def create_event(store: Store, spec: dict) -> dict:
    ev = Event(**{**{k: v for k, v in spec.items() if k != "blocked_employees"},
                  "event_id": f"EV_H{sum(1 for e in store.events if e.startswith('EV_H')) + 1:02d}"})
    unknown = [g.skill_id for g in ev.develops_skills if g.skill_id not in store.skills]
    if unknown:
        raise ValueError(f"Unknown skills {unknown}")
    store.events[ev.event_id] = ev
    reach = sum(1 for eid in store.employees
                if any(x["event_id"] == ev.event_id for x in candidates(store, eid)["candidates"]))
    return {"event_id": ev.event_id, "title": ev.title, "now_recommendable_for": reach}


# ---------- HR: who may need support (the case's optional "attrition risk", framed as support) ----------
def support_signals(store: Store, department: str | None = None, limit: int = 15) -> list[dict]:
    """Plain-language signals, no opaque risk score. HR-only; meant to start a supportive conversation."""
    from datetime import date, timedelta
    today = date.fromisoformat(store.as_of)
    year_ago, half_year = (today - timedelta(days=365)).isoformat(), (today - timedelta(days=182)).isoformat()
    out = []
    for emp in store.employees.values():
        if department and emp.department != department:
            continue
        rows = store.history_of(emp.employee_id)
        vol = [h for h in rows if not store.events[h.event_id].mandatory]
        skips = sum(1 for h in vol if h.date >= year_ago and h.status in SKIP_STATUSES)
        done_recent = sum(1 for h in vol if h.date >= half_year and h.status == "completed")
        overdue = sum(1 for h in rows if h.status == "overdue" and h.date >= year_ago)
        reasons, actions = [], []
        if skips >= 3:
            reasons.append({"code": "skips", "n": skips})
            actions.append("ask_format")
        if overdue >= 2:
            reasons.append({"code": "overdue", "n": overdue})
            actions.append("check_workload")
        if done_recent == 0 and emp.tenure_months >= 6:
            reasons.append({"code": "no_recent_growth", "n": 0})
            actions.append("offer_mentor")
        if not emp.career_goal:
            reasons.append({"code": "no_goal", "n": 0})
            actions.append("goal_talk")
        if emp.grade == "Junior" and emp.tenure_months >= 36:
            reasons.append({"code": "long_junior", "n": emp.tenure_months})
            actions.append("promotion_path")
        if len(reasons) >= 2:
            out.append({"employee_id": emp.employee_id, "full_name": emp.full_name, "role": emp.role, "grade": emp.grade,
                        "department": emp.department, "reasons": reasons, "suggested_actions": list(dict.fromkeys(actions))})
    out.sort(key=lambda x: (-len(x["reasons"]), x["employee_id"]))
    return out[:limit]


# ---------- peer recognition and team goal ----------
KUDOS_POINTS = 15
KUDOS_PER_DAY = 3


def send_kudos(store: Store, from_id: str, to_id: str, message: str) -> dict:
    """Private thanks between colleagues of the same department; the recipient gets recognition points."""
    sender, rec = store.employees[from_id], store.employees.get(to_id)
    if not rec or to_id == from_id:
        raise ValueError("Choose a colleague")
    if rec.department != sender.department:
        raise ValueError("Recognition is for colleagues in your department")
    sent_today = sum(1 for x in store.ledger if x.get("kind") == "kudos" and x.get("from_id") == from_id and x["date"] == store.as_of)
    if sent_today >= KUDOS_PER_DAY:
        raise ValueError(f"Up to {KUDOS_PER_DAY} thanks per day")
    if any(x.get("kind") == "kudos" and x.get("from_id") == from_id and x["employee_id"] == to_id
           and _days(x["date"], store.as_of) < 7 for x in store.ledger):
        raise ValueError("You already thanked this colleague this week")
    entry = {"employee_id": to_id, "kind": "kudos", "from_id": from_id, "title": f"Thanks from {sender.full_name}",
             "message": message[:200], "date": store.as_of, "points": KUDOS_POINTS}
    store.ledger.append(entry)
    return {"to": rec.full_name, "points": KUDOS_POINTS}


def team_goal(store: Store, department: str, days: int = 90) -> dict:
    """Cooperative, anonymous: voluntary activities the whole department completed recently vs one per person."""
    from datetime import date, timedelta
    since = (date.fromisoformat(store.as_of) - timedelta(days=days)).isoformat()
    members = {e.employee_id for e in store.employees.values() if e.department == department}
    done = sum(1 for h in store.history if h.employee_id in members and h.status == "completed"
               and h.date >= since and not store.events[h.event_id].mandatory)
    return {"department": department, "days": days, "done": done, "goal": len(members)}


def supported_factors(x: dict, c: dict) -> set[str]:
    """Which explanation factors are actually true for candidate x — the AI may only claim these."""
    f = x["factors"]
    fr = f["format_reliability"]
    s = set()
    if f["gap_points"] > 0:
        s.add("skill_gap")
    if f["closes_critical_gap"]:
        s.add("critical_for_next_grade")
    if x["gains"]:
        s.add("expected_gain")
    if fr["completed"] + fr["skipped"] > 0 or f["type_reliability"]["completed"] + f["type_reliability"]["skipped"] > 0:
        s.update({"participation_history", "format_fit"})
    if any(g["required"] > 0 for g in x["gains"]) or x.get("unlocks"):
        s.add("next_level_requirement")
    if c["target"]["kind"] == "career_goal":
        s.add("career_goal")
    if x["next_session"] or x["format"] == "self_paced":
        s.add("session_timing")
    return s


# ---------- AI cost: measured usage and monthly estimate ----------
DEFAULT_TOKENS = {"recommendation": (2054, 300), "navigator": (2050, 170)}   # measured during the hackathon
USAGE_PROFILES = {"normal": (8, 10), "heavy": (24, 30)}   # (fresh AI recommendations, navigator questions) per month


def record_usage(store: Store, kind: str, model: str, input_tokens: int, output_tokens: int) -> None:
    from app import config
    pin, pout = config.PRICES.get(model, (0.0, 0.0))
    store.usage.append({"kind": kind, "model": model, "in": input_tokens, "out": output_tokens,
                        "usd": (input_tokens * pin + output_tokens * pout) / 1e6})


def ai_cost(store: Store) -> dict:
    from app import config
    def cost(model, tin, tout):
        pin, pout = config.PRICES[model]
        return (tin * pin + tout * pout) / 1e6
    # per-call token averages: measured on this server if available, else hackathon measurements
    avg = {}
    for kind, (din, dout) in DEFAULT_TOKENS.items():
        rows = [u for u in store.usage if u["kind"] == kind and u["model"] == config.OPENAI_MODEL]
        avg[kind] = (sum(u["in"] for u in rows) / len(rows), sum(u["out"] for u in rows) / len(rows)) if rows else (din, dout)
    rec_sol = cost("gpt-6-sol", *avg["recommendation"]) + cost("gpt-6-luna", *avg["recommendation"])  # luna runs as backup
    nav_sol = cost("gpt-6-sol", *avg["navigator"])
    rec_luna, nav_luna = cost("gpt-6-luna", *avg["recommendation"]), cost("gpt-6-luna", *avg["navigator"])
    per_employee = {"normal": USAGE_PROFILES["normal"][0] * rec_sol + USAGE_PROFILES["normal"][1] * nav_sol,
                    "heavy": USAGE_PROFILES["heavy"][0] * rec_sol + USAGE_PROFILES["heavy"][1] * nav_sol,
                    "luna_only": USAGE_PROFILES["normal"][0] * rec_luna + USAGE_PROFILES["normal"][1] * nav_luna,
                    "rules": 0.0}
    return {"per_employee_month_usd": {k: round(v, 4) for k, v in per_employee.items()},
            "per_call_usd": {"recommendation": round(rec_sol, 5), "navigator_question": round(nav_sol, 5)},
            "avg_tokens": {k: [round(x) for x in v] for k, v in avg.items()},
            "usage_profiles": USAGE_PROFILES, "prices_per_1m": config.PRICES,
            "measured": {"calls": len(store.usage), "input_tokens": sum(u["in"] for u in store.usage),
                         "output_tokens": sum(u["out"] for u in store.usage),
                         "spent_usd": round(sum(u["usd"] for u in store.usage), 4)},
            "employees": len(store.employees)}


NON_GAP_FACTORS = {"participation_history", "format_fit", "session_timing", "career_goal", "critical_for_next_grade"}
