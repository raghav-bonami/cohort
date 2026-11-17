
# Cohort Insights API (take-home assignment)

## Context

- Take-home for a Backend Engineer role. Design + debrief matter more than features.
- Full design and build order: see PLAN.md. Follow it; flag deviations before making them.
- Assignment brief is confidential; never commit the PDF.

## Stack

- Python, FastAPI, Pydantic v2, pydantic-settings
- MongoDB via pymongo AsyncMongoClient (not Motor)
- Redis via redis.asyncio
- structlog JSON logging; pytest + pytest-asyncio + httpx
- Docker Compose: api, worker, mongo, redis

## Conventions

- App code lives in app/ (entry point app/main.py); worker in app/worker/
- Routers never touch Mongo directly; go through repositories/
- Never `except Exception: pass`; log every fallback
- Use service names (mongo, redis) in container URLs, not localhost

## Commands

- Run stack: docker compose up --build
- Tests: pytest
- Lint: ruff check . && ruff format .

## How I want to work

- Explain changes in plain language; I must defend every decision in the debrief.
- Ask before making changes unless I say "go ahead".
- Windows + PowerShell environment.

## Progress

- [X] Phase 0: repo, venv, requirements, Docker installed
- [ ] Phase 1: skeleton + /health
