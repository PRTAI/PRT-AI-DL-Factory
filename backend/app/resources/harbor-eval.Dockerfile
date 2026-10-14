FROM python:3.12-slim

# Judge 初始化使用包内价格表，避免 GitHub 请求失败触发后台重试和导入异常。
ENV LITELLM_LOCAL_MODEL_COST_MAP=True

RUN useradd -m -u 1000 agent
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git bash jq ripgrep unzip nodejs npm \
    libreoffice-calc libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.114 @openai/codex@0.155.1 \
    && mkdir -p /root/.local/bin \
    && ln -sf "$(which claude)" /root/.local/bin/claude \
    && ln -sf "$(which codex)" /root/.local/bin/codex \
    && claude --version && codex --version
RUN python -m pip install --no-cache-dir \
    "harbor-rewardkit[all]==0.1.7" "litellm==1.101.0" markitdown \
    openpyxl pandas python-docx pypdf python-pptx PyYAML chardet matplotlib

# 构建阶段即检查 Judge 依赖，避免评测运行到 verifier 才暴露安装问题。
RUN python -m pip check \
    && python -c "import litellm, rewardkit; from rewardkit.judges import arun_agent, arun_llm; from importlib.metadata import version; assert litellm.model_cost; print('litellm=' + version('litellm')); print('harbor-rewardkit=' + version('harbor-rewardkit'))" \
    && rewardkit --help

WORKDIR /app
