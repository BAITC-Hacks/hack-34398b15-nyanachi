"""Settings from environment / .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-sol")
OPENAI_FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-6-luna")
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
