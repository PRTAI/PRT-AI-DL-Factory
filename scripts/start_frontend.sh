#!/usr/bin/env bash
# Frontend dev server (proxies /api and /ws to the backend on :8000).
#
#   scripts/start_frontend.sh          # vite dev server on :5153
#   PORT=5200 scripts/start_frontend.sh
#   scripts/start_frontend.sh build    # production build into frontend/dist
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/frontend"

if [[ ! -d node_modules ]]; then
  echo "▶ installing frontend dependencies…"
  npm install --no-audit --no-fund
fi

if [[ "${1:-dev}" == "build" ]]; then
  exec npm run build
fi

export PORT="${PORT:-5153}"
echo "▶ 前端开发服务器 http://0.0.0.0:${PORT}  (API 代理到 http://127.0.0.1:8000)"
exec npm run dev
