# Build from the repository root:  docker build -f experio_client_mcp/Dockerfile -t experio-client-mcp .
FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home mcp
WORKDIR /srv
COPY experio_client_mcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY experio_client_mcp ./experio_client_mcp
RUN rm -rf experio_client_mcp/tests
USER mcp
ENV HOST=0.0.0.0 PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["python", "-m", "experio_client_mcp"]
