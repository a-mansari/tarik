# Tarik's orchestrator: LangGraph's MIT parts run the workflows and the agent, tarik/ holds the tools, app/server.py serves
# them all (BSD). The Claude Code CLI is the model when a cabinet picks it (MODEL_ADAPTER, tarik/core/claude_code.py).
# The team's pages (web/, Vue), built first: only their files reach the image.
FROM node:24-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd -m tarik
USER tarik
RUN curl -fsSL https://claude.ai/install.sh | bash -s 2.1.300
ENV PATH="/home/tarik/.local/bin:${PATH}"
WORKDIR /app
COPY --from=web /web/dist /app/web/dist
CMD ["python", "-m", "app"]
