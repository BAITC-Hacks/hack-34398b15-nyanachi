"""FastAPI app. Run: ./run.sh  (or: uvicorn app.main:app)"""
from pathlib import Path

import base64
import hashlib
import hmac
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
engine.hr_summary(STORE)  # warm the HR cache so the first HR visit is instant


def _sign(r: str) -> str:
    return hmac.new(config.APP_SECRET.encode(), r.encode(), hashlib.sha256).hexdigest()


def make_token(r: str) -> str:
    return base64.urlsafe_b64encode(r.encode()).decode().rstrip("=") + "." + _sign(r)


def role(authorization: str = Header(default=""), x_auth_token: str = Header(default="")) -> str:
    """Token from POST /api/login, sent as `X-Auth-Token` (used by the web UI, so it does not clash with a
    reverse proxy's Basic auth) or `Authorization: Bearer`. Returns 'hr' or 'employee:E0028'; 401 if missing or forged."""
    token = x_auth_token.strip() or authorization.removeprefix("Bearer ").strip()
    if "." not in token:
        raise HTTPException(401, "Log in first: POST /api/login")
    body, sig = token.rsplit(".", 1)
    try:
        r = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(401, "Invalid token")
    if not hmac.compare_digest(sig, _sign(r)):
        raise HTTPException(401, "Invalid token")
    return r


class LoginIn(BaseModel):
    role: str
    employee_id: str | None = None
    password: str | None = None


@app.post("/api/login")
def login(body: LoginIn):
    """Demo login. Employees pick their ID (in production this comes from the bank's SSO); HR needs HR_PASSWORD."""
    if body.role == "hr":
        if not hmac.compare_digest(body.password or "", config.HR_PASSWORD):
            raise HTTPException(401, "Wrong HR password")
        return {"role": "hr", "token": make_token("hr")}
    if body.role == "employee" and body.employee_id in STORE.employees:
        r = f"employee:{body.employee_id}"
        return {"role": r, "token": make_token(r)}
    raise HTTPException(401, "Unknown employee")


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
            "skills": len(STORE.skills), "history": len(STORE.history), "ai_enabled": bool(config.OPENAI_API_KEY)}


@app.get("/api/employees")
def list_employees(r: str = Depends(role)):
    """Company directory (name, role, grade) for any logged-in user; no skills or engagement data."""
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
    c = engine.candidates(STORE, emp_id)
    reason = engine.eligibility(STORE, c["employee"], STORE.events[body.event_id], c["levels"], c["target"])
    if reason not in engine.COMPLETABLE:
        raise HTTPException(422, f"This activity cannot be marked as done: {reason}")
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


@app.get("/api/hr/ai-cost")
def hr_ai_cost(r: str = Depends(role)):
    """AI budget: real token usage on this server and a monthly estimate per employee."""
    require_hr(r)
    return engine.ai_cost(STORE)


@app.get("/api/hr/event-draft")
def hr_event_draft(skill_id: str, r: str = Depends(role)):
    """Pre-filled new activity for a catalogue gap (HR)."""
    require_hr(r)
    try:
        return engine.event_draft(STORE, skill_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/hr/events")
def hr_create_event(spec: dict, r: str = Depends(role)):
    """HR event builder: add an activity to the catalogue; it is recommended immediately where it fits."""
    require_hr(r)
    try:
        return engine.create_event(STORE, spec)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"Invalid activity: {e}")


class KudosIn(BaseModel):
    to_employee_id: str
    message: str = ""


@app.post("/api/employees/{emp_id}/kudos")
def kudos(emp_id: str, body: KudosIn, r: str = Depends(role)):
    """Peer recognition: thank a colleague in your department (they get points; nothing is public)."""
    if r != f"employee:{emp_id}":
        raise HTTPException(403, "Only the employee can send their own thanks")
    try:
        return engine.send_kudos(STORE, emp_id, body.to_employee_id, body.message)
    except ValueError as e:
        raise HTTPException(422, str(e))


class FeedbackIn(BaseModel):
    event_id: str
    reason: str


@app.post("/api/employees/{emp_id}/feedback")
def feedback(emp_id: str, body: FeedbackIn, r: str = Depends(role)):
    """'Not now' with a reason: format | time | not_interested. The engine adapts; nothing is punished."""
    can_see(emp_id, r)
    if body.event_id not in STORE.events:
        raise HTTPException(404, f"Unknown event {body.event_id}")
    try:
        return engine.dismiss(STORE, emp_id, body.event_id, body.reason)
    except ValueError as e:
        raise HTTPException(422, str(e))


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
    return {**engine.hr_summary(STORE, department or None), "ai_cost": engine.ai_cost(STORE)}


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
    snapshot = (dict(STORE.employees), list(STORE.history))  # all-or-nothing: a bad file leaves the data unchanged
    try:
        if employees:
            out["added_employees"] = STORE.merge_employees(json.loads((await employees.read()).decode("utf-8-sig")))
        if history:
            out["added_history"] = STORE.merge_history((await history.read()).decode("utf-8-sig"))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        STORE.employees, STORE.history = snapshot
        STORE.data_version += 1
        raise HTTPException(422, f"Invalid upload: {e}")
    except Exception:
        STORE.employees, STORE.history = snapshot  # never leave a half-applied upload behind, then fail loudly
        STORE.data_version += 1
        raise
    return out


EXAMPLES = {"employees.json": ROOT / "eval" / "trap_employees.json",
            "activity_history.csv": ROOT / "eval" / "trap_history.csv"}


@app.get("/api/data/examples/{name}")
def example_file(name: str):
    """Example upload files (the synthetic trap profiles from eval/) so HR can see the format."""
    if name not in EXAMPLES:
        raise HTTPException(404, "Unknown example file")
    return FileResponse(EXAMPLES[name], filename=name)


@app.post("/api/data/upload-example")
def upload_example(r: str = Depends(role)):
    """HR: load the example files in one click (same merge as a manual upload)."""
    require_hr(r)
    return {"added_employees": STORE.merge_employees(json.loads(EXAMPLES["employees.json"].read_text("utf-8"))),
            "added_history": STORE.merge_history(EXAMPLES["activity_history.csv"].read_text("utf-8"))}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
