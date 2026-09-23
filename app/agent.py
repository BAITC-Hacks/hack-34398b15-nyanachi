"""AI layer: gpt-6-sol picks 1–3 steps from the engine's candidates and explains them.

Grounding: the model only sees numbers computed by app.engine and may only choose event_ids from
the candidate list. A validator enforces the case rules (1–3 steps, known events, >=3 factors);
on failure we fall back to gpt-6-luna, then to deterministic template rationales.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait

from openai import OpenAI

from app import config, engine

FACTORS = ["skill_gap", "critical_for_next_grade", "participation_history", "format_fit",
           "expected_gain", "next_level_requirement", "career_goal", "session_timing"]
LANG = {"kk": "Kazakh", "ru": "Russian", "en": "English"}

SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "rationale": {"type": "string"},
                "factors_used": {"type": "array", "items": {"type": "string", "enum": FACTORS}},
            },
            "required": ["event_id", "rationale", "factors_used"], "additionalProperties": False}},
        "why_not": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["steps", "why_not", "summary"], "additionalProperties": False,
}

INSTRUCTIONS = """You are Career Quest, a development navigator for bank employees.
You receive an employee's situation and a list of candidate activities, all pre-computed by a scoring engine.
Choose 1-3 activities that best move the employee toward the target profile.

Rules:
- Only use event_ids from `candidates`. Never invent activities, numbers, dates or facts.
- Prefer closing gaps in critical skills for the target; respect participation history: if the employee
  repeatedly skipped a format or skill area, prefer an alternative they are likely to finish.
- Avoid two activities that develop the same main skill unless nothing else is useful.
- If a candidate has `unlocks`, it is a prerequisite step for a more valuable activity: say so explicitly.
- Refer to activities and skills by their title/name, never by IDs like EV_006 or SK_CLOUD.
- Express participation as counts ("completed 4 of 5 online activities"), never as decimals or rates.
- Each rationale: 1-2 sentences, cite at least 3 factors with concrete numbers from the input
  (e.g. "System Design 2 vs 4 required for Senior", "4 of 4 online courses completed").
  List those factors in factors_used.
- why_not: one sentence on why the obvious single-factor pick (`lowest_skill_baseline`) was not chosen,
  or "" if it was chosen.
- summary: one encouraging sentence about the path to the target. Voluntary tone, no pressure.
- Write rationale, why_not and summary in {language}."""


def _payload(c: dict, store) -> dict:
    emp = c["employee"]
    names = {s: store.skills[s].name for s in store.skills}
    base = engine.baseline_lowest_skill(store, emp.employee_id)
    return {
        "employee": {"role": emp.role, "grade": emp.grade, "tenure_months": emp.tenure_months,
                     "work_format": emp.work_format},
        "target": {"role": c["target"]["role"], "grade": c["target"]["grade"], "kind": c["target"]["kind"],
                   "readiness_pct": c["readiness"]},
        "gaps": [{**g, "name": names[g["skill_id"]]} for g in c["gaps"][:8]],
        "skills_updated_since_last_review": c["applied_after_review"],
        "participation_by_format": c["signals"]["format"],
        "participation_by_type": c["signals"]["type"],
        "skips_by_skill": {names[k]: v for k, v in c["signals"]["skips_by_skill"].items()},
        "lowest_skill_baseline": {"skill": names.get(base, base), "recorded_level": emp.skills.get(base, 0),
                                  "skips_in_this_area": c["signals"]["skips_by_skill"].get(base, 0),
                                  "critical_for_target": base in c["target"]["critical"]} if base else None,
        "candidates": [{"event_id": x["event_id"], "title": x["title"], "type": x["type"], "format": x["format"],
                        "duration_hours": x["duration_hours"], "next_session": x["next_session"],
                        "engine_score": x["score"],
                        "gains": [{**g, "name": names[g["skill_id"]]} for g in x["gains"]],
                        "reliability_in_this_format": x["factors"]["format_reliability"],
                        "similar_skips": x["factors"]["similar_skips"],
                        "unlocks": ({"title": x["unlocks"]["title"], "closes_critical_gap": x["unlocks"]["closes_critical_gap"]} if x.get("unlocks") else None)} for x in c["candidates"][:config.AI_CANDIDATES]],
    }


def _validate(out: dict, cand_ids: set[str]) -> str | None:
    ids = [s["event_id"] for s in out["steps"]]
    if not 1 <= len(ids) <= 3:
        return "need 1-3 steps"
    if len(set(ids)) != len(ids):
        return "duplicate steps"
    if unknown := [i for i in ids if i not in cand_ids]:
        return f"unknown event_ids {unknown}"
    if thin := [s["event_id"] for s in out["steps"] if len(set(s["factors_used"])) < 3]:
        return f"fewer than 3 factors for {thin}"
    return None


def _call(model: str, payload: dict, lang: str) -> dict:
    client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=config.AI_TIMEOUT_S, max_retries=0)
    r = client.responses.create(
        model=model,
        instructions=INSTRUCTIONS.replace("{language}", LANG.get(lang, "Russian")),
        input=json.dumps(payload, ensure_ascii=False),
        text={"format": {"type": "json_schema", "name": "recommendation", "schema": SCHEMA, "strict": True}},
        reasoning={"effort": config.AI_REASONING},
    )
    return json.loads(r.output_text)


def recommend(store, emp_id: str, use_ai: bool = True) -> dict:
    t0 = time.time()
    c = engine.candidates(store, emp_id)
    by_id = {x["event_id"]: x for x in c["candidates"]}
    rules = engine.pick_diverse(c["candidates"])
    lang = c["employee"].preferred_language
    result = {"mode": "rules", "language": lang, "target": c["target"], "readiness": c["readiness"],
              "uncovered_gaps": c["uncovered_gaps"], "attempts": []}

    if use_ai and config.OPENAI_API_KEY and c["candidates"]:
        # Race primary and fast model in parallel; prefer the primary if it is valid within the budget.
        payload = _payload(c, store)
        models = [config.OPENAI_MODEL, config.OPENAI_FAST_MODEL]
        pool = ThreadPoolExecutor(len(models))
        futs = {pool.submit(_call, m, payload, lang): m for m in models}
        wait(futs, timeout=config.AI_TIMEOUT_S)
        pool.shutdown(wait=False, cancel_futures=True)
        for f, model in futs.items():
            if not f.done():
                result["attempts"].append({"model": model, "error": f"no answer within {config.AI_TIMEOUT_S}s"})
                continue
            if f.exception():
                e = f.exception()
                result["attempts"].append({"model": model, "error": f"{type(e).__name__}: {e}"[:200]})
                continue
            out = f.result()
            if problem := _validate(out, set(by_id)):
                result["attempts"].append({"model": model, "error": f"invalid output: {problem}"})
                continue
            result.update(mode="ai", model=model, why_not=out["why_not"], summary=out["summary"],
                          steps=[{**by_id[s["event_id"]], "rationale": s["rationale"],
                                  "factors_used": s["factors_used"]} for s in out["steps"]])
            break

    if result["mode"] == "rules":
        result["steps"] = [{**x, "rationale": engine.template_rationale(store, x, c, lang),
                            "factors_used": engine.template_factors(x, c)} for x in rules]
        result["why_not"] = engine.template_why_not(store, c, rules, lang)
        result["summary"] = ""
    result["latency_s"] = round(time.time() - t0, 2)
    return result
