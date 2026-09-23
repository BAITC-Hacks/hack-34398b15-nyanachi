"""OpenAI API (ChatGPT models): one text call + one structured-JSON call.

Needs in .env: OPENAI_API_KEY (from the $50 event credits), optional OPENAI_MODEL.
Run: python examples/openai_example.py
"""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
MODEL = os.getenv("OPENAI_MODEL", "gpt-6-sol")

# 1. Plain text
resp = client.responses.create(
    model=MODEL,
    instructions="Answer briefly.",
    input="Астана қай жылы астана болды? Ответь на казахском и русском.",
)
print("TEXT:", resp.output_text)

# 2. Structured output: the model must return JSON matching the schema
resp = client.responses.create(
    model=MODEL,
    input="Extract fields: 'Айгерим, 29 лет, из Караганды, жалоба: нет отопления 3 дня'",
    text={"format": {
        "type": "json_schema",
        "name": "request",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
                "city": {"type": "string"},
                "problem": {"type": "string"},
            },
            "required": ["name", "age", "city", "problem"],
            "additionalProperties": False,
        },
    }},
)
print("JSON:", json.loads(resp.output_text))
