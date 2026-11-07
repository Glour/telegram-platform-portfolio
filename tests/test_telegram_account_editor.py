from __future__ import annotations

import hashlib
import sys
import tarfile
import zipfile
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

import app.db.import_models  # noqa: F401
from app.core.crypto import encrypt_text
from app.db.base import Base
from app.modules.proxies.models import Proxy
from app.modules.telegram_accounts import api as accounts_api
from app.modules.telegram_accounts import service as accounts_service
from app.modules.telegram_accounts.models import (
    AccountHealthEvent,
    AccountQualityCheck,
    RiskDecision,
    TelegramAccount,
    TelegramSession,
)
from app.modules.telegram_accounts.schemas import (
    AccountBulkAiProfilesRequest,
    AccountBulkCloneProfileRequest,
    AccountBulkDetachOverloadedProxiesRequest,
    AccountBulkJoinChatsRequest,
    AccountBulkStatusRequest,
    AccountImportBatchPatch,
    AccountQualityCheckCreate,
    TelegramAccountCloneProfileRequest,
    TelegramAccountImport,
    TelegramAccountProfilePatch,
    TelegramAccountTwoFaUpdate,
    TelegramSessionFileImportMeta,
)
from app.modules.telegram_accounts.service import (
    delete_account_profile_photo,
    generated_profile_for_account,
    get_account_profile,
    list_account_profile_photos,
    request_account_login_code,
)
from app.modules.telegram_gateway.contracts import TelegramAccountInfo, TelegramProfileInfo
from app.modules.workspaces.models import Workspace


@pytest.mark.asyncio
async def test_phone_login_connect_does_not_retry_transient_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    sleeps: list[int] = []

    class FlakyClient:
        async def connect(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TimeoutError("temporary timeout")

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(accounts_service.asyncio, "sleep", fake_sleep)

    with pytest.raises(TimeoutError):
        await accounts_service._connect_phone_login_client(FlakyClient())

    assert attempts == 1
    assert sleeps == []


def test_phone_login_client_uses_long_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_login_client(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    safety = ModuleType("app.modules.telegram_safety.service")
    safety.create_login_client = fake_create_login_client
    monkeypatch.setitem(sys.modules, safety.__name__, safety)

    accounts_service._phone_login_client(
        SimpleNamespace(
            telegram_api_id=123,
            telegram_api_hash="hash",
            telegram_request_timeout_seconds=25,
        ),
        "",
        ("socks5", "127.0.0.1", 1080),
        phone="+15550101000",
    )

    assert captured["timeout"] == accounts_service.PHONE_LOGIN_REQUEST_TIMEOUT_SECONDS
    assert captured["request_retries"] == captured["connection_retries"] == 0
    assert captured["auto_reconnect"] is False


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture(autouse=True)
def scaffold_telegram_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: False)


async def _workspace(session: AsyncSession, slug: str = "test") -> Workspace:
    workspace = Workspace(owner_user_id="owner", name="Test", slug=slug)
    session.add(workspace)
    await session.flush()
    return workspace


async def _account(session: AsyncSession, workspace: Workspace, **kwargs: object) -> TelegramAccount:
    account = TelegramAccount(
        workspace_id=workspace.id,
        username=kwargs.pop("username", "old_user"),
        display_name=kwargs.pop("display_name", "Old Name"),
        status=kwargs.pop("status", "new"),
        import_type="session",
        health_score=kwargs.pop("health_score", 45),
        daily_limits=kwargs.pop("daily_limits", {}),
        **kwargs,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _proxy(session: AsyncSession, workspace: Workspace, host: str = "10.0.0.1") -> Proxy:
    proxy = Proxy(workspace_id=workspace.id, type="socks5", host=host, port=8000, status="active")
    session.add(proxy)
    await session.commit()
    await session.refresh(proxy)
    return proxy


@pytest.mark.asyncio
async def test_profile_photos_scaffold_lists_and_deletes_metadata_photo(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    photo_data_url = "data:image/jpeg;base64,ZmFrZS1waG90bw=="
    account = await _account(
        session,
        workspace,
        daily_limits={"profile": {"phone": "15550000000", "photo_data_url": photo_data_url}},
    )

    photos = await list_account_profile_photos(session, account)

    assert len(photos.photos) == 1
    assert photos.photos[0].photo_id == "local-current"
    assert photos.photos[0].photo_data_url == photo_data_url
    assert photos.photos[0].is_current is True

    next_photos = await delete_account_profile_photo(session, account, photo_id="local-current")

    assert next_photos.photos == []
    assert account.profile_photo_data_url is None


@pytest.mark.asyncio
async def test_request_login_code_scaffold_uses_profile_phone(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace, daily_limits={"profile": {"phone": "15550000000"}})

    result = await request_account_login_code(session, account)

    assert result.status == "queued"
    assert result.phone == "15550000000"
    assert "не отправлялся" in result.message


@pytest.mark.asyncio
async def test_import_session_keeps_trusted_personal_warmup_preset(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)

    account = await accounts_service.import_session_account(
        session,
        workspace.id,
        TelegramAccountImport(
            session_blob="trusted-session",
            display_name="Personal account",
            proxy_id=proxy.id,
            proxy_required=True,
            warmup_status="completed",
            warmup_score=100,
        ),
    )

    assert account.proxy_required is True
    assert account.proxy_id == proxy.id
    assert account.warmup_status == "completed"
    assert account.warmup_score == 100


def _upload_file(content_type: str, body: bytes) -> UploadFile:
    return UploadFile(
        filename="avatar.bin",
        file=BytesIO(body),
        headers=Headers({"content-type": content_type}),
    )


def _named_upload_file(filename: str, content_type: str, body: bytes) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=BytesIO(body),
        headers=Headers({"content-type": content_type}),
    )


def test_find_tdata_dir_accepts_real_telegram_key_datas(tmp_path: Path) -> None:
    tdata = tmp_path / "outer" / "tdata"
    tdata.mkdir(parents=True)
    (tdata / "key_datas").write_bytes(b"tdf")

    assert accounts_service._find_tdata_dir(tmp_path) == tdata


def test_find_tdata_dir_accepts_root_key_data_variant(tmp_path: Path) -> None:
    (tmp_path / "key_data1").write_bytes(b"tdf")

    assert accounts_service._find_tdata_dir(tmp_path) == tmp_path


def test_session_archive_session_paths_ignore_macos_sidecars(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    macos = tmp_path / "__MACOSX" / "sessions"
    sessions.mkdir(parents=True)
    macos.mkdir(parents=True)

    for index in range(9):
        (sessions / f"+1000000000{index}.session").write_bytes(b"sqlite")
        (sessions / f"+1000000000{index}.json").write_text("{}", encoding="utf-8")
        (macos / f"._+1000000000{index}.session").write_bytes(b"sidecar")
        (macos / f"._+1000000000{index}.json").write_text("{}", encoding="utf-8")

    session_paths = accounts_service._session_archive_session_paths(tmp_path)
    metadata = accounts_service._session_archive_metadata(tmp_path)

    assert len(session_paths) == 9
    assert all(not path.name.startswith("._") for path in session_paths)
    assert len(metadata) == 9


def test_session_archive_extracts_tar_archives(tmp_path: Path) -> None:
    archive_path = tmp_path / "sessions.tar.gz"
    extract_dir = tmp_path / "extract"
    source_dir = tmp_path / "source" / "sessions"
    extract_dir.mkdir()
    source_dir.mkdir(parents=True)
    (source_dir / "+15550101000.session").write_bytes(b"sqlite")

    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source_dir, arcname="sessions")

    accounts_service._extract_session_archive(archive_path, extract_dir)

    session_paths = accounts_service._session_archive_session_paths(extract_dir)
    assert [path.name for path in session_paths] == ["+15550101000.session"]


@pytest.mark.asyncio
async def test_account_archive_imports_tdata_when_no_session_files(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    proxies = [await _proxy(session, workspace, host=f"10.0.0.{index}") for index in range(1, 4)]
    archive_body = BytesIO()
    with zipfile.ZipFile(archive_body, "w") as archive:
        for index in range(1, 4):
            archive.writestr(f"account-{index}/tdata/key_datas", b"tdf")

    imported_dirs: list[str] = []

    async def fake_import_tdata_dir_account(
        db_session: AsyncSession,
        workspace_id: str,
        tdata_dir: Path,
        meta: accounts_service.TelegramTdataImportMeta,
    ) -> TelegramAccount:
        account = TelegramAccount(
            workspace_id=workspace_id,
            display_name="Imported tdata",
            import_type="tdata",
            proxy_id=meta.proxy_id,
            proxy_required=meta.proxy_required,
            status="active",
            health_score=90,
        )
        db_session.add(account)
        await db_session.commit()
        await db_session.refresh(account)
        assert tdata_dir.name == "tdata"
        imported_dirs.append(str(tdata_dir.parent.name))
        return account

    monkeypatch.setattr(accounts_service, "_import_tdata_dir_account", fake_import_tdata_dir_account)

    response = await accounts_service.import_session_archive_accounts(
        session,
        workspace.id,
        _named_upload_file("tdata.zip", "application/zip", archive_body.getvalue()),
        TelegramSessionFileImportMeta(proxy_required=True),
        [proxy.id for proxy in proxies],
    )

    assert response["imported"] == 3
    assert response["failed"] == 0
    assert [item["status"] for item in response["items"]] == ["imported", "imported", "imported"]
    assert {item["account"].import_type for item in response["items"]} == {"tdata"}
    assert imported_dirs == ["account-1", "account-2", "account-3"]


@pytest.mark.asyncio
async def test_account_archive_import_uses_proxy_capacity_not_proxy_count(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    proxy.max_accounts = 3
    await session.commit()
    archive_body = BytesIO()
    with zipfile.ZipFile(archive_body, "w") as archive:
        for index in range(1, 4):
            archive.writestr(f"account-{index}/tdata/key_datas", b"tdf")

    async def fake_import_tdata_dir_account(
        db_session: AsyncSession,
        workspace_id: str,
        tdata_dir: Path,
        meta: accounts_service.TelegramTdataImportMeta,
    ) -> TelegramAccount:
        account = TelegramAccount(
            workspace_id=workspace_id,
            display_name=tdata_dir.parent.name,
            import_type="tdata",
            proxy_id=meta.proxy_id,
            proxy_required=meta.proxy_required,
            status="active",
            health_score=90,
        )
        db_session.add(account)
        await db_session.commit()
        await db_session.refresh(account)
        return account

    monkeypatch.setattr(accounts_service, "_import_tdata_dir_account", fake_import_tdata_dir_account)

    response = await accounts_service.import_session_archive_accounts(
        session,
        workspace.id,
        _named_upload_file("tdata.zip", "application/zip", archive_body.getvalue()),
        TelegramSessionFileImportMeta(proxy_required=True),
        [proxy.id],
    )

    assert response["imported"] == 3
    assert response["failed"] == 0
    assert {item["account"].proxy_id for item in response["items"]} == {proxy.id}


@pytest.mark.asyncio
async def test_session_file_import_skips_existing_phone_account(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    phone = "+15550101000"
    existing = await _account(
        session,
        workspace,
        username=None,
        display_name="Екатерина Лебедева",
        phone_hash=hashlib.sha256(phone.encode("utf-8")).hexdigest(),
    )

    imported = await accounts_service._create_session_file_account(
        session,
        workspace.id,
        session_blob="new-session-blob",
        meta=TelegramSessionFileImportMeta(phone=phone, display_name="Екатерина Лебедева"),
        event_type="session_archive_imported",
        event_message="import",
    )
    accounts = list((await session.execute(select(TelegramAccount))).scalars().all())

    assert imported.id == existing.id
    assert len(accounts) == 1


@pytest.mark.asyncio
async def test_session_file_import_skips_existing_session_fingerprint(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    meta = TelegramSessionFileImportMeta(display_name="Archive account")

    first = await accounts_service._create_session_file_account(
        session,
        workspace.id,
        session_blob="same-session-blob",
        meta=meta,
        event_type="session_archive_imported",
        event_message="import",
    )
    duplicate = await accounts_service._create_session_file_account(
        session,
        workspace.id,
        session_blob="same-session-blob",
        meta=meta,
        event_type="session_archive_imported",
        event_message="import",
    )
    accounts = list((await session.execute(select(TelegramAccount))).scalars().all())

    assert duplicate.id == first.id
    assert len(accounts) == 1


@pytest.mark.asyncio
async def test_session_file_import_ignores_deleted_session_fingerprint(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    meta = TelegramSessionFileImportMeta(display_name="Reimported account")

    deleted = await accounts_service._create_session_file_account(
        session,
        workspace.id,
        session_blob="reimport-session-blob",
        meta=TelegramSessionFileImportMeta(display_name="Deleted account"),
        event_type="session_archive_imported",
        event_message="import",
    )
    deleted.status = "deleted"
    await session.commit()

    reimported = await accounts_service._create_session_file_account(
        session,
        workspace.id,
        session_blob="reimport-session-blob",
        meta=meta,
        event_type="session_archive_imported",
        event_message="import",
    )
    accounts = list((await session.execute(select(TelegramAccount))).scalars().all())

    assert reimported.id != deleted.id
    assert reimported.status == "new"
    assert len(accounts) == 2


@pytest.mark.asyncio
async def test_session_file_import_skips_legacy_existing_session_blob(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    existing = await _account(session, workspace, display_name="Legacy import", daily_limits={})
    session.add(
        TelegramSession(account_id=existing.id, encrypted_session_blob=encrypt_text("legacy-session-blob") or "")
    )
    await session.commit()

    duplicate = await accounts_service._create_session_file_account(
        session,
        workspace.id,
        session_blob="legacy-session-blob",
        meta=TelegramSessionFileImportMeta(display_name="Legacy import"),
        event_type="session_archive_imported",
        event_message="import",
    )
    accounts = list((await session.execute(select(TelegramAccount))).scalars().all())

    assert duplicate.id == existing.id
    assert len(accounts) == 1


@pytest.mark.asyncio
async def test_session_file_import_ignores_deleted_legacy_session_blob(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    deleted = await _account(session, workspace, display_name="Deleted legacy", daily_limits={}, status="deleted")
    session.add(
        TelegramSession(account_id=deleted.id, encrypted_session_blob=encrypt_text("deleted-legacy-blob") or "")
    )
    await session.commit()

    reimported = await accounts_service._create_session_file_account(
        session,
        workspace.id,
        session_blob="deleted-legacy-blob",
        meta=TelegramSessionFileImportMeta(display_name="Reimported legacy"),
        event_type="session_archive_imported",
        event_message="import",
    )
    accounts = list((await session.execute(select(TelegramAccount))).scalars().all())

    assert reimported.id != deleted.id
    assert reimported.status == "new"
    assert len(accounts) == 2


@pytest.mark.asyncio
async def test_bulk_archive_accounts_updates_selected_accounts(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    first = await _account(session, workspace, display_name="First", status="active")
    second = await _account(session, workspace, display_name="Second", status="quarantined")

    result = await accounts_api.bulk_archive_accounts(
        AccountBulkStatusRequest(account_ids=[first.id, second.id]),
        session,
        workspace.id,
    )
    accounts = list((await session.execute(select(TelegramAccount))).scalars().all())
    health_events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert result.status == "completed"
    assert result.account_count == 2
    assert {account.id: account.status for account in accounts} == {
        first.id: "archived",
        second.id: "archived",
    }
    assert {event.event_type for event in health_events} == {"bulk_accounts_archived"}


@pytest.mark.asyncio
async def test_bulk_delete_accounts_updates_selected_accounts(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    first = await _account(session, workspace, display_name="First", status="active")
    second = await _account(session, workspace, display_name="Second", status="new")

    result = await accounts_api.bulk_delete_accounts(
        AccountBulkStatusRequest(account_ids=[first.id, second.id]),
        session,
        workspace.id,
    )
    accounts = list((await session.execute(select(TelegramAccount))).scalars().all())

    assert result.status == "completed"
    assert result.account_count == 2
    assert {account.id: account.status for account in accounts} == {
        first.id: "deleted",
        second.id: "deleted",
    }


@pytest.mark.asyncio
async def test_bulk_detach_overloaded_proxies_keeps_capacity_and_detaches_excess(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    first = await _account(session, workspace, display_name="First", status="active", proxy_id=proxy.id)
    second = await _account(session, workspace, display_name="Second", status="active", proxy_id=proxy.id)
    third = await _account(session, workspace, display_name="Third", status="warming", proxy_id=proxy.id)

    result = await accounts_api.bulk_detach_overloaded_proxies(
        AccountBulkDetachOverloadedProxiesRequest(proxy_ids=[proxy.id]),
        session,
        workspace.id,
    )
    accounts = {account.id: account for account in (await session.execute(select(TelegramAccount))).scalars().all()}
    health_events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert result.status == "completed"
    assert result.account_count == 2
    assert result.target_count == 1
    assert accounts[first.id].proxy_id == proxy.id
    assert accounts[second.id].proxy_id is None
    assert accounts[third.id].proxy_id is None
    assert accounts[second.id].proxy_required is True
    assert {event.event_type for event in health_events} == {"bulk_proxy_overload_detached"}


@pytest.mark.asyncio
async def test_profile_get_scaffold_returns_local_account_fallback(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        username="local_user",
        display_name="Local User",
        status="warming",
        health_score=72,
        daily_limits={"profile": {"first_name": "Local", "last_name": "User", "bio": "safe local bio"}},
    )

    profile = await get_account_profile(session, account)

    assert profile.account_id == account.id
    assert profile.username == "local_user"
    assert profile.first_name == "Local"
    assert profile.last_name == "User"
    assert profile.display_name == "Local User"
    assert profile.bio == "safe local bio"
    assert profile.about == "safe local bio"
    assert profile.phone is None
    assert profile.status == "warming"
    assert profile.health_score == 72
    assert profile.can_edit_security is False


@pytest.mark.asyncio
async def test_generated_ai_profile_respects_requested_gender(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)

    female_profile = generated_profile_for_account(
        account,
        index=0,
        style_prompt="деловой профиль",
        profile_gender="female",
    )
    male_profile = generated_profile_for_account(
        account,
        index=0,
        style_prompt="деловой профиль",
        profile_gender="male",
    )

    assert female_profile["gender"] == "female"
    assert male_profile["gender"] == "male"
    assert str(female_profile["last_name"]).endswith("а")
    assert not str(male_profile["last_name"]).endswith("а")
    assert len(str(female_profile["bio"])) <= 68
    assert len(str(male_profile["bio"])) <= 68


@pytest.mark.asyncio
async def test_account_response_exposes_profile_identity_and_cached_photo(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        display_name="Internal Slot",
        daily_limits={
            "session_import": {
                "batch_id": "batch-1",
                "batch_size": 21,
                "batch_at": "2026-05-29T12:00:00+00:00",
            },
            "profile": {
                "first_name": "Ирина",
                "last_name": "Иванова",
                "phone": "+15550101000",
                "bio": "safe bio",
                "photo_data_url": "data:image/jpeg;base64,abcd",
            },
        },
    )

    response = accounts_api.TelegramAccountResponse.model_validate(account)

    assert response.display_name == "Internal Slot"
    assert response.first_name == "Ирина"
    assert response.last_name == "Иванова"
    assert response.phone == "+15550101000"
    assert response.profile_photo_data_url == "data:image/jpeg;base64,abcd"
    assert response.import_batch_id == "batch-1"
    assert response.import_batch_size == 21


@pytest.mark.asyncio
async def test_real_profile_get_caches_identity_and_avatar(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace, display_name="Internal Slot", status="active")

    async def fake_get_profile(*args: object, **kwargs: object) -> TelegramProfileInfo:
        return TelegramProfileInfo(
            tg_user_id=123,
            username="profile_user",
            first_name="Ирина",
            last_name="Иванова",
            display_name="Ирина Иванова",
            about="bio",
            phone=None,
            photo_data_url="data:image/jpeg;base64,photo",
        )

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.telegram_gateway.get_account_profile", fake_get_profile)

    profile = await get_account_profile(session, account)
    await session.refresh(account)

    assert profile.photo_data_url == "data:image/jpeg;base64,photo"
    assert account.daily_limits["profile"]["first_name"] == "Ирина"
    assert account.daily_limits["profile"]["last_name"] == "Иванова"
    assert account.daily_limits["profile"]["photo_data_url"] == "data:image/jpeg;base64,photo"


@pytest.mark.asyncio
async def test_real_profile_get_returns_fresh_cached_snapshot_without_gateway(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        display_name="Internal Slot",
        status="active",
        daily_limits={
            "profile": {
                "first_name": "Мария",
                "last_name": "Войтова",
                "phone": "15859406271",
                "photo_data_url": "data:image/jpeg;base64,cached",
            }
        },
    )

    async def fail_get_profile(*args: object, **kwargs: object) -> TelegramProfileInfo:
        raise AssertionError("fresh cached profile should not hit Telegram")

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.telegram_gateway.get_account_profile", fail_get_profile)

    profile = await get_account_profile(session, account)

    assert profile.first_name == "Мария"
    assert profile.last_name == "Войтова"
    assert profile.phone == "15859406271"
    assert profile.photo_data_url == "data:image/jpeg;base64,cached"
    assert profile.can_edit_security is True


@pytest.mark.asyncio
async def test_check_account_does_not_poll_service_bots_and_preserves_runtime_restriction(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        status="quarantined",
        health_score=35,
        daily_limits={
            "restriction_status": {
                "status": "limited",
                "label": "runtime PeerFlood",
                "checked_at": "2026-06-16T10:00:00+00:00",
                "until_text": None,
                "until_at": None,
            }
        },
    )

    async def fake_check_account(*args: object, **kwargs: object) -> TelegramAccountInfo:
        return TelegramAccountInfo(
            tg_user_id=123,
            username="restricted_user",
            display_name="Restricted User",
            phone=None,
        )

    async def fake_get_profile(*args: object, **kwargs: object) -> TelegramProfileInfo:
        return TelegramProfileInfo(
            tg_user_id=123,
            username="restricted_user",
            first_name="Restricted",
            last_name="User",
            display_name="Restricted User",
            about=None,
            phone=None,
            photo_data_url=None,
        )

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.check_real_account", fake_check_account)
    monkeypatch.setattr("app.modules.telegram_accounts.service.telegram_gateway.get_account_profile", fake_get_profile)

    checked = await accounts_api.check(account.id, session, workspace.id)
    events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert checked.status == "quarantined"
    assert checked.health_score == 35
    assert checked.restriction_status == "limited"
    assert events[-1].event_type == "account_checked"
    assert events[-1].raw_error["restriction_probe_skipped"] == "active_probe_disabled"


@pytest.mark.asyncio
async def test_check_account_caches_missing_profile_avatar(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace, status="active", health_score=90)

    async def fake_check_account(*args: object, **kwargs: object) -> TelegramAccountInfo:
        return TelegramAccountInfo(tg_user_id=123, username="fresh_user", display_name="Fresh User", phone=None)

    async def fake_get_profile(*args: object, **kwargs: object) -> TelegramProfileInfo:
        return TelegramProfileInfo(
            tg_user_id=123,
            username="fresh_user",
            first_name="Ирина",
            last_name="Иванова",
            display_name="Ирина Иванова",
            about="bio",
            phone=None,
            photo_data_url="data:image/jpeg;base64,photo",
        )

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.check_real_account", fake_check_account)
    monkeypatch.setattr("app.modules.telegram_accounts.service.telegram_gateway.get_account_profile", fake_get_profile)

    checked = await accounts_api.check(account.id, session, workspace.id)
    await session.refresh(account)

    assert checked.first_name == "Ирина"
    assert checked.last_name == "Иванова"
    assert checked.profile_photo_data_url == "data:image/jpeg;base64,photo"
    assert account.daily_limits["profile"]["photo_data_url"] == "data:image/jpeg;base64,photo"
    assert account.daily_limits["profile"]["photo_checked_at"]


@pytest.mark.asyncio
async def test_profile_load_uses_cached_photo_when_gateway_omits_it(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        status="active",
        daily_limits={"profile": {"photo_data_url": "data:image/jpeg;base64,cached"}},
    )

    async def fake_get_profile(*args: object, **kwargs: object) -> TelegramProfileInfo:
        return TelegramProfileInfo(
            tg_user_id=123,
            username="fresh_user",
            first_name="Fresh",
            last_name="User",
            display_name="Fresh User",
            about="bio",
            phone=None,
            photo_data_url=None,
        )

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.telegram_gateway.get_account_profile", fake_get_profile)

    profile = await get_account_profile(session, account)

    assert profile.photo_data_url == "data:image/jpeg;base64,cached"


@pytest.mark.asyncio
async def test_bulk_ai_profiles_rejects_multiple_accounts_on_same_proxy(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    first = await _account(
        session,
        workspace,
        display_name="First",
        status="active",
        proxy_id=proxy.id,
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    second = await _account(
        session,
        workspace,
        display_name="Second",
        status="active",
        proxy_id=proxy.id,
        daily_limits={"restriction_status": {"status": "clear"}},
    )

    with pytest.raises(HTTPException) as exc:
        await accounts_api.bulk_ai_profiles(
            AccountBulkAiProfilesRequest(account_ids=[first.id, second.id]),
            session,
            workspace.id,
        )

    assert exc.value.status_code == 400
    assert "одним прокси" in exc.value.detail


@pytest.mark.asyncio
async def test_bulk_clone_profile_rejects_multiple_accounts_without_proxy(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    first = await _account(session, workspace, display_name="First", status="active", proxy_id=None)
    second = await _account(session, workspace, display_name="Second", status="active", proxy_id=None)

    with pytest.raises(HTTPException) as exc:
        await accounts_api.bulk_clone_profile(
            AccountBulkCloneProfileRequest(account_ids=[first.id, second.id], source_ref="@source"),
            session,
            workspace.id,
        )

    assert exc.value.status_code == 400
    assert "без прокси" in exc.value.detail


@pytest.mark.asyncio
async def test_limited_account_risk_blocks_outbound_modules(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        status="quarantined",
        health_score=35,
        daily_limits={
            "restriction_status": {
                "status": "limited",
                "raw_text": "While the account is limited, you will not be able to send messages...",
            }
        },
    )

    risk = await accounts_api.account_risk(account.id, session, workspace.id)

    assert risk["risk_profile"]["risk_level"] == "critical"
    assert "telegram_restriction_limited" in risk["risk_profile"]["hard_blocks"]
    assert risk["module_eligibility"]["neurocommenting"]["status"] == "blocked"
    assert risk["module_eligibility"]["bulk_join_chats"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_account_risk_blocks_overloaded_proxy(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    first = await _account(
        session,
        workspace,
        status="active",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
    )
    await _account(
        session,
        workspace,
        username="second",
        status="active",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
    )

    risk = await accounts_api.account_risk(first.id, session, workspace.id)

    assert "proxy_overloaded" in risk["risk_profile"]["hard_blocks"]
    assert risk["module_eligibility"]["neurocommenting"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_account_risk_summary_counts_blocks(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    await _account(
        session,
        workspace,
        status="active",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    await _account(
        session,
        workspace,
        status="quarantined",
        health_score=35,
        daily_limits={"restriction_status": {"status": "limited"}},
    )

    summary = await accounts_api.risk_summary(session, workspace.id)

    assert summary.total == 2
    assert summary.risk_levels["critical"] == 1
    assert summary.module_eligibility["neurocommenting"]["blocked"] == 1
    assert summary.restriction_statuses["limited"] == 1
    assert summary.hard_blocks["telegram_restriction_limited"] == 1


@pytest.mark.asyncio
async def test_quality_score_does_not_override_limited_restriction(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        status="active",
        health_score=95,
        daily_limits={"restriction_status": {"status": "limited"}},
    )

    response = await accounts_api.create_quality_check(
        account.id,
        AccountQualityCheckCreate(
            provider="gramgpt",
            rating=9.3,
            potential_30d=4.0,
            label="high",
            potential_label="medium",
            raw={"source": "manual"},
        ),
        session,
        workspace.id,
    )
    risk = await accounts_api.account_risk(account.id, session, workspace.id)
    checks = list((await session.execute(select(AccountQualityCheck))).scalars().all())

    assert response["provider"] == "gramgpt"
    assert len(checks) == 1
    assert risk["risk_profile"]["risk_level"] == "critical"
    assert "telegram_restriction_limited" in risk["risk_profile"]["hard_blocks"]
    assert "quality_score_is_not_permission" in risk["risk_profile"]["warnings"]
    assert risk["module_eligibility"]["neurocommenting"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_blocked_bulk_action_records_risk_decision(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        status="active",
        daily_limits={"restriction_status": {"status": "limited"}},
    )

    with pytest.raises(HTTPException):
        await accounts_api.bulk_join_chats(
            AccountBulkJoinChatsRequest(account_ids=[account.id], target_refs=["@target"]),
            session,
            workspace.id,
        )
    decisions = list((await session.execute(select(RiskDecision))).scalars().all())

    assert len(decisions) == 1
    assert decisions[0].module == "bulk_join_chats"
    assert decisions[0].decision == "blocked"
    assert "telegram_restriction_limited" in decisions[0].reasons


@pytest.mark.asyncio
async def test_import_batches_marks_toxic_purchase(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    batch_meta = {
        "session_import": {
            "batch_id": "batch-toxic",
            "batch_size": 4,
            "batch_at": "2026-06-01T10:00:00+00:00",
        }
    }
    await _account(
        session,
        workspace,
        username="clear",
        status="active",
        daily_limits={**batch_meta, "restriction_status": {"status": "clear"}},
    )
    for index in range(3):
        await _account(
            session,
            workspace,
            username=f"limited_{index}",
            status="quarantined",
            daily_limits={**batch_meta, "restriction_status": {"status": "limited"}},
        )

    batches = await accounts_api.import_batches(session, workspace.id)

    assert len(batches) == 1
    assert batches[0].batch_id == "batch-toxic"
    assert batches[0].account_count == 4
    assert batches[0].limited_count == 3
    assert batches[0].spam_block_rate == 0.75
    assert batches[0].verdict == "toxic_do_not_scale"


@pytest.mark.asyncio
async def test_import_batch_metadata_can_be_updated_for_purchase_cohort(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    batch_meta = {
        "session_import": {
            "batch_id": "batch-meta",
            "batch_size": 2,
            "batch_at": "2026-06-01T10:00:00+00:00",
        }
    }
    first = await _account(session, workspace, username="first", daily_limits=batch_meta)
    second = await _account(session, workspace, username="second", daily_limits=batch_meta)

    response = await accounts_api.update_import_batch(
        "batch-meta",
        AccountImportBatchPatch(
            seller="Koiler",
            purchase_date="2026-06-01",
            declared_geo="US",
            declared_age="30+ days",
            notes="test batch",
            status="toxic",
        ),
        session,
        workspace.id,
    )
    accounts = {account.id: account for account in (await session.execute(select(TelegramAccount))).scalars().all()}

    assert response.seller == "Koiler"
    assert response.declared_geo == "US"
    assert response.status == "toxic"
    assert accounts[first.id].daily_limits["session_import"]["cohort"]["seller"] == "Koiler"
    assert accounts[second.id].daily_limits["session_import"]["cohort"]["seller"] == "Koiler"


@pytest.mark.asyncio
async def test_limited_account_rejects_bulk_join_chats(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        status="quarantined",
        daily_limits={"restriction_status": {"status": "limited"}},
    )

    with pytest.raises(HTTPException) as exc:
        await accounts_api.bulk_join_chats(
            AccountBulkJoinChatsRequest(account_ids=[account.id], target_refs=["@target"]),
            session,
            workspace.id,
        )

    assert exc.value.status_code == 400
    assert "Telegram-ограничение" in exc.value.detail


@pytest.mark.asyncio
async def test_incubator_account_rejects_bulk_join_chats(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        status="active",
        daily_limits={"account_metadata": {"incubator_mode": True}},
        warmup_score=100,
        warmup_status="running",
    )

    with pytest.raises(HTTPException) as exc:
        await accounts_api.bulk_join_chats(
            AccountBulkJoinChatsRequest(account_ids=[account.id], target_refs=["@target"]),
            session,
            workspace.id,
        )
    decisions = list((await session.execute(select(RiskDecision))).scalars().all())

    assert exc.value.status_code == 400
    assert "инкубатор" in str(exc.value.detail).lower()
    assert len(decisions) == 1
    assert decisions[0].module == "bulk_join_chats"
    assert decisions[0].decision == "blocked"
    assert "incubator_mode" in decisions[0].reasons


@pytest.mark.asyncio
async def test_profile_patch_scaffold_updates_local_metadata_and_health_event(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    account = await _account(
        session,
        workspace,
        status="new",
        proxy_id=proxy.id,
        health_score=90,
        daily_limits={"restriction_status": {"status": "clear"}},
    )

    profile = await accounts_api.update_profile(
        account.id,
        TelegramAccountProfilePatch(first_name="New", last_name="Name", username="new_user", bio="updated bio"),
        session,
        workspace.id,
    )
    await session.refresh(account)
    events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert profile.display_name == "Old Name"
    assert profile.username == "new_user"
    assert profile.bio == "updated bio"
    assert account.display_name == "Old Name"
    assert account.username == "new_user"
    assert account.status == "warming"
    assert account.health_score >= 70
    assert events[-1].event_type == "account_profile_updated"
    assert events[-1].raw_error == {
        "mode": "scaffold",
        "fields": ["first_name", "last_name", "username", "bio"],
    }


@pytest.mark.asyncio
async def test_session_import_defaults_display_name_to_phone(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=1,
    )
    session.add(proxy)
    await session.flush()

    account = await accounts_api.import_session(
        TelegramAccountImport(
            session_blob="session",
            phone="+15550101000",
            username="imported_user",
            display_name=None,
            proxy_id=proxy.id,
            proxy_required=True,
        ),
        session,
        workspace.id,
    )

    assert account.display_name == "+15550101000"


@pytest.mark.asyncio
async def test_profile_queue_returns_immediately_and_records_event(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    sent_tasks: list[tuple[str, dict]] = []

    def fake_send_task(name: str, kwargs: dict) -> None:
        sent_tasks.append((name, kwargs))

    monkeypatch.setattr(accounts_api.celery_app, "send_task", fake_send_task)

    profile = await accounts_api.queue_profile_update(
        account.id,
        TelegramAccountProfilePatch(first_name="Fast", last_name="Queue"),
        session,
        workspace.id,
    )
    events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert profile.display_name == "Old Name"
    assert account.display_name == "Old Name"
    assert events[-1].event_type == "account_profile_update_queued"
    assert sent_tasks[0][0] == "app.workers.tasks.update_account_profile"
    assert sent_tasks[0][1]["account_id"] == account.id


@pytest.mark.asyncio
async def test_bulk_join_chats_queues_worker_with_random_delay_config(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    account = await _account(
        session, workspace, status="active", proxy_id=proxy.id, daily_limits={"restriction_status": {"status": "clear"}}
    )
    sent_tasks: list[tuple[str, dict, dict]] = []

    def fake_send_task(name: str, kwargs: dict, **options: object) -> None:
        sent_tasks.append((name, kwargs, options))

    monkeypatch.setattr(accounts_api.celery_app, "send_task", fake_send_task)

    response = await accounts_api.bulk_join_chats(
        AccountBulkJoinChatsRequest(
            account_ids=[account.id],
            target_refs=["@first_chat", "https://t.me/second_chat"],
            delay_seconds_min=90,
            delay_seconds_max=600,
        ),
        session,
        workspace.id,
    )
    events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert response.status == "queued"
    assert response.request_id
    assert sent_tasks == [
        (
            "app.workers.tasks.bulk_join_chats",
            {
                "account_ids": [account.id],
                "target_refs": ["@first_chat", "https://t.me/second_chat"],
                "delay_seconds_min": 90,
                "delay_seconds_max": 600,
                "split_folder_links": True,
                "request_id": response.request_id,
            },
            {"queue": "telegram.actions"},
        )
    ]
    assert events[-1].event_type == "bulk_join_chats_requested"
    assert events[-1].raw_error["bulk_request_id"] == response.request_id


@pytest.mark.asyncio
async def test_profile_patch_rejects_frozen_account_before_real_gateway(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace, status="frozen", health_score=10)

    async def fail_gateway(*args: object, **kwargs: object) -> object:
        raise AssertionError("frozen account must not call Telegram profile API")

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.telegram_gateway.update_account_profile", fail_gateway)

    with pytest.raises(HTTPException) as exc:
        await accounts_api.update_profile(
            account.id,
            TelegramAccountProfilePatch(first_name="New"),
            session,
            workspace.id,
        )

    assert exc.value.status_code == 400
    assert "заморож" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_profile_photo_rejects_non_image_or_too_large_before_gateway(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)

    with pytest.raises(HTTPException) as invalid_type:
        await accounts_api.update_profile_photo(
            account.id,
            session,
            workspace.id,
            _upload_file("text/plain", b"not image"),
        )
    assert invalid_type.value.status_code == 400
    assert "image/jpeg" in invalid_type.value.detail

    with pytest.raises(HTTPException) as too_large:
        await accounts_api.update_profile_photo(
            account.id,
            session,
            workspace.id,
            _upload_file("image/png", b"x" * (5 * 1024 * 1024 + 1)),
        )
    assert too_large.value.status_code == 400
    assert "5 MB" in too_large.value.detail


@pytest.mark.asyncio
async def test_clone_profile_scaffold_copies_imported_source_profile_and_keeps_target_username(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    source = await _account(
        session,
        workspace,
        username="source_user",
        display_name="Source Display",
        daily_limits={"profile": {"first_name": "Source", "last_name": "User", "bio": "source bio"}},
    )
    target = await _account(
        session,
        workspace,
        username="target_user",
        display_name="Target Display",
        status="new",
        health_score=45,
        daily_limits={"profile": {"first_name": "Target", "last_name": "User", "bio": "old bio"}},
    )

    result = await accounts_api.clone_profile(
        target.id,
        TelegramAccountCloneProfileRequest(source_account_id=source.id),
        session,
        workspace.id,
    )
    await session.refresh(target)
    events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert result.status == "ok"
    assert result.profile.display_name == "Source User"
    assert result.profile.username == "target_user"
    assert result.profile.bio == "source bio"
    assert target.display_name == "Source User"
    assert target.username == "target_user"
    assert target.status == "warming"
    assert target.health_score >= 70
    assert target.daily_limits["profile"] == {"first_name": "Source", "last_name": "User", "bio": "source bio"}
    assert result.copied_fields == ["name", "bio"]
    assert "username" in result.skipped_fields
    assert events[-1].event_type == "account_profile_cloned"
    assert events[-1].raw_error["mode"] == "scaffold"
    assert events[-1].raw_error["source_account_id"] == source.id


@pytest.mark.asyncio
async def test_clone_profile_requires_source(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    target = await _account(session, workspace)

    with pytest.raises(HTTPException) as exc:
        await accounts_api.clone_profile(
            target.id,
            TelegramAccountCloneProfileRequest(),
            session,
            workspace.id,
        )

    assert exc.value.status_code == 400
    assert "источник" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_clone_profile_rejects_frozen_target_before_real_gateway(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    target = await _account(session, workspace, status="frozen", health_score=10)

    async def fail_gateway(*args: object, **kwargs: object) -> object:
        raise AssertionError("frozen account must not call Telegram clone API")

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.telegram_gateway.clone_account_profile", fail_gateway)

    with pytest.raises(HTTPException) as exc:
        await accounts_api.clone_profile(
            target.id,
            TelegramAccountCloneProfileRequest(source_ref="@source_user"),
            session,
            workspace.id,
        )

    assert exc.value.status_code == 400
    assert "заморож" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_2fa_requires_confirmation_and_never_exposes_passwords_in_health_event(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)

    with pytest.raises(HTTPException) as invalid_confirmation:
        await accounts_api.update_2fa(
            account.id,
            TelegramAccountTwoFaUpdate(
                current_password="old-secret",
                new_password="new-secret",
                hint="private hint",
                confirmation_text="change",
            ),
            session,
            workspace.id,
        )
    assert invalid_confirmation.value.status_code == 400

    result = await accounts_api.update_2fa(
        account.id,
        TelegramAccountTwoFaUpdate(
            current_password="old-secret",
            new_password=None,
            hint="private hint",
            confirmation_text="CHANGE 2FA",
        ),
        session,
        workspace.id,
    )
    events = list((await session.execute(select(AccountHealthEvent))).scalars().all())
    serialized_events = " ".join(f"{event.message} {event.raw_error}" for event in events)

    assert result["status"] == "ok"
    assert events[-1].event_type == "account_2fa_updated"
    assert "old-secret" not in serialized_events
    assert "new-secret" not in serialized_events
    assert "private hint" not in serialized_events
    assert events[-1].raw_error == {"mode": "scaffold", "action": "disabled"}
