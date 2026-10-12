FROM python:3.12-slim
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
RUN pip install --no-cache-dir \
    "harbor-rewardkit[all]==0.1.7" markitdown \
    openpyxl pandas python-docx pypdf python-pptx PyYAML chardet matplotlib
WORKDIR /app
