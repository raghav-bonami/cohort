# ---- base: Python + runtime dependencies (shared by both images) -----------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# Copy requirements first so the pip layer is cached between code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ---- test: the suite, run inside compose  (docker compose run --rm tests) --
FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pyproject.toml .
COPY app ./app
COPY tests ./tests

CMD ["python", "-m", "pytest", "-q"]


# ---- runtime: api and worker. Last stage, so it is what `build: .` builds --
FROM base AS runtime

COPY app ./app

# Don't run as root
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
