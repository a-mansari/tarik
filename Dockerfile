# Tarik's orchestrator: LangGraph's MIT parts run the workflows and the agent, tarik/ holds the tools, server.py serves
# them all (BSD).
FROM python:3.14-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd -m tarik
USER tarik
WORKDIR /app
CMD ["python", "server.py"]
