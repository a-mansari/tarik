FROM python:3.12-slim
RUN pip install --no-cache-dir mcp==2.2.0 'psycopg[binary]==3.2.9' pillow==12.3.0 pypdfium2==5.13.0 httpx==0.28.1 openpyxl==3.1.5 pytest==8.3.5 && useradd -m tarik
USER tarik
WORKDIR /app
COPY . .
CMD ["python", "server.py"]
