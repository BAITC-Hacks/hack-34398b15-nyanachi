# AGENTS.md — guide for AI coding agents

Career Quest: an AI navigator that recommends 1–3 next development steps with a multi-factor rationale, updates progress, and gives HR a view (Halyk Bank case, HackAlem AI 2026). Human docs: `README.md` (RU), `README.en.md`, `README.kk.md`.

## Run and verify
```bash
./run.sh                                      # venv + deps + http://127.0.0.1:8000 (PORT/HOST env to change)
.venv/bin/python -m pytest -q                 # 26 tests, ~5 s
.venv/bin/python -m eval.run_benchmark        # trap profiles, rules mode: Baseline 0/9 · Career Quest 9/9
.venv/bin/python -m eval.run_benchmark --ai   # same with the LLM (needs OPENAI_API_KEY)
```
Without `OPENAI_API_KEY` everything runs in rules mode (template rationales); only the navigator chat needs a key. Settings are env vars, see `.env.example` and `app/config.py`. HR password in the UI: `HR_PASSWORD`, default `hr-demo`.

## Map
- `app/engine.py` — deterministic core, the source of every number: effective skills (completions after `last_review_date`), target (next grade or `career_goal`), eligibility filters, `score_event` with `score_parts`, prerequisite chains, `simulate_path`, mentors, points/garden, HR summary, template rationales (kk/ru/en).
- `app/agent.py` — LLM layer: gpt-6-sol and gpt-6-luna race (Structured Outputs), `_validate` rejects any answer that is worse than the engine (unknown ids, <3 supported factors, missing the top pick, avoided format), falls back to templates. Cached per profile state.
- `app/chat.py` — navigator: tool-calling agent over engine functions, scoped to the logged-in employee.
- `app/data.py` — Pydantic schema, `Store` (in-memory), tolerant upload parsing (`merge_employees`, `merge_history`).
- `app/main.py` — FastAPI endpoints and roles (HMAC tokens from `POST /api/login`, header `X-Auth-Token` or `Authorization: Bearer`).
- `web/index.html` — the whole UI (one file, no build). i18n dict `copy.{en,ru,kk}`; dataset names in `web/i18n_names.json`.
- `data/` — organisers' synthetic starter dataset (do not modify, do not export). `eval/` — our trap profiles and expected answers.

## Invariants — keep them true (tests cover most)
- Recommendations: 1–3 steps, only eligible activities (not mandatory, not completed, audience/prerequisites met, a future session or self-paced, real skill gain toward the target), each with ≥3 factors that are actually true for it. Never pad with useless steps.
- The LLM may rephrase and choose among engine candidates but can never override the engine's top pick or invent facts; the engine decides, the model explains.
- No public rankings; points only for voluntary activities; mandatory training earns nothing; engagement data is private to the employee (HR sees aggregates and support signals only).
- Employee tokens can read only their own profile; HR-only endpoints return 403 for employees. Uploads are all-or-nothing and validated.
- Every user-visible string exists in en, ru and kk.

## Working rules
- Run tests and the benchmark before committing; update README numbers (all three languages) if behaviour changes.
- Never commit `.env` or keys; never send `data/` to external services.
- Fail loudly: no `try/except` that hides errors; return clear 4xx messages to the client.
- Keep changes small and in the style of the surrounding code; `web/index.html` is minified-style, edit it carefully and check that the script still parses (`node --check`).
