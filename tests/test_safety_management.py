from datetime import timedelta

import pytest
from sqlalchemy import select

from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety import service as safety
from app.modules.telegram_safety.management import account_overview, workspace_overview
from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState

pytest_plugins = ["test_telegram_safety_transport"]


async def pending_admission(factory):
    async with factory.begin() as db:
        a = await db.get(TelegramAccount, "account")
        a.daily_limits = {"restriction_status": {"status": "unknown"}}
        s = await db.get(TelegramSafetyState, "account")
        s.manual_hold = True
        s.reason = "admission_required"
        s.restriction_status = "unknown"
        s.restriction_checked_at = None
        s.operator_approved_at = None


async def test_pause_and_owner_readmission_preserve_unknown_and_audit(sql_store):
    await pending_admission(sql_store.factory)
    await safety.approve_owner_attestation("account", "owner", "Trusted by owner")
    result = await safety.pause_account("account", "owner", "Pause")
    assert result["manual_hold"] and result["reason"] == "operator_paused"
    assert result["restriction_status"] == "unknown"
    with pytest.raises(TelegramGatewayError) as e:
        await (await sql_store.client())._check()
    assert e.value.code == "SAFETY_MANUAL_HOLD"
    await safety.approve_owner_attestation("account", "owner", "Trusted by owner again")
    await (await sql_store.client())._check()
    async with sql_store.factory() as db:
        a = await db.get(TelegramAccount, "account")
        s = await db.get(TelegramSafetyState, "account")
        assert a.restriction_status == "unknown" and s.restriction_checked_at is None
        methods = list((await db.scalars(select(TelegramOperation.method))).all())
        assert methods.count("operator.pause") == 1
        assert methods.count("operator.owner_attest") == 2


async def test_pause_is_idempotent_and_preserves_diagnostic_hold(sql_store):
    async with sql_store.factory.begin() as db:
        s = await db.get(TelegramSafetyState, "account")
        s.manual_hold = True
        s.reason = "unknown_rpc"
        s.operation_id = "original"
        s.evidence = "original evidence"
    await safety.pause_account("account", "owner", "Pause")
    async with sql_store.factory() as db:
        s = await db.get(TelegramSafetyState, "account")
        assert (s.reason, s.operation_id, s.evidence) == ("unknown_rpc", "original", "original evidence")
        assert not list(
            (await db.scalars(select(TelegramOperation).where(TelegramOperation.method == "operator.pause"))).all()
        )
    with pytest.raises(TelegramGatewayError) as e:
        await safety.approve_owner_attestation("account", "owner", "Trusted")
    assert e.value.code == "SAFETY_REVIEW_REQUIRED"


async def test_overview_is_scoped_and_does_not_create_state(sql_store):
    await pending_admission(sql_store.factory)
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramAccount(
                id="no-state", workspace_id="workspace", import_type="session", status="new", daily_limits={}
            )
        )
    async with sql_store.factory() as db:
        result = await workspace_overview(db, "workspace", "owner")
        assert result["is_owner"] and result["can_manage"]
        assert {"account", "no-state"} <= {r["id"] for r in result["accounts"]}
        assert await db.get(TelegramSafetyState, "no-state") is None
        row = next(r for r in result["accounts"] if r["id"] == "no-state")
        assert row["can_attest"] and not row["ready"] and row["permission"] == "required"
        other = await workspace_overview(db, "different-workspace", "owner")
        assert other["accounts"] == []


@pytest.mark.parametrize("block", ["invalid", "quarantined", "limited", "wait", "pending", "operational"])
async def test_table_disables_only_real_admission_blocks(sql_store, block):
    await pending_admission(sql_store.factory)
    async with sql_store.factory() as db:
        a = await db.get(TelegramAccount, "account")
        s = await db.get(TelegramSafetyState, "account")
        if block in {"invalid", "quarantined"}:
            a.status = block
        elif block == "limited":
            s.restriction_status = "limited"
        elif block == "wait":
            from datetime import UTC, datetime

            s.wait_until = datetime.now(UTC) + timedelta(hours=1)
        elif block == "operational":
            s.reason = "unknown_rpc"
        row = account_overview(a, s, None, pending=block == "pending", is_owner=True, can_manage=True)
        assert not row["can_attest"] and row["attest_problem"] and not row["ready"]


async def test_owner_permission_is_distinct_from_proxy_and_telegram_check(sql_store):
    await pending_admission(sql_store.factory)
    await safety.approve_owner_attestation("account", "owner", "Trusted")
    async with sql_store.factory() as db:
        a = await db.get(TelegramAccount, "account")
        s = await db.get(TelegramSafetyState, "account")
        row = account_overview(a, s, None, is_owner=True, can_manage=True)
        assert row["permission"] == "allowed" and row["admission_basis"] == "owner_attestation"
        assert row["restriction_status"] == "unknown"
        assert not row["ready"] and row["problem"] == "Нужен рабочий прокси"
        assert row["can_pause"]
        assert not account_overview(a, s, None, is_owner=False)["can_attest"]


async def test_non_owner_cannot_attest(sql_store):
    await pending_admission(sql_store.factory)
    with pytest.raises(TelegramGatewayError) as e:
        await safety.approve_owner_attestation("account", "stranger", "Trusted")
    assert e.value.code == "SAFETY_OWNER_REQUIRED"


async def test_historical_account_is_visible_and_owner_can_attest(sql_store):
    await pending_admission(sql_store.factory)
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.reason = "historical_account_requires_operator_admission"
    async with sql_store.factory() as db:
        result = await workspace_overview(db, "workspace", "owner")
        row = next(r for r in result["accounts"] if r["id"] == "account")
        assert row["can_attest"] and row["permission"] == "required"
    result = await safety.approve_owner_attestation("account", "owner", "Explicit historical account approval")
    assert not result["manual_hold"]
    assert result["restriction_status"] == "unknown"
    assert result["restriction_checked_at"] is None


@pytest.mark.parametrize("block", ["limited", "pending", "wait", "non_owner"])
async def test_historical_reason_never_bypasses_safety_blocks(sql_store, block):
    await pending_admission(sql_store.factory)
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.reason = "historical_account_requires_operator_admission"
        if block == "limited":
            state.restriction_status = "limited"
        elif block == "wait":
            state.wait_until = safety.utcnow() + timedelta(hours=1)
        elif block == "pending":
            db.add(
                TelegramOperation(
                    account_id="account",
                    method="SendMessageRequest",
                    outcome="unknown_delivery",
                    outbound=True,
                )
            )
    with pytest.raises(TelegramGatewayError):
        await safety.approve_owner_attestation(
            "account",
            "stranger" if block == "non_owner" else "owner",
            "Explicit approval",
        )


async def test_owner_admission_initializes_runtime_budget_and_keeps_explicit_limit(sql_store):
    await pending_admission(sql_store.factory)
    async with sql_store.factory.begin() as db:
        a = await db.get(TelegramAccount, "account")
        a.daily_limits = {
            **a.daily_limits,
            "owner_admission": {
                "comment_limit_override": {
                    "basis": "owner_request",
                    "actor_id": "owner",
                    "account_id": "account",
                    "workspace_id": "workspace",
                    "limit": 25,
                }
            },
        }
    await safety.approve_owner_attestation("account", "owner", "Trusted")
    async with sql_store.factory() as db:
        a = await db.get(TelegramAccount, "account")
        s = await db.get(TelegramSafetyState, "account")
        assert a.daily_limits["owner_admission"]["comment_attempt_limit"] == 25
        assert s.outbound_attempt_limit >= 73 and s.rpc_attempt_limit >= 10000
        assert s.restriction_status == "unknown"


async def test_account_projection_exposes_operational_hold_without_faking_a_ban(sql_store):
    from app.modules.telegram_safety.views import apply_safety_projection

    async with sql_store.factory.begin() as db:
        a = await db.get(TelegramAccount, "account")
        s = await db.get(TelegramSafetyState, "account")
        s.manual_hold = True
        s.reason = "transport_error"
        result = await apply_safety_projection(db, a, {"status": a.status})
        assert result["telegram_manual_hold"] is True and result["telegram_safety_reason"] == "transport_error"
        assert result["status"] == "active" and a.status == "active"
        s.manual_hold = False
        result = await apply_safety_projection(db, a, {"status": a.status})
        assert result["telegram_manual_hold"] is False
