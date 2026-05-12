FROM python:3.12-slim

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    postgresql-client \
    redis-tools \
    && groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin appuser \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/admin_panel/requirements.txt /app/services/admin_panel/requirements.txt
RUN pip install --no-cache-dir -r /app/services/admin_panel/requirements.txt

COPY . /app
RUN chmod +x /app/scripts/*.sh \
    && install -d -o "${APP_UID}" -g "${APP_GID}" /app/uploads /tmp/vzaimno_uploads \
    && chown -R "${APP_UID}:${APP_GID}" /app
USER appuser:appuser

EXPOSE 8001

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "services.admin_panel.app.main:app", "--host", "0.0.0.0", "--port", "8001"]
