FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    postgresql-client \
    redis-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY services/admin_panel/requirements.txt /app/services/admin_panel/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/services/admin_panel/requirements.txt

COPY . /app
RUN chmod +x /app/scripts/entrypoint.sh

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "services.admin_panel.app.main:app", "--host", "0.0.0.0", "--port", "8001"]
