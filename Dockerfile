# syntax=docker/dockerfile:1

# ---- web build stage -------------------------------------------------------
FROM node:20-bookworm-slim AS webbuild
WORKDIR /web
COPY web/package.json ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- runtime stage ---------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DATA_PATH=/data/upgrade.db
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY --from=webbuild /web/dist ./web/dist
RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
