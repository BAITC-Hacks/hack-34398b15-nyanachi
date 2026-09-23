# Career Quest — AI navigator for employee development

> Work in progress (HackAlem AI 2026, case by Halyk Bank). Full README is generated at the end of the build.

Career Quest shows an employee their path to the next grade and recommends 1–3 development steps,
each explained by several factors: skill gaps against next-grade requirements, how critical a skill is,
participation history (no-shows, declines, drops by format) and expected skill gain. HR sees which skills
lag, who has no next step and turnout per activity.

## Run
```bash
./run.sh            # http://127.0.0.1:8000  ·  API docs: /docs
```
Optional: `cp .env.example .env` and set `OPENAI_API_KEY` for AI explanations. Without a key the app runs in rules-only mode.

## Test
```bash
.venv/bin/python -m pytest -q
```

## Data
`data/` contains the organisers' synthetic Career Quest starter kit, included only so the solution can be
evaluated. It is not for redistribution. Additional profiles and history in the same format can be uploaded.

## Tools used
Built during the hackathon with AI coding agents (Claude Code, Codex). A small FastAPI/OpenAI template prepared
before the event was replaced by case-specific code.
