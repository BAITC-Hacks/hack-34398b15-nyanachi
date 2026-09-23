"""Deterministic core: effective skills, target profile, eligibility, factor scoring, progress, HR stats.

Everything the AI layer says is grounded in numbers computed here.
"""
from collections import Counter, defaultdict

from app.data import GRADES, SKIP_STATUSES, Employee, Event, Store

REPEATABLE = {"EV_036"}
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
        if h.status != "completed" or h.date <= emp.last_review_date:
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
    statuses = {h.status for h in store.history_of(emp.employee_id) if h.event_id == ev.event_id}
    if "completed" in statuses and ev.event_id not in REPEATABLE:
        return "already_completed"
    if "in_progress" in statuses:
        return "in_progress"
    roles_ok = emp.role in ev.target_roles or target["role"] in ev.target_roles
    grades_ok = emp.grade in ev.target_grades or target["grade"] in ev.target_grades
    if not (roles_ok and grades_ok):
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
    skip_penalty = 0.25 * max(0, similar_skips - 1)
    sessions = [d for d in ev.upcoming_sessions if d >= store.as_of]
    score = ((gap_points + beyond) * (0.2 + 0.8 * reliability) * (AVOID_FACTOR if format_avoided else 1.0)
             - skip_penalty - 0.01 * ev.duration_hours)
    return {
        "event_id": ev.event_id, "title": ev.title, "type": ev.type, "format": ev.format,
        "duration_hours": ev.duration_hours, "next_session": sessions[0] if sessions else None,
        "score": round(score, 3),
        "factors": {
            "gap_points": round(gap_points, 2),
            "closes_critical_gap": any(g["critical"] and g["closes_gap"] > 0 for g in gains),
            "format_reliability": fmt, "type_reliability": typ, "reliability": reliability,
            "format_avoided": format_avoided,
            "similar_skips": similar_skips,
            "feedback_on_type": sig["feedback_by_type"].get(ev.type),
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
        c_["score"] = round(c_["score"] + c_["factors"].get("unlock_bonus", 0), 3)
    scored.sort(key=lambda c: -c["score"])
    g = gaps(levels, target)
    covered = {x["skill_id"] for c in scored for x in c["gains"] if x["closes_gap"] > 0}
    for b in blocked:
        if any("unlocks" in c_ and c_["unlocks"]["event_id"] == b["event_id"] for c_ in scored):
            covered |= {g_.skill_id for g_ in store.events[b["event_id"]].develops_skills}
    uncovered = [x for x in g if x["skill_id"] not in covered]
    return {"employee": emp, "levels": levels, "applied_after_review": applied, "target": target,
            "gaps": g, "uncovered_gaps": uncovered, "readiness": readiness(levels, target), "signals": sig,
            "candidates": [c for c in scored if c["score"] > 0], "excluded": dict(excluded), "blocked": blocked}


def pick_diverse(cands: list[dict], k: int = 3) -> list[dict]:
    """Top-k, avoiding two picks that mainly target the same skill."""
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
    store.history.append(HistoryRow(record_id=store.next_record_id(), employee_id=emp_id, event_id=event_id,
                                    date=store.as_of, status="completed", completion_pct=100, assigned_by="self"))
    # Completed "today" counts after last review, so effective_skills picks it up.
    if emp.last_review_date >= store.as_of:
        emp.last_review_date = "1900-01-01"
    after, _ = effective_skills(store, emp)
    changed = {s: {"from": before.get(s, 0), "to": v} for s, v in after.items() if v != before.get(s, 0)}
    return {"changed": changed, "readiness_before": r_before, "readiness_after": readiness(after, t)}


# ---------- HR ----------
def hr_summary(store: Store) -> dict:
    lagging, no_step = Counter(), []
    for emp in store.employees.values():
        levels, _ = effective_skills(store, emp)
        own = store.role_profiles[(emp.role, emp.grade)]
        for sid, req in own.required_skills.items():
            if levels.get(sid, 0) < req:
                lagging[sid] += 1
        c = candidates(store, emp.employee_id)
        if not c["candidates"]:
            no_step.append({"employee_id": emp.employee_id, "role": emp.role, "grade": emp.grade,
                            "target": f'{c["target"]["role"]} {c["target"]["grade"]}',
                            "top_exclusion": max(c["excluded"], key=c["excluded"].get) if c["excluded"] else None})
    participation = defaultdict(Counter)
    for h in store.history:
        participation[h.event_id][h.status] += 1
    return {
        "employees": len(store.employees),
        "lagging_skills": [{"skill_id": s, "name": store.skills[s].name, "employees": n} for s, n in lagging.most_common(10)],
        "no_recommended_step": no_step,
        "participation": [{"event_id": e, "title": store.events[e].title, **dict(c)} for e, c in sorted(participation.items())],
    }


# ---------- template rationales (rules-only mode, no API key) ----------
_T = {
    "en": {"gap": "{name}: {cur} → {to} (target {req})", "crit": "critical for {grade}",
           "rel": "you completed {done} of {tot} {fmt} activities", "sess": "next session {d}",
           "why": "{name} is your lowest skill ({lvl}), but {reason}.",
           "r_skip": "you skipped {n} similar activities", "r_notcrit": "it is not critical for {grade}",
           "r_none": "no eligible activity develops it now", "unlock": "unlocks {title}"},
    "ru": {"gap": "{name}: {cur} → {to} (нужно {req})", "crit": "критично для {grade}",
           "rel": "вы завершили {done} из {tot} активностей формата {fmt}", "sess": "ближайшая сессия {d}",
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
    f = ["skill_gap", "expected_gain", "participation_history"]
    if x["factors"]["closes_critical_gap"]:
        f.insert(1, "critical_for_next_grade")
    if c["target"]["kind"] == "career_goal":
        f.append("career_goal")
    if x.get("unlocks"):
        f.append("next_level_requirement")
    return f


def template_rationale(store: Store, x: dict, c: dict, lang: str) -> str:
    t = _T.get(lang, _T["en"])
    parts = []
    for g in sorted(x["gains"], key=lambda g: (-g["closes_gap"], not g["critical"]))[:2]:
        s = t["gap"].format(name=store.skills[g["skill_id"]].name, cur=g["from"], to=g["to"], req=g["required"])
        if g["critical"]:
            s += f' — {t["crit"].format(grade=c["target"]["grade"])}'
        parts.append(s)
    fr = x["factors"]["format_reliability"]
    parts.append(t["rel"].format(done=fr["completed"], tot=fr["completed"] + fr["skipped"], fmt=x["format"]))
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
