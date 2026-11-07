# Telegram campaign platform — architecture snapshot

A portfolio snapshot of a Telegram campaign management platform. The repository retains the application structure, domain modules, dashboard, database migrations and a substantial test suite. It has been anonymized and intentionally omits the production integration used to connect Telegram accounts.

## Architecture

- `src/app/api` — API composition and common request dependencies.
- `src/app/modules` — domain modules for accounts, campaigns, sources, billing, analytics, safety controls and content generation. Each module groups its API, schemas, models and services where appropriate.
- `src/app/workers` — background jobs and event processing.
- `frontend/src` — React and TypeScript dashboard, components and API client.
- `alembic` — database migration history.
- `tests` — unit and integration tests retained for review.

The `telegram_gateway` package contains interfaces and contracts; its transport implementation and contact integration are intentionally excluded. Deployment configuration, private operational documents, runtime assets and credentials are also excluded. Imports and tests that require the missing transport cannot run in this snapshot, and it is not a deployable application.

## Static checks and isolated tests

Python 3.12 is required. To inspect the code locally:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check src tests alembic
pytest -q tests/test_market_authorization_unit.py tests/test_contact_broadcast_policy_unit.py tests/test_link_validation_unit.py \
  tests/test_spambot_appeal_automatic.py tests/test_spambot_appeal_rules.py \
  tests/test_billing_config.py tests/test_trial_access.py \
  tests/test_account_market_payments.py tests/test_operations_alerts.py
```

The selected tests exercise validation, request classification, billing configuration, trial access, account-market behavior and safety rules without the removed gateway. The rest of the test suite is included as architectural context, not as a passing full-system gate.

For the frontend, Node.js and npm are required:

```sh
cd frontend
npm ci
npm run typecheck
npm test
npm run lint
```

The frontend checks validate source code only; the snapshot omits assets and build configuration needed to run the full interface. No frontend build or end-to-end integration is claimed.
