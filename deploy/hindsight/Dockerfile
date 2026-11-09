# Hindsight (Tarik's memory, engine/memory.py) with the Claude Code CLI added: its fact extraction runs on the same Claude
# subscription as a Claude cabinet's model (HINDSIGHT_API_LLM_PROVIDER=claude-code). The stock image has no CLI, nor the
# curl its installer needs. Installed as the main Dockerfile does: downloaded then run, and an empty argument fails the build.
FROM ghcr.io/vectorize-io/hindsight:1.2.0
ARG CLAUDE_INSTALL_URL
ARG CLAUDE_CODE_VERSION
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
USER hindsight
RUN [ -n "$CLAUDE_INSTALL_URL" ] && [ -n "$CLAUDE_CODE_VERSION" ] \
      && curl -fsSL "$CLAUDE_INSTALL_URL" -o /tmp/claude-install.sh \
      && bash /tmp/claude-install.sh "$CLAUDE_CODE_VERSION" \
      && rm -f /tmp/claude-install.sh
ENV PATH="/home/hindsight/.local/bin:${PATH}"
