# The platform's web image: Django behind gunicorn, static files served by WhiteNoise. Builds for amd64 and arm64.
#   docker compose up --build        (docker-compose.yml: the app and its PostgreSQL for development)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DJANGO_SETTINGS_MODULE=cedars.settings.prod

RUN pip install --no-cache-dir "uv>=0.11,<0.12" \
 && useradd --create-home --uid 10001 cedars
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY manage.py ./
COPY cedars ./cedars
COPY data ./data
RUN uv sync --locked --no-dev \
 && DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

USER cedars
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["gunicorn", "cedars.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
