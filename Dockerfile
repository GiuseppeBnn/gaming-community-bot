FROM python:3.11-slim

WORKDIR /app

# Build deps for asyncpg / psycopg; removed from the final layer's apt lists.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first so the layer is cached across source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source lives under src/ (src-layout). Running `python src/main.py`
# puts /app/src on sys.path, so `from config_data.config import ...` resolves.
COPY src/ ./src/

# Writable data dir for the SQLite fallback (overridden by the ./data volume in compose).
RUN mkdir -p /app/data \
    && useradd -m -u 1001 botuser \
    && chown -R botuser:botuser /app   \
    && mkdir -p /app/backups && chown -R botuser:botuser /app/backups


USER botuser

CMD ["python", "src/main.py"]
