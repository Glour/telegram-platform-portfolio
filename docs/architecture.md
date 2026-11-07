# Architecture overview

This repository is a static portfolio snapshot of a larger application. It is intended for code review and excludes the concrete Telegram transport, deployment configuration and private data. The missing transport means that the application and integration tests that depend on it are not runnable as a complete system.

## Request flow

1. The React dashboard calls the versioned HTTP API through the shared client in `frontend/src/lib/api.ts`.
2. FastAPI routers compose feature endpoints. Shared dependencies decode the access token, load the active user and verify membership of the workspace selected by `X-Workspace-Id`.
3. Feature modules contain request and response schemas, persistence models and service logic. SQLAlchemy sessions provide database access; Alembic holds schema migrations.
4. Time-consuming operations are represented by background tasks and scheduled workers. Campaign runs record their state, actions and events for inspection by the dashboard.
5. Telegram interactions are modeled through gateway contracts. The transport implementation is deliberately omitted from this snapshot.

## Areas to review

- `src/app/api/deps.py`: authentication and workspace membership checks.
- `src/app/modules/campaigns/`: campaign models, scheduling and account-conflict rules.
- `src/app/modules/telegram_safety/`: operation safety states, admission checks and error categorization.
- `src/app/modules/link_tracking/`: destination validation, request classification and statistics.
- `frontend/src/components/`: dashboard controls and views; `frontend/src/lib/` contains API and presentation helpers.
- `tests/`: application tests plus isolated tests that run without the missing gateway.

## Boundaries and limitations

Workspace selection and membership validation are centralized dependencies, but the snapshot is not a security audit: individual endpoints and data-access paths still need review. The dashboard and some service modules remain large and are candidates for further decomposition. The checks listed in the README cover formatting, static analysis and selected independent tests; they do not prove end-to-end behavior or production readiness.
