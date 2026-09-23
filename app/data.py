"""Load and validate the Career Quest dataset (starter-kit format) into an in-memory store.

The jury uploads extra profiles/history in the same format; `Store.merge_*` adds them.
"""
import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

GRADES = ["Junior", "Middle", "Senior", "Lead"]
SKIP_STATUSES = {"no_show", "declined", "dropped"}


class CareerGoal(BaseModel):
    target_role: str
    target_grade: str


class Employee(BaseModel):
    """Starter-kit schema. Only employee_id, role, grade, skills are strictly needed; the rest gets safe defaults
    so a minimal profile like the case example ({employee_id, role, grade, tenure_months, skills}) can be uploaded."""
    employee_id: str
    full_name: str = ""
    department: str = ""
    role: str
    grade: str
    manager_id: str | None = None
    hire_date: str = ""
    tenure_months: int = 12
    work_format: str = "office"
    preferred_language: str = "ru"
    career_goal: CareerGoal | None = None
    skills: dict[str, int] = Field(default_factory=dict)
    # Missing review date = skill levels are current; no post-review gains are added on top.
    last_review_date: str = "9999-12-31"

    def model_post_init(self, _ctx) -> None:
        if not self.full_name:
            self.full_name = self.employee_id
        if not self.department:
            self.department = self.role


class SkillGain(BaseModel):
    skill_id: str
    gain: int
    max_level: int


class Event(BaseModel):
    event_id: str
    title: str
    description: str = ""
    type: str
    format: str
    duration_hours: float
    mandatory: bool
    target_roles: list[str]
    target_grades: list[str]
    develops_skills: list[SkillGain]
    prerequisites: dict[str, int] = Field(default_factory=dict)
    upcoming_sessions: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    skill_id: str
    name: str
    type: str
    category: str
    description: str = ""


class RoleProfile(BaseModel):
    role: str
    grade: str
    required_skills: dict[str, int]
    critical_skills: list[str]


class HistoryRow(BaseModel):
    record_id: str
    employee_id: str
    event_id: str
    date: str
    due_date: str = ""
    status: str
    completion_pct: int
    score: int | None = None
    feedback_rating: int | None = None
    assigned_by: str


def _opt_int(v: str) -> int | None:
    return int(v) if v not in ("", None) else None


def parse_history_csv(text: str) -> list[HistoryRow]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r["score"] = _opt_int(r.get("score", ""))
        r["feedback_rating"] = _opt_int(r.get("feedback_rating", ""))
        r["completion_pct"] = int(r["completion_pct"])
        rows.append(HistoryRow(**r))
    return rows


@dataclass
class Store:
    as_of: str
    employees: dict[str, Employee]
    events: dict[str, Event]
    skills: dict[str, Skill]
    role_profiles: dict[tuple[str, str], RoleProfile]
    history: list[HistoryRow]
    proficiency_scale: dict[str, str] = field(default_factory=dict)
    # Runtime gamification state (in memory, per employee): points ledger and accepted challenges.
    ledger: list[dict] = field(default_factory=list)
    challenges: dict[str, list[dict]] = field(default_factory=dict)
    shared_gardens: set[str] = field(default_factory=set)  # employees who opted in to share their garden
    dismissed: dict[str, dict[str, str]] = field(default_factory=dict)  # emp -> {event_id: reason} from "Not now"
    usage: list[dict] = field(default_factory=list)  # every LLM call: kind, model, input/output tokens
    ai_cache: dict = field(default_factory=dict)
    hr_cache: dict = field(default_factory=dict)  # HR summary per data state (recomputing 200 profiles takes ~1 s)  # (emp, profile-state) -> AI recommendation, avoids repeat LLM calls

    def history_of(self, employee_id: str) -> list[HistoryRow]:
        return [h for h in self.history if h.employee_id == employee_id]

    def merge_employees(self, payload: dict) -> list[str]:
        items = payload["employees"] if isinstance(payload, dict) else payload
        added = []
        for raw in items:
            e = Employee(**raw)
            if (e.role, e.grade) not in self.role_profiles:
                raise ValueError(f"{e.employee_id}: unknown role/grade {e.role}/{e.grade}")
            self.employees[e.employee_id] = e
            added.append(e.employee_id)
        return added

    def merge_history(self, text: str) -> int:
        rows = parse_history_csv(text)
        # A row is a duplicate only if it is the same participation, not just the same record_id:
        # uploaded files may number their records from R000001 again.
        key = lambda h: (h.record_id, h.employee_id, h.event_id, h.date, h.status)
        known = {key(h) for h in self.history}
        for r in rows:
            if r.employee_id not in self.employees:
                raise ValueError(f"{r.record_id}: unknown employee {r.employee_id}")
            if r.event_id not in self.events:
                raise ValueError(f"{r.record_id}: unknown event {r.event_id}")
        new = [r for r in rows if key(r) not in known]
        self.history.extend(new)
        self.history.sort(key=lambda h: (h.date, h.employee_id, h.event_id))
        return len(new)

    def next_record_id(self) -> str:
        n = max((int(h.record_id.lstrip("RT")) for h in self.history if h.record_id.lstrip("RT").isdigit()), default=0)
        return f"R{n + 1:06d}"


def load_store(data_dir: Path) -> Store:
    emp = json.loads((data_dir / "employees.json").read_text(encoding="utf-8"))
    evs = json.loads((data_dir / "events.json").read_text(encoding="utf-8"))
    sk = json.loads((data_dir / "skills.json").read_text(encoding="utf-8"))
    hist = parse_history_csv((data_dir / "activity_history.csv").read_text(encoding="utf-8"))
    store = Store(
        as_of=evs["meta"]["as_of_date"],
        employees={e["employee_id"]: Employee(**e) for e in emp["employees"]},
        events={e["event_id"]: Event(**e) for e in evs["events"]},
        skills={s["skill_id"]: Skill(**s) for s in sk["skills"]},
        role_profiles={(r["role"], r["grade"]): RoleProfile(**r) for r in sk["role_profiles"]},
        history=hist,
        proficiency_scale=sk.get("proficiency_scale", {}),
    )
    return store
