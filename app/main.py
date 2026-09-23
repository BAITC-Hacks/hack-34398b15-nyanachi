"""FastAPI app. Run: ./run.sh  (or: uvicorn app.main:app)"""
from pathlib import Path

import json

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import agent, chat as navigator, engine
from app import config
from app.config import DATA_DIR, ROOT
from app.data import load_store

app = FastAPI(title="Career Quest")
STORE = load_store(Path(DATA_DIR))


def role(x_role: str = Header(default="hr")) -> str:
    """Demo auth: 'hr' or 'employee:E0028'. Real SSO is out of scope."""
    return x_role


def can_see(emp_id: str, r: str) -> None:
    if r != "hr" and r != f"employee:{emp_id}":
        raise HTTPException(403, "Employees can only see their own profile")
    if emp_id not in STORE.employees:
        raise HTTPException(404, f"Unknown employee {emp_id}")


def require_hr(r: str) -> None:
    if r != "hr":
        raise HTTPException(403, "HR only")


@app.get("/api/meta")
def meta():
    return {"as_of": STORE.as_of, "employees": len(STORE.employees), "events": len(STORE.events),
            "skills": len(STORE.skills), "history": len(STORE.history)}


@app.get("/api/employees")
def list_employees(r: str = Depends(role)):
    require_hr(r)
    return [{"employee_id": e.employee_id, "full_name": e.full_name, "role": e.role, "grade": e.grade}
            for e in STORE.employees.values()]


@app.get("/api/employees/{emp_id}")
def profile(emp_id: str, r: str = Depends(role)):
    can_see(emp_id, r)
    c = engine.candidates(STORE, emp_id)
    return {
        "employee": c["employee"].model_dump(), "effective_skills": c["levels"],
        "applied_after_review": c["applied_after_review"], "target": c["target"], "gaps": c["gaps"],
        "uncovered_gaps": c["uncovered_gaps"], "readiness": c["readiness"], "signals": c["signals"],
        "readiness_breakdown": engine.readiness_breakdown(c["levels"], c["target"]),
        "completed": [h.model_dump() for h in STORE.history_of(emp_id) if h.status == "completed"],
        "eligible_steps": c["candidates"], "excluded": c["excluded"],
    }


@app.post("/api/employees/{emp_id}/recommend")
def recommend(emp_id: str, ai: bool = True, r: str = Depends(role)):
    """1–3 next steps. ai=true uses gpt-6-sol (fallback gpt-6-luna → templates); ai=false is rules-only."""
    can_see(emp_id, r)
    return agent.recommend(STORE, emp_id, use_ai=ai)


@app.get("/api/employees/{emp_id}/path")
def path(emp_id: str, steps: int = 5, r: str = Depends(role)):
    """What-if: take the best step, apply it, re-plan — up to `steps` times. Nothing is saved."""
    can_see(emp_id, r)
    return engine.simulate_path(STORE, emp_id, max_steps=max(1, min(steps, 8)))


@app.get("/api/employees/{emp_id}/mentors")
def mentors(emp_id: str, r: str = Depends(role)):
    """Suggested mentors for the employee's biggest gaps (same department, Senior/Lead, mentoring-ready)."""
    can_see(emp_id, r)
    return engine.find_mentors(STORE, emp_id)


class CompleteIn(BaseModel):
    event_id: str


@app.post("/api/employees/{emp_id}/complete")
def complete(emp_id: str, body: CompleteIn, r: str = Depends(role)):
    can_see(emp_id, r)
    if body.event_id not in STORE.events:
        raise HTTPException(404, f"Unknown event {body.event_id}")
    res = engine.complete_event(STORE, emp_id, body.event_id)
    res["points"] = engine.award_completion(STORE, emp_id, body.event_id, res["changed"])
    return res


@app.get("/api/employees/{emp_id}/wallet")
def get_wallet(emp_id: str, r: str = Depends(role)):
    """Private points balance, rewards and challenge offer. Employees see only their own; no rankings exist."""
    can_see(emp_id, r)
    return engine.wallet(STORE, emp_id)


@app.post("/api/employees/{emp_id}/challenge")
def accept(emp_id: str, r: str = Depends(role)):
    can_see(emp_id, r)
    try:
        return engine.accept_challenge(STORE, emp_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/employees/{emp_id}/garden")
def get_garden(emp_id: str, r: str = Depends(role)):
    """Skill garden + personal level. Colleagues' gardens only with mutual opt-in in the same department."""
    can_see(emp_id, r)
    return engine.garden(STORE, emp_id)


class ShareIn(BaseModel):
    on: bool


@app.post("/api/employees/{emp_id}/share")
def share(emp_id: str, body: ShareIn, r: str = Depends(role)):
    can_see(emp_id, r)
    return {"shared": engine.set_share(STORE, emp_id, body.on)}


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/employees/{emp_id}/chat")
def chat(emp_id: str, body: ChatIn, r: str = Depends(role)):
    """Career navigator: an agent that answers by calling engine tools about this employee only."""
    can_see(emp_id, r)
    if not body.message.strip():
        raise HTTPException(422, "Empty message")
    if not config.OPENAI_API_KEY:
        raise HTTPException(503, "The AI navigator needs OPENAI_API_KEY in .env; recommendations work without it.")
    return navigator.chat(STORE, emp_id, body.message[:1000], body.history)


class RedeemIn(BaseModel):
    reward_id: str


@app.post("/api/employees/{emp_id}/redeem")
def redeem(emp_id: str, body: RedeemIn, r: str = Depends(role)):
    can_see(emp_id, r)
    try:
        return engine.redeem(STORE, emp_id, body.reward_id)
    except KeyError:
        raise HTTPException(404, f"Unknown reward {body.reward_id}")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/hr/summary")
def hr(department: str | None = None, r: str = Depends(role)):
    require_hr(r)
    return engine.hr_summary(STORE, department or None)


@app.get("/api/skills")
def skills():
    return {s.skill_id: {"name": s.name, "type": s.type, "category": s.category} for s in STORE.skills.values()}


@app.get("/api/events")
def events():
    return [e.model_dump() for e in STORE.events.values()]


@app.get("/api/events/{event_id}/ics")
def event_ics(event_id: str, date: str | None = None):
    """Calendar file (.ics) for an activity session. Works with Outlook, Google Calendar and Apple Calendar."""
    ev = STORE.events.get(event_id)
    if not ev:
        raise HTTPException(404, f"Unknown event {event_id}")
    day = date or next((d for d in sorted(ev.upcoming_sessions) if d >= STORE.as_of), None) or STORE.as_of
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        raise HTTPException(422, "date must be YYYY-MM-DD")
    ymd = day.replace("-", "")
    hours = min(8, max(1, int(round(ev.duration_hours)))) if ev.format != "self_paced" else 1
    esc = lambda t: t.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Career Quest//HackAlem//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{event_id}-{ymd}@careerquest",
        f"DTSTAMP:{STORE.as_of.replace('-', '')}T000000Z",
        f"DTSTART;TZID=Asia/Almaty:{ymd}T100000",
        f"DTEND;TZID=Asia/Almaty:{ymd}T{10 + hours:02d}0000",
        f"SUMMARY:{esc(ev.title)}",
        f"DESCRIPTION:{esc(ev.description + ' (' + ev.format + ', ' + str(ev.duration_hours) + ' h)')}",
        "END:VEVENT", "END:VCALENDAR", ""])
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{event_id}-{ymd}.ics"'})


@app.post("/api/data/upload")
async def upload(employees: UploadFile | None = File(None), history: UploadFile | None = File(None),
                 r: str = Depends(role)):
    """HR/jury: add profiles (employees.json) and history (activity_history.csv) in the starter-kit format."""
    require_hr(r)
    out = {"added_employees": [], "added_history": 0}
    try:
        if employees:
            out["added_employees"] = STORE.merge_employees(json.loads(await employees.read()))
        if history:
            out["added_history"] = STORE.merge_history((await history.read()).decode("utf-8"))
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise HTTPException(422, f"Invalid upload: {e}")
    return out


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
