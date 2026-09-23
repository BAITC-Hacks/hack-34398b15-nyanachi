"""FastAPI app. Run: ./run.sh  (or: uvicorn app.main:app)"""
from pathlib import Path

import json

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import agent, engine
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
        "completed": [h.model_dump() for h in STORE.history_of(emp_id) if h.status == "completed"],
        "eligible_steps": c["candidates"], "excluded": c["excluded"],
    }


@app.post("/api/employees/{emp_id}/recommend")
def recommend(emp_id: str, ai: bool = True, r: str = Depends(role)):
    """1–3 next steps. ai=true uses gpt-6-sol (fallback gpt-6-luna → templates); ai=false is rules-only."""
    can_see(emp_id, r)
    return agent.recommend(STORE, emp_id, use_ai=ai)


class CompleteIn(BaseModel):
    event_id: str


@app.post("/api/employees/{emp_id}/complete")
def complete(emp_id: str, body: CompleteIn, r: str = Depends(role)):
    can_see(emp_id, r)
    if body.event_id not in STORE.events:
        raise HTTPException(404, f"Unknown event {body.event_id}")
    return engine.complete_event(STORE, emp_id, body.event_id)


@app.get("/api/hr/summary")
def hr(r: str = Depends(role)):
    require_hr(r)
    return engine.hr_summary(STORE)


@app.get("/api/skills")
def skills():
    return {s.skill_id: {"name": s.name, "type": s.type, "category": s.category} for s in STORE.skills.values()}


@app.get("/api/events")
def events():
    return [e.model_dump() for e in STORE.events.values()]


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
