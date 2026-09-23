"""Generic tool-calling loop over the OpenAI Responses API.

run_agent(user_input) -> (final_text, trace). trace lists every tool call and result,
so the UI can show what the agent did.
"""
import json

from openai import OpenAI

from app import config
from app.tools import REGISTRY

# TODO(case): describe the user, the task, the rules and the output format.
SYSTEM_PROMPT = """You are an AI agent. Answer in the user's language (Kazakh, Russian or English).
Use the available tools when they help. If you lack data, say so plainly."""

MAX_STEPS = 8


def run_agent(user_input: str, history: list | None = None) -> tuple[str, list[dict]]:
    config.require_key()
    client = OpenAI(api_key=config.OPENAI_API_KEY)
    items: list = list(history or []) + [{"role": "user", "content": user_input}]
    tools = [t["schema"] for t in REGISTRY.values()]
    trace: list[dict] = []

    for _ in range(MAX_STEPS):
        resp = client.responses.create(
            model=config.OPENAI_MODEL,
            instructions=SYSTEM_PROMPT,
            input=items,
            tools=tools,
        )
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            return resp.output_text, trace
        items += resp.output
        for c in calls:
            args = json.loads(c.arguments or "{}")
            result = REGISTRY[c.name]["fn"](**args)
            trace.append({"tool": c.name, "args": args, "result": result})
            items.append({
                "type": "function_call_output",
                "call_id": c.call_id,
                "output": json.dumps(result, ensure_ascii=False, default=str),
            })
    raise RuntimeError(f"Agent did not finish within {MAX_STEPS} steps")
