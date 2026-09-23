"""OpenRouter: one key, hundreds of models (Claude, Gemini, Qwen, DeepSeek, GPT-OSS...).

OpenAI-compatible API. Needs in .env: OPENROUTER_API_KEY (sk-or-...), optional OPENROUTER_MODEL.
Model list: curl https://openrouter.ai/api/v1/models
Run: python examples/openrouter_example.py
"""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.8-27b")

resp = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "Сәлем! Answer in one line: what is Astana Hub?"}],
    max_tokens=1000,
)
print(f"[{resp.model}]", resp.choices[0].message.content)
