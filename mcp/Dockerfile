FROM python:3.12-slim
RUN pip install --no-cache-dir mcp==2.2.0 && useradd -m tarik
USER tarik
WORKDIR /app
COPY server.py .
CMD ["python", "server.py"]
