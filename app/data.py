"""Loading case data. TODO(case): parse the files the case provides."""
from pathlib import Path

from app.config import DATA_DIR


def list_sample_files() -> list[Path]:
    return sorted(p for p in (DATA_DIR / "sample").glob("*") if p.is_file() and p.name != ".gitkeep")
