import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.api.deps import get_current_user
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_session
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety import api
from app.modules.telegram_safety.models import TelegramLoginSafety, TelegramOperation, TelegramSafetyState
from app.modules.telegram_safety.schemas import SafetyAdmissionRequest
from app.modules.workspaces.models import Workspace, WorkspaceMember


@pytest_asyncio.fixture
async def safety_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                Workspace(id="workspace", owner_user_id="operator", name="test", slug="test"),
                Workspace(id="other", owner_user_id="other-user", name="other", slug="other"),
                WorkspaceMember(workspace_id="workspace", user_id="operator", role="owner"),
                TelegramAccount(id="account", workspace_id="workspace", import_type="session"),
                TelegramAccount(id="foreign", workspace_id="other", import_type="session"),
                TelegramOperation(
                    id="operation",
                    account_id="account",
                    method="messages.SendMessage",
                    outcome="unknown_delivery",
                    outbound=True,
                ),
                TelegramOperation(
                    id="foreign-operation",
                    account_id="foreign",
                    method="messages.SendMessage",
                    outcome="pending",
                    outbound=True,
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def safety_client(safety_db, monkeypatch):
    app = FastAPI()
    app.include_router(api.router, prefix="/telegram-safety")
    app.dependency_overrides[get_session] = lambda: safety_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="operator")
    state = {"account_id": "account", "manual_hold": True, "restriction_status": "unknown"}
    service = SimpleNamespace(
        get_safety_state=AsyncMock(return_value=state),
        resume_account=AsyncMock(return_value=state),
        pause_account=AsyncMock(return_value=state),
        approve_account=AsyncMock(return_value=state),
        resolve_operation=AsyncMock(return_value=state),
    )
    monkeypatch.setattr(api, "import_module", lambda name: service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, service


@pytest.mark.parametrize("method,path", [("GET", ""), ("POST", "/resume"), ("POST", "/admit"), ("POST", "/pause")])
async def test_cross_workspace_denied_before_service(safety_client, method, path):
    client, service = safety_client
    body = {"restriction_status": "clear", "checked_at": datetime.now(UTC).isoformat(), "evidence": "operator check"}
    response = await client.request(
        method,
        f"/telegram-safety/foreign{path}",
        headers={"X-Workspace-Id": "workspace"},
        json=body if path == "/admit" else None,
    )
    assert response.status_code == 404
    for function in vars(service).values():
        function.assert_not_awaited()


@pytest.mark.parametrize("workspace,status", [(None, 400), ("other", 403)])
async def test_workspace_header_and_membership_required(safety_client, workspace, status):
    client, service = safety_client
    response = await client.get("/telegram-safety/account", headers={"X-Workspace-Id": workspace} if workspace else {})
    assert response.status_code == status
    service.get_safety_state.assert_not_awaited()


@pytest.mark.parametrize(
    "path,body",
    [
        ("resume", None),
        ("pause", None),
        ("admit", {"restriction_status": "clear", "checked_at": "2026-01-01T00:00:00Z", "evidence": "operator check"}),
        ("operations/operation/resolve", {"outcome": "not_delivered", "evidence": "destination checked"}),
    ],
)
async def test_viewer_cannot_mutate_before_service(safety_client, safety_db, path, body):
    client, service = safety_client
    workspace = await safety_db.get(Workspace, "workspace")
    workspace.owner_user_id = "other-user"
    member = (
        await safety_db.execute(
            sa.select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == "workspace",
                WorkspaceMember.user_id == "operator",
            )
        )
    ).scalar_one()
    member.role = "viewer"
    await safety_db.commit()
    response = await client.post(f"/telegram-safety/account/{path}", headers={"X-Workspace-Id": "workspace"}, json=body)
    assert response.status_code == 403
    for function in vars(service).values():
        function.assert_not_awaited()
    assert (await client.get("/telegram-safety/account", headers={"X-Workspace-Id": "workspace"})).status_code == 200


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_operator_membership_allows_mutation(safety_client, safety_db, role):
    client, service = safety_client
    workspace = await safety_db.get(Workspace, "workspace")
    workspace.owner_user_id = "other-user"
    member = (
        await safety_db.execute(
            sa.select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == "workspace",
                WorkspaceMember.user_id == "operator",
            )
        )
    ).scalar_one()
    member.role = role
    await safety_db.commit()
    response = await client.post("/telegram-safety/account/resume", headers={"X-Workspace-Id": "workspace"})
    assert response.status_code == 200
    service.resume_account.assert_awaited_once_with("account", "operator")


async def test_read_and_resume_delegate_with_authenticated_actor(safety_client):
    client, service = safety_client
    headers = {"X-Workspace-Id": "workspace"}
    assert (await client.get("/telegram-safety/account", headers=headers)).json()["manual_hold"] is True
    service.get_safety_state.assert_awaited_once_with("account")
    service.resume_account.return_value = {"account_id": "account", "manual_hold": False, "restriction_status": "clear"}
    response = await client.post("/telegram-safety/account/resume", headers=headers)
    assert response.status_code == 200
    assert response.json()["manual_hold"] is False
    service.resume_account.assert_awaited_once_with("account", "operator")


@pytest.mark.parametrize("enabled", [False, True])
async def test_execution_flag_is_separate_from_manual_hold(safety_client, enabled):
    client, service = safety_client
    service.get_safety_state.return_value = {
        "account_id": "account",
        "manual_hold": False,
        "restriction_status": "clear",
        "execution_enabled": enabled,
    }
    response = await client.get("/telegram-safety/account", headers={"X-Workspace-Id": "workspace"})
    assert response.status_code == 200
    assert response.json()["execution_enabled"] is enabled
    assert response.json()["manual_hold"] is False


async def test_admission_passes_evidence_and_no_actor_spoofing(safety_client):
    client, service = safety_client
    checked_at = datetime.now(UTC) - timedelta(minutes=1)
    payload = {"restriction_status": "clear", "checked_at": checked_at.isoformat(), "evidence": "  manual check  "}
    headers = {"X-Workspace-Id": "workspace"}
    assert (await client.post("/telegram-safety/account/admit", headers=headers, json=payload)).status_code == 200
    service.approve_account.assert_awaited_once_with("account", "operator", checked_at, "manual check")
    service.approve_account.reset_mock()
    payload["actor_id"] = "spoofed"
    assert (await client.post("/telegram-safety/account/admit", headers=headers, json=payload)).status_code == 422
    service.approve_account.assert_not_awaited()


@pytest.mark.parametrize(
    "account_id,operation_id",
    [
        ("foreign", "foreign-operation"),
        ("account", "foreign-operation"),
        ("account", "missing"),
    ],
)
async def test_resolution_cross_workspace_and_operation_ownership(safety_client, account_id, operation_id):
    client, service = safety_client
    response = await client.post(
        f"/telegram-safety/{account_id}/operations/{operation_id}/resolve",
        headers={"X-Workspace-Id": "workspace"},
        json={"outcome": "not_delivered", "evidence": "operator checked destination"},
    )
    assert response.status_code == 404
    for function in vars(service).values():
        function.assert_not_awaited()


@pytest.mark.parametrize("outcome,message_id", [("accepted", "12345"), ("not_delivered", None)])
async def test_resolution_delegates_with_authenticated_actor(safety_client, outcome, message_id):
    client, service = safety_client
    payload = {"outcome": outcome, "evidence": "  destination checked  ", "message_id": message_id}
    response = await client.post(
        "/telegram-safety/account/operations/operation/resolve", headers={"X-Workspace-Id": "workspace"}, json=payload
    )
    assert response.status_code == 200
    service.resolve_operation.assert_awaited_once_with(
        "account",
        "operation",
        "operator",
        outcome,
        "destination checked",
        message_id,
    )
    service.resume_account.assert_not_awaited()
    service.approve_account.assert_not_awaited()


@pytest.mark.parametrize(
    "override",
    [
        {"outcome": "accepted"},
        {"outcome": "accepted", "message_id": " "},
        {"evidence": " "},
        {"outcome": "failed"},
        {"actor_id": "spoofed"},
        {"message_id": "x" * 65},
    ],
)
async def test_invalid_resolution_rejected_before_service(safety_client, override):
    client, service = safety_client
    response = await client.post(
        "/telegram-safety/account/operations/operation/resolve",
        headers={"X-Workspace-Id": "workspace"},
        json={"outcome": "not_delivered", "evidence": "destination checked", **override},
    )
    assert response.status_code == 422
    service.resolve_operation.assert_not_awaited()


async def test_resolution_preserves_service_conflict(safety_client):
    client, service = safety_client
    service.resolve_operation.side_effect = TelegramGatewayError("SAFETY_ALREADY_RESOLVED", "Already resolved")
    response = await client.post(
        "/telegram-safety/account/operations/operation/resolve",
        headers={"X-Workspace-Id": "workspace"},
        json={"outcome": "not_delivered", "evidence": "destination checked"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SAFETY_ALREADY_RESOLVED"


@pytest.mark.parametrize("error_type", [AppError, TelegramGatewayError])
@pytest.mark.parametrize("code", ["restriction_limited", "restriction_unknown", "restriction_stale", "wait_active"])
async def test_resume_preserves_service_rejection(safety_client, code, error_type):
    client, service = safety_client
    service.resume_account.side_effect = error_type(code, "Blocked by safety policy")
    response = await client.post("/telegram-safety/account/resume", headers={"X-Workspace-Id": "workspace"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    service.approve_account.assert_not_awaited()


@pytest.mark.parametrize(
    "override",
    [
        {"restriction_status": "limited"},
        {"evidence": "  "},
        {"checked_at": "2026-01-01T00:00:00"},
        {"checked_at": "2999-01-01T00:00:00Z"},
    ],
)
def test_invalid_admission_evidence_rejected(override):
    with pytest.raises(ValidationError):
        SafetyAdmissionRequest.model_validate(
            {
                "restriction_status": "clear",
                "evidence": "manual check",
                "checked_at": datetime.now(UTC),
                **override,
            }
        )


async def test_model_defaults_bigint_and_unique_fingerprint(safety_db):
    state = TelegramSafetyState(account_id="account", fingerprint="a" * 64)
    login = TelegramLoginSafety(fingerprint="b" * 64)
    operation = TelegramOperation(
        account_id="account",
        method="messages.SendMessage",
        outbound=True,
        random_id=2**62,
        idempotency_key="one-attempt",
    )
    safety_db.add_all([state, login, operation])
    await safety_db.commit()
    assert state.manual_hold is True
    assert state.restriction_status == "unknown"
    assert state.outbound_attempt_limit == 1
    assert state.rpc_attempt_limit == 20
    assert state.first_seen_at is not None
    assert login.manual_hold is False
    assert operation.random_id == 2**62
    safety_db.add(TelegramSafetyState(account_id="foreign", fingerprint="a" * 64))
    with pytest.raises(IntegrityError):
        await safety_db.commit()
    await safety_db.rollback()
    safety_db.add(TelegramOperation(account_id="foreign", method="messages.SendMessage", idempotency_key="one-attempt"))
    with pytest.raises(IntegrityError):
        await safety_db.commit()
    await safety_db.rollback()


async def test_first_seen_is_immutable_after_creation(safety_db):
    state = TelegramSafetyState(account_id="account")
    safety_db.add(state)
    await safety_db.commit()
    original = state.first_seen_at
    state.reason = "operator_resume"
    await safety_db.commit()
    assert state.first_seen_at == original
    state.first_seen_at = original + timedelta(days=1)
    with pytest.raises(ValueError, match="immutable"):
        await safety_db.commit()
    await safety_db.rollback()


def test_migration_backfill_and_downgrade():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261003_0028_telegram_safety.py"
    spec = importlib.util.spec_from_file_location("safety_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        metadata = sa.MetaData()
        accounts = sa.Table(
            "telegram_accounts",
            metadata,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("status", sa.String()),
            sa.Column("daily_limits", sa.JSON()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        metadata.create_all(connection)
        historical = datetime(2024, 1, 1, tzinfo=UTC)
        for account_id, status, limits in [
            ("clear", "active", {"restriction_status": {"status": "clear"}}),
            ("limited", "active", {"restriction_status": {"status": "limited"}}),
            ("legacy", "spam_blocked", {}),
            ("unknown", "active", {}),
        ]:
            connection.execute(
                accounts.insert().values(id=account_id, status=status, daily_limits=limits, created_at=historical)
            )
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            states = sa.Table("telegram_safety_states", sa.MetaData(), autoload_with=connection)
            rows = {row["account_id"]: row for row in connection.execute(sa.select(states)).mappings()}
            assert rows["clear"]["restriction_status"] == "unknown"
            assert rows["limited"]["restriction_status"] == rows["legacy"]["restriction_status"] == "limited"
            assert all(row["manual_hold"] and row["operator_approved_at"] is None for row in rows.values())
            assert all(row["reason"] == "historical_account_requires_operator_admission" for row in rows.values())
            assert rows["clear"]["first_seen_at"].year == 2024
            assert all(row["rpc_attempt_limit"] == 20 and row["outbound_attempt_limit"] == 1 for row in rows.values())
            assert sa.inspect(connection).get_foreign_keys("telegram_safety_states")[0]["options"] == {
                "ondelete": "CASCADE",
            }
            assert sa.inspect(connection).get_foreign_keys("telegram_operations")[0]["options"] == {}
            module.downgrade()
            assert sa.inspect(connection).get_table_names() == ["telegram_accounts"]
    engine.dispose()


async def test_pause_audits_authenticated_operator(safety_client):
    client, service = safety_client
    response = await client.post("/telegram-safety/account/pause", headers={"X-Workspace-Id": "workspace"})
    assert response.status_code == 200
    service.pause_account.assert_awaited_once_with("account", "operator", "Operator pause from Security account table")
