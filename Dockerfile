# Tarik's orchestrator: LangGraph's MIT parts run the workflows and the agent, engine/tools/ holds the tools, server/server.py serves
# them all (BSD). The Claude Code CLI is the model when a cabinet picks it (MODEL_ADAPTER, engine/tools/core/claude_code.py).
# The team's pages (server/web/, Vue), built first: only their files reach the image.
FROM node:24-alpine AS web
WORKDIR /web
COPY server/web/package.json server/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY server/web/ ./
RUN npm run build

FROM python:3.14-slim
# The Claude Code CLI, for a cabinet whose model is Claude (MODEL_ADAPTER claude-code): INSTALL_CLAUDE_CODE=0 leaves it
# out. Its installer's address and version come from the build arguments (.env.example; docker-compose.yml passes them).
ARG INSTALL_CLAUDE_CODE=1
ARG CLAUDE_INSTALL_URL
ARG CLAUDE_CODE_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd -m tarik
USER tarik
# Downloaded then run, not piped: a failed download fails the build (sh has no pipefail), and so does an empty argument.
RUN if [ "$INSTALL_CLAUDE_CODE" = "1" ]; then \
      [ -n "$CLAUDE_INSTALL_URL" ] && [ -n "$CLAUDE_CODE_VERSION" ] \
        || { echo "CLAUDE_INSTALL_URL and CLAUDE_CODE_VERSION are required (or INSTALL_CLAUDE_CODE=0)"; exit 1; }; \
      curl -fsSL "$CLAUDE_INSTALL_URL" -o /tmp/claude-install.sh && bash /tmp/claude-install.sh "$CLAUDE_CODE_VERSION"; \
      rm -f /tmp/claude-install.sh; \
    fi
ENV PATH="/home/tarik/.local/bin:${PATH}"
WORKDIR /app
# The code is in the image, so it runs without any mount (docker-compose.prod.yml); docker-compose.yml mounts the
# working copy over it for development. The pages' sources are dropped: the server serves only their build.
COPY --chown=tarik:tarik engine/ /app/engine/
COPY --chown=tarik:tarik server/ /app/server/
COPY --chown=tarik:tarik check.py run.py /app/
RUN find /app/server/web -mindepth 1 -maxdepth 1 -exec rm -rf {} +
COPY --from=web /web/dist /app/server/web/dist
CMD ["python", "-m", "server"]
