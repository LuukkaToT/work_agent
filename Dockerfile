FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY work_agent ./work_agent
COPY sql ./sql
COPY config ./config
COPY scripts ./scripts
COPY deploy/entrypoint.py ./deploy/entrypoint.py

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/workspace \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# LLM 请求长、advisory lock 占连接，worker 先保持 1。
ENTRYPOINT ["python", "/app/deploy/entrypoint.py"]
