FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY data/migrations/ ./data/migrations/
COPY data/seed_places.py ./data/seed_places.py

RUN pip install --no-cache-dir -e .

VOLUME ["/var/lib/aviapars"]

CMD ["sh", "-c", "python -m aviapars.db.migrate && python data/seed_places.py && python -m aviapars"]
