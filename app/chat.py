"""Career navigator chat: a tool-calling agent over the deterministic engine.

The model never sees raw data dumps; it calls tools that return engine-computed facts about ONE employee
(the one who is logged in). It cannot query other employees. Max TOOL_ROUNDS rounds of tool calls.
"""
import json
import time

from openai import OpenAI

from app import config, engine

TOOL_ROUNDS = 4
LANG = {"kk": "Kazakh", "ru": "Russian", "en": "English"}

INSTRUCTIONS = """You are Career Quest, a friendly development navigator for one bank employee.
Answer questions about their growth: what to do next, why something was or was not recommended,
how to reach a grade or career goal, mentors, timelines.
- Always ground answers in tool results. Call tools before answering factual questions. Never invent
  activities, dates, numbers or colleagues.
- Refer to activities and skills by name, never by IDs. Use counts, not decimals.
- Participation is voluntary: suggest, never pressure. Never compare the employee with colleagues.
- Keep answers short: 2-5 sentences or a short list.
- Reply in the language of the user's message (Kazakh, Russian or English); if unclear, use {language}."""


def _tool(name, description, props=None, required=None):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": props or {}, "required": required or [],
                           "additionalProperties": False}}


TOOLS = [
    _tool("get_overview", "Employee's role, grade, target (next grade or career goal), readiness %, biggest skill gaps, "
          "gaps no activity can close, and participation history by format."),
    _tool("list_recommendations", "Top eligible activities with engine scores and factors (skill gains, critical gap, "
          "completion history in that format, prerequisite chains)."),
    _tool("skill_options", "Everything about one skill: current vs required level, whether it is critical, past skips, "
          "and every activity that develops it with its eligibility status or exclusion reason.",
          {"skill": {"type": "string", "description": "Skill name or id, e.g. 'Public Speaking'"}}, ["skill"]),
    _tool("explain_activity", "Why a specific activity is or is not recommended: eligibility, exclusion reason, factors.",
          {"activity": {"type": "string", "description": "Activity title or id, e.g. 'Kubernetes in Practice'"}}, ["activity"]),
    _tool("simulate_path", "What-if: take the best steps in order and show readiness after each and the dates."),
    _tool("find_mentors", "Colleagues in the same department who can mentor the employee's biggest gaps (name, role only)."),
]


class Tools:
    def __init__(self, store, emp_id: str):
        self.s, self.emp_id = store, emp_id
        self.c = engine.candidates(store, emp_id)
        self.name = lambda sid: store.skills[sid].name if sid in store.skills else sid

    def _find_skill(self, q: str):
        q = q.strip().lower()
        for sid, sk in self.s.skills.items():
            if q in (sid.lower(), sk.name.lower()):
                return sid
        return next((sid for sid, sk in self.s.skills.items() if q in sk.name.lower()), None)

    def _find_event(self, q: str):
        q = q.strip().lower()
        for eid, ev in self.s.events.items():
            if q in (eid.lower(), ev.title.lower()):
                return eid
        return next((eid for eid, ev in self.s.events.items() if q in ev.title.lower()), None)

    def get_overview(self):
        c, e = self.c, self.c["employee"]
        return {"role": e.role, "grade": e.grade, "tenure_months": e.tenure_months,
                "target": f'{c["target"]["role"]} {c["target"]["grade"]}', "target_kind": c["target"]["kind"],
                "readiness_pct": c["readiness"],
                "gaps": [{"skill": self.name(g["skill_id"]), "current": g["current"], "required": g["required"],
                          "critical": g["critical"]} for g in c["gaps"][:8]],
                "gaps_no_activity_can_close": [self.name(g["skill_id"]) for g in c["uncovered_gaps"]],
                "participation_by_format": c["signals"]["format"]}

    def list_recommendations(self):
        return [{"activity": x["title"], "format": x["format"], "next_session": x["next_session"], "score": x["score"],
                 "gains": [f'{self.name(g["skill_id"])} {g["from"]}->{g["to"]} (required {g["required"]})' for g in x["gains"]],
                 "closes_critical_gap": x["factors"]["closes_critical_gap"],
                 "completed_in_this_format": f'{x["factors"]["format_reliability"]["completed"]} of '
                                             f'{x["factors"]["format_reliability"]["completed"] + x["factors"]["format_reliability"]["skipped"]}',
                 "unlocks": x.get("unlocks", {}).get("title") if x.get("unlocks") else None}
                for x in self.c["candidates"][:5]]

    def skill_options(self, skill: str):
        sid = self._find_skill(skill)
        if not sid:
            return {"error": f"unknown skill '{skill}'"}
        c, emp = self.c, self.c["employee"]
        opts = []
        for ev in self.s.events.values():
            if not any(g.skill_id == sid for g in ev.develops_skills):
                continue
            reason = engine.eligibility(self.s, emp, ev, c["levels"], c["target"])
            opts.append({"activity": ev.title, "format": ev.format, "eligible": reason is None,
                         "why_not_eligible": reason})
        return {"skill": self.name(sid), "current": c["levels"].get(sid, 0),
                "required_for_target": c["target"]["required"].get(sid, 0),
                "critical_for_target": sid in c["target"]["critical"],
                "times_skipped_similar": c["signals"]["skips_by_skill"].get(sid, 0), "activities": opts}

    def explain_activity(self, activity: str):
        eid = self._find_event(activity)
        if not eid:
            return {"error": f"unknown activity '{activity}'"}
        c, emp, ev = self.c, self.c["employee"], self.s.events[eid]
        reason = engine.eligibility(self.s, emp, ev, c["levels"], c["target"])
        cand = next((x for x in c["candidates"] if x["event_id"] == eid), None)
        rank = [x["event_id"] for x in c["candidates"]].index(eid) + 1 if cand else None
        return {"activity": ev.title, "eligible": reason is None, "exclusion_reason": reason, "rank_among_eligible": rank,
                "factors": cand["factors"] if cand else None,
                "gains": [f'{self.name(g["skill_id"])} {g["from"]}->{g["to"]}' for g in cand["gains"]] if cand else None}

    def simulate_path(self):
        p = engine.simulate_path(self.s, self.emp_id)
        return {"readiness_now": p["readiness_now"], "readiness_after": p["readiness_after"], "by": p["estimated_by"],
                "steps": [{"activity": s["title"], "date": s["date"], "readiness_after": s["readiness_after"]} for s in p["steps"]],
                "still_open": [self.name(g["skill_id"]) for g in p["remaining_gaps"]]}

    def find_mentors(self):
        return engine.find_mentors(self.s, self.emp_id)["mentors"]


def chat(store, emp_id: str, message: str, history: list[dict]) -> dict:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    t0 = time.time()
    tools = Tools(store, emp_id)
    lang = store.employees[emp_id].preferred_language
    client = OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL or None,
                    timeout=config.CHAT_TIMEOUT_S, max_retries=0)
    items = [{"role": h["role"], "content": h["content"]} for h in history[-8:] if h.get("role") in ("user", "assistant")]
    items.append({"role": "user", "content": message})
    trace = []
    for _ in range(TOOL_ROUNDS + 1):
        r = client.responses.create(model=config.OPENAI_MODEL, input=items, tools=TOOLS,
                                    instructions=INSTRUCTIONS.replace("{language}", LANG.get(lang, "Russian")),
                                    reasoning={"effort": config.AI_REASONING})
        calls = [o for o in r.output if o.type == "function_call"]
        if not calls:
            return {"answer": r.output_text, "tools_used": trace, "model": config.OPENAI_MODEL,
                    "latency_s": round(time.time() - t0, 2)}
        items += r.output
        for call in calls:
            args = json.loads(call.arguments or "{}")
            result = getattr(tools, call.name)(**args)
            trace.append({"tool": call.name, "args": args})
            items.append({"type": "function_call_output", "call_id": call.call_id,
                          "output": json.dumps(result, ensure_ascii=False, default=str)})
    raise RuntimeError(f"navigator did not finish within {TOOL_ROUNDS} tool rounds")
