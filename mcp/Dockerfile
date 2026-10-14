FROM python:3.12-slim
RUN pip install --no-cache-dir mcp==2.2.0 'psycopg[binary]==3.2.9' pillow==12.3.0 && useradd -m tarik
USER tarik
WORKDIR /app
COPY *.py .
CMD ["python", "server.py"]
