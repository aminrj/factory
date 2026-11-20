# Research: #1
`src/app/main.py` builds the FastAPI app and includes routers from `src/app/routes/`. `routes/health.py` defines `/healthz`
with an `APIRouter`. Tests use `fastapi.testclient.TestClient(create_app())` in `tests/unit/`. The package is named `app`
in `pyproject.toml` (version 0.1.0).
