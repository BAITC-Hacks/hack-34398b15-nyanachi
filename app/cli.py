"""Headless run for the check scenario: python -m app.cli "input text" """
import json
import sys

from app.agent import run_agent

if __name__ == "__main__":
    answer, trace = run_agent(" ".join(sys.argv[1:]) or "What time is it in Astana?")
    for step in trace:
        print("TOOL", json.dumps(step, ensure_ascii=False, default=str))
    print("ANSWER", answer)
