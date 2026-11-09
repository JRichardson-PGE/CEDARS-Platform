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
# The pinned ReCET engine (engine/README.md), when its wheel was built into engine/wheels/ before the image: the Results, Compare and
# Edit inputs pages call its code. The build never sees a token; without the wheel those pages say the engine is not installed.
COPY engine ./engine
RUN if ls engine/wheels/recet-*.whl >/dev/null 2>&1; then uv pip install --python /opt/venv/bin/python engine/wheels/recet-*.whl; fi
COPY manage.py ./
COPY cedars ./cedars
COPY data ./data
RUN uv sync --locked --no-dev --inexact \
 && DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput \
 && pip uninstall --yes uv
# uv only builds the image; nothing runs it after this, so it is not left in (CI's image scan, .github/workflows/ci.yml)

USER cedars
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["gunicorn", "cedars.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
