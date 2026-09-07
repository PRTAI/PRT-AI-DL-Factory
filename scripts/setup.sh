#!/usr/bin/env bash
# One-shot environment setup: conda env + python deps + frontend deps +
# the framework compatibility catalog.
#
#   scripts/setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_NAME="${CONDA_ENV:-prt-ai-dl-factory}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

# shellcheck disable=SC1091
if [[ -f /opt/miniconda3/etc/profile.d/conda.sh ]]; then
  source /opt/miniconda3/etc/profile.d/conda.sh
elif [[ -n "${CONDA_EXE:-}" ]]; then
  source "$(dirname "$(dirname "$CONDA_EXE")")/etc/profile.d/conda.sh"
else
  echo "✗ 未找到 conda，请先安装 Miniconda/Anaconda" >&2
  exit 1
fi

if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "▶ creating conda env $ENV_NAME (python $PYTHON_VERSION)"
  conda create -y -n "$ENV_NAME" "python=$PYTHON_VERSION"
fi

conda activate "$ENV_NAME"
echo "▶ installing python dependencies"
pip install -r requirements.txt

echo "▶ generating framework support catalog"
PYTHONPATH="$REPO_ROOT" python scripts/gen_framework_support.py

if command -v npm >/dev/null 2>&1; then
  echo "▶ installing frontend dependencies"
  (cd frontend && npm install --no-audit --no-fund)
else
  echo "! 未找到 npm，跳过前端依赖安装"
fi

cat <<'EOF'

✔ 环境准备完成

启动方式：
  scripts/start_backend.sh                 # API + 已构建的前端  (http://localhost:8000)
  scripts/start_frontend.sh                # 前端开发服务器      (http://localhost:5173)
  scripts/start_frontend.sh build          # 构建前端到 frontend/dist

首次启动会自动创建管理员账号（见 backend/.env.example 中的
PRT_BOOTSTRAP_ADMIN_EMAIL / PRT_BOOTSTRAP_ADMIN_PASSWORD），请登录后立即修改密码。
EOF
