"""Settings from environment / .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-sol")
OPENAI_FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-6-luna")
# Any OpenAI-compatible endpoint: OpenRouter, or an on-premise vLLM server. Empty = api.openai.com
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
AI_TIMEOUT_S = float(os.getenv("AI_TIMEOUT_S", "8.5"))
AI_REASONING = os.getenv("AI_REASONING", "none")
AI_CANDIDATES = int(os.getenv("AI_CANDIDATES", "6"))
CHAT_TIMEOUT_S = float(os.getenv("CHAT_TIMEOUT_S", "20"))
# Auth: tokens are HMAC-signed with APP_SECRET (random per start if unset). HR needs HR_PASSWORD.
import secrets as _secrets
APP_SECRET = os.getenv("APP_SECRET") or _secrets.token_hex(32)
HR_PASSWORD = os.getenv("HR_PASSWORD", "hr-demo")
# USD per 1M tokens (input, output) for cost estimates
PRICES = {"gpt-6-sol": (2.0, 10.0), "gpt-6-luna": (0.10, 0.50), "gpt-6-astra": (10.0, 50.0)}
