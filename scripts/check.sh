#!/usr/bin/env bash
# Runs the main check scenario end to end. TODO(case): replace the input with the sample scenario.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m app.cli "What time is it in Astana?"
