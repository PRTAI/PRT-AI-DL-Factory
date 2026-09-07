#!/usr/bin/env bash
# Start the PRT AI DL Factory API (and the built SPA, if frontend/dist exists).
#
#   scripts/start_backend.sh              # 0.0.0.0:8000
#   PORT=9000 scripts/start_backend.sh    # custom port
#   RELOAD=1 scripts/start_backend.sh     # auto-reload during development
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_NAME="${CONDA_ENV:-prt-ai-dl-factory}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"

# shellcheck disable=SC1091
if [[ -f /opt/miniconda3/etc/profile.d/conda.sh ]]; then
  source /opt/miniconda3/etc/profile.d/conda.sh
elif [[ -n "${CONDA_EXE:-}" ]]; then
  source "$(dirname "$(dirname "$CONDA_EXE")")/etc/profile.d/conda.sh"
fi
conda activate "$ENV_NAME"

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

ARGS=(backend.app.main:app --host "$HOST" --port "$PORT")
if [[ "${RELOAD:-0}" == "1" ]]; then
  ARGS+=(--reload --reload-dir backend)
else
  # The scheduler and telemetry sampler are in-process singletons, so more than
  # one worker would duplicate them. Scale out with a shared DB instead.
  ARGS+=(--workers "$WORKERS")
fi

echo "▶ PRT AI DL Factory  http://${HOST}:${PORT}  (conda env: ${ENV_NAME})"
exec python -m uvicorn "${ARGS[@]}"
