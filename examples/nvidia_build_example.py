"""NVIDIA Build (build.nvidia.com): hosted open models behind an OpenAI-compatible API.

Needs in .env: NVIDIA_API_KEY (starts with nvapi-, from build.nvidia.com -> API keys),
optional NVIDIA_MODEL. Model list: curl https://integrate.api.nvidia.com/v1/models
Run: python examples/nvidia_build_example.py
"""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ["NVIDIA_API_KEY"],
)
MODEL = os.getenv("NVIDIA_MODEL", "openai/gpt-oss-20b")

resp = client.chat.completions.create(
    model=MODEL,
    messages=[
        {"role": "system", "content": "Answer briefly."},
        {"role": "user", "content": "Name three industries in Kazakhstan where AI agents help most."},
    ],
    max_tokens=512,
)
print(resp.choices[0].message.content)
