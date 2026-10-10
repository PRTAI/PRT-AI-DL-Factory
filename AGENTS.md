# Repository Guidelines

## Project Structure & Module Organization

This platform runs model post-training and Harbor agent evaluations through external framework subprocesses.

- `backend/app/`: FastAPI application. Keep request parsing and authorization in `api/`, business logic in `services/`, configuration-to-argv conversion in `builders/`, schemas in `models/`, and persistence in `store/`.
- `backend/tests/`: pytest suite and shared fixtures.
- `frontend/src/`: React/TypeScript pages, components, and shared utilities in `lib/`; theme tokens live in `index.css`.
- `backend/app/resources/`: configuration catalogs, console documentation, and image assets. `docs/` contains architecture and deployment guides; `scripts/` contains setup and management utilities.

## Build, Test, and Development Commands

Run commands from the repository root unless indicated:

- `scripts/setup.sh`: provision the Conda environment, dependencies, and framework catalog for a new installation; reuse existing environments.
- `RELOAD=1 scripts/start_backend.sh`: start the API on port 8000 with hot reload.
- `scripts/start_frontend.sh`: start Vite on port 5153, proxying `/api` and `/ws` to the backend.
- `scripts/start_frontend.sh build`: type-check and build into `frontend/dist/`.
- `PYTHONPATH=$PWD python -m pytest`: run backend tests in the activated `prt-ai-dl-factory` environment.
- `python scripts/gen_framework_support.py`: regenerate the compatibility catalog after framework upgrades; do not edit it manually.

## Coding Style & Naming Conventions

Use four-space Python indentation, type hints, and `snake_case` functions/modules. Follow existing TypeScript style: two-space indentation, single quotes, no semicolons, PascalCase components, and camelCase functions/hooks. Use semantic Tailwind classes such as `bg-card` and `text-fg-muted`. No formatter or lint command is configured; match surrounding code.

Keep persistence behind `store/`. Builders must produce argv without shell execution, route secrets through `BuildResult.secrets`, and validate parameters against installed framework sources.

## Testing Guidelines

Name tests `backend/tests/test_*.py` and functions `test_*`. Add regression coverage for changed behavior; no numeric coverage threshold is configured. Configure `backend/.env` with absolute framework interpreter paths before testing. Set test environment overrides in `conftest.py` before application imports. Builder changes require `test_builders.py` and `test_config_keys.py`. Run the full pytest suite and frontend build before submission; report environment-dependent skips.

## Commit & Pull Request Guidelines

History favors `feat(scope): description`, `fix(scope): description`, and `chore(docs): description`, often with Chinese descriptions. Keep commits focused. PRs should explain behavior changes, link relevant issues, list validation results, and include screenshots for UI changes.

## Security & Configuration

Never commit `.env`, runtime data, checkpoints, or build outputs. Override bootstrap administrator credentials. Keep Uvicorn at one worker because scheduling and telemetry use process-local singletons.
