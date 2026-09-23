"""Tools the agent can call. Case-specific tools go here.

Register a tool with @tool(description, parameters_json_schema). The function receives the
parsed arguments as keyword args and returns anything JSON-serialisable.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

REGISTRY: dict[str, dict] = {}


def tool(description: str, parameters: dict):
    def wrap(fn):
        REGISTRY[fn.__name__] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "name": fn.__name__,
                "description": description,
                "parameters": parameters,
            },
        }
        return fn
    return wrap


# Example tool. Replace or extend with the case tools.
@tool("Current date and time in Astana.", {"type": "object", "properties": {}, "required": [], "additionalProperties": False})
def current_datetime() -> str:
    return datetime.now(ZoneInfo("Asia/Almaty")).strftime("%Y-%m-%d %H:%M")
