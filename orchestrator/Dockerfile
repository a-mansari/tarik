# Tarik's orchestrator: LangGraph's MIT parts run the workflows and the agent, server.py serves them (BSD).
FROM python:3.12-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd -m tarik
USER tarik
WORKDIR /app
CMD ["python", "server.py"]
