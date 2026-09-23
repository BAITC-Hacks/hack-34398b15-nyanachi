# API examples

All examples use the `openai` Python package. OpenRouter and a vLLM server on Brev speak the same
OpenAI-compatible protocol; only `base_url`, key and model name change.

Defaults: OpenAI `gpt-6-sol` ($2 in / $10 out per 1M tokens), OpenRouter `qwen/qwen3.8-27b` ($0.42 / $3).

| Example | Needs | Cost |
|---|---|---|
| `openai_example.py` | `OPENAI_API_KEY` in `.env` | event API credits ($50/person) |
| `openrouter_example.py` | `OPENROUTER_API_KEY` in `.env` (`sk-or-...`), 450+ models: Claude, Gemini, Qwen, DeepSeek | pay per token |
| `brev_vllm_example.py` | Brev account + running GPU instance | Brev credits ($50/person), billed per hour |

```bash
pip install -r requirements.txt
cp .env.example .env   # fill keys
python examples/openai_example.py
python examples/openrouter_example.py
```

## Brev: serve your own model on a rented GPU
```bash
# 1. Install the CLI and log in (opens a browser)
bash -c "$(curl -fsSL https://raw.githubusercontent.com/brevdev/brev-cli/main/bin/install-latest.sh)"
brev login

# 2. Find a GPU type and create an instance. Prices seen 23.09: A6000 48 GB $0.60/h, L4 24 GB $0.85/h
brev search
brev create hackalem-gpu --type hyperstack_A6000

# 3. On the instance: start an OpenAI-compatible server
brev shell hackalem-gpu
pip install vllm
vllm serve Qwen/Qwen3-8B --port 8000

# 4. On your machine (new terminal): forward the port and call it
brev port-forward hackalem-gpu --port 8000:8000
python examples/brev_vllm_example.py

# 5. Stop paying when done
brev stop hackalem-gpu      # or: brev delete hackalem-gpu
```
Judges can't use your Brev instance after the event, so don't make the main scenario depend on it.
