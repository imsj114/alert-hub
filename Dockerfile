FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY alert_hub /app/alert_hub
COPY scripts /app/scripts
COPY config /app/config

RUN pip install --no-cache-dir .

ENV ALERT_HUB_CONFIG=/app/config/config.yaml
ENV ALERT_HUB_ENV=/app/.env

CMD ["uvicorn", "alert_hub.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
