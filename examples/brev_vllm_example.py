"""NVIDIA Brev: call a model you serve yourself on a rented GPU (steps in examples/README.md).

After `brev port-forward <instance> --port 8000:8000`, the vLLM server is on localhost:8000.
Run: python examples/brev_vllm_example.py
"""
import os

from openai import OpenAI

client = OpenAI(base_url=os.getenv("BREV_BASE_URL", "http://localhost:8000/v1"), api_key="not-needed")
MODEL = os.getenv("BREV_MODEL", "Qwen/Qwen3-8B")

resp = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "Say hello in Kazakh, Russian and English."}],
    max_tokens=256,
)
print(resp.choices[0].message.content)
