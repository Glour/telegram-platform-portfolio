from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Query, UploadFile
from sqlalchemy import select

from app.api.deps import SessionDep, WorkspaceIdDep
from app.core.errors import bad_request, not_found
from app.modules.account_trust.models import AccountTrustSnapshot
from app.modules.account_trust.schemas import (
    AccountTechnicalCheckResponse,
    AccountTrustSnapshotResponse,
    AccountTrustSummaryResponse,
)
from app.modules.account_trust.service import (
    account_trust_history,
    ensure_account_trust_snapshot,
    latest_account_trust_snapshot,
    recompute_account_trust,
    trust_summary,
)
from app.modules.proxies.models import Proxy
from app.modules.proxies.service import active_proxy_account_count, ensure_proxy_account_capacity
from app.modules.telegram_accounts.avatars import account_list_avatar
from app.modules.telegram_accounts.bulk_ai_profiles import record_bulk_action_for_account_ids
from app.modules.telegram_accounts.models import AccountHealthEvent, AccountQualityCheck, RiskDecision, TelegramAccount
from app.modules.telegram_accounts.risk import (
    build_account_risk_profile,
    get_module_eligibility,
)
from app.modules.telegram_accounts.schemas import (
    AccountBulkActionResponse,
    AccountBulkAiProfilesRequest,
    AccountBulkCloneProfileRequest,
    AccountBulkCopyStoriesRequest,
    AccountBulkDetachOverloadedProxiesRequest,
    AccountBulkJoinChatsRequest,
    AccountBulkStatusRequest,
    AccountHealthEventResponse,
    AccountImportBatchPatch,
    AccountImportBatchResponse,
    AccountQualityCheckCreate,
    AccountQualityCheckResponse,
    AccountRiskResponse,
    AccountRiskSummaryResponse,
    ImmortalCandidateResponse,
    RiskDecisionResponse,
    TelegramAccountCloneProfileRequest,
    TelegramAccountCloneProfileResponse,
    TelegramAccountImport,
    TelegramAccountLoginCodeResponse,
    TelegramAccountOrderUpdate,
    TelegramAccountPatch,
    TelegramAccountProfilePatch,
    TelegramAccountProfilePhotosResponse,
    TelegramAccountProfileResponse,
    TelegramAccountResponse,
    TelegramAccountTwoFaUpdate,
    TelegramPhoneLoginCodeConfirm,
    TelegramPhoneLoginCompleteResponse,
    TelegramPhoneLoginPasswordConfirm,
    TelegramPhoneLoginStart,
    TelegramPhoneLoginStartResponse,
    TelegramSessionArchiveImportResponse,
    TelegramSessionFileImportMeta,
    TelegramTdataImportMeta,
)
from app.modules.telegram_accounts.service import (
    check_account,
    clone_account_profile,
    confirm_phone_login_code,
    confirm_phone_login_password,
    delete_account_profile_photo,
    get_account_profile,
    import_session_account,
    import_session_archive_accounts,
    import_session_file_account,
    import_tdata_account,
    list_account_profile_photos,
    queue_account_profile_update,
    request_account_login_code,
    start_phone_login,
    update_account_2fa,
    update_account_profile,
    update_account_profile_photo,
)
from app.workers.celery_app import celery_app

router = APIRouter()


@router.get("", response_model=list[TelegramAccountResponse])
async def list_accounts(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    status: Annotated[str | None, Query()] = None,
) -> list[dict]:
    statement = select(TelegramAccount).where(TelegramAccount.workspace_id == workspace_id)
    if status == "all":
        statement = statement.where(TelegramAccount.status != "deleted")
    elif status:
        statement = statement.where(TelegramAccount.status == status)
    else:
        statement = statement.where(TelegramAccount.status.not_in(["archived", "deleted"]))

    result = await session.execute(
        statement.order_by(TelegramAccount.sort_order.asc(), TelegramAccount.created_at.desc())
    )
    accounts = list(result.scalars().all())
    payloads = [await _account_response_with_trust(session, workspace_id, account) for account in accounts]
    for payload in payloads:
        payload["profile_photo_data_url"] = account_list_avatar(payload.get("profile_photo_data_url"))
    return payloads


@router.get("/trust-summary", response_model=AccountTrustSummaryResponse)
async def account_trust_summary(session: SessionDep, workspace_id: WorkspaceIdDep) -> AccountTrustSummaryResponse:
    return await trust_summary(session, workspace_id)


@router.put("/order", response_model=list[TelegramAccountResponse])
async def reorder_accounts(
    payload: TelegramAccountOrderUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> list[dict]:
    account_ids = payload.account_ids
    if len(account_ids) != len(set(account_ids)):
        raise bad_request("Список аккаунтов для сортировки содержит повторы")

    result = await session.execute(
        select(TelegramAccount)
        .where(TelegramAccount.workspace_id == workspace_id)
        .where(TelegramAccount.status.not_in(["archived", "deleted"]))
    )
    accounts = list(result.scalars().all())
    account_by_id = {account.id: account for account in accounts}
    if set(account_ids) != set(account_by_id):
        raise bad_request("Передайте полный актуальный список рабочих аккаунтов")

    for position, account_id in enumerate(account_ids):
        account_by_id[account_id].sort_order = position
    await session.commit()

    ordered_accounts = [account_by_id[account_id] for account_id in account_ids]
    return [await _account_response_with_trust(session, workspace_id, account) for account in ordered_accounts]


@router.get("/risk-summary", response_model=AccountRiskSummaryResponse)
async def risk_summary(session: SessionDep, workspace_id: WorkspaceIdDep) -> AccountRiskSummaryResponse:
    accounts = await _workspace_active_accounts(session, workspace_id)
    proxy_by_id, proxy_counts = await _proxy_context_for_accounts(session, workspace_id, accounts)
    risk_levels = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    module_eligibility = {
        module: {"allowed": 0, "limited": 0, "blocked": 0}
        for module in ["warmup_safe", "neurocommenting", "neurochatting", "mass_reactions"]
    }
    restriction_statuses: dict[str, int] = {}
    hard_blocks: dict[str, int] = {}
    for account in accounts:
        proxy = proxy_by_id.get(account.proxy_id or "")
        proxy_account_count = proxy_counts.get(account.proxy_id or "")
        profile = build_account_risk_profile(account, proxy=proxy, proxy_account_count=proxy_account_count)
        risk_levels[profile.risk_level] = risk_levels.get(profile.risk_level, 0) + 1
        for hard_block in profile.hard_blocks:
            hard_blocks[hard_block] = hard_blocks.get(hard_block, 0) + 1
        restriction = account.restriction_status or "unknown"
        restriction_statuses[restriction] = restriction_statuses.get(restriction, 0) + 1
        for module in module_eligibility:
            eligibility = get_module_eligibility(
                account,
                module,
                proxy=proxy,
                proxy_account_count=proxy_account_count,
            )
            module_eligibility[module][eligibility.status] = module_eligibility[module].get(eligibility.status, 0) + 1
    return AccountRiskSummaryResponse(
        total=len(accounts),
        risk_levels=risk_levels,
        module_eligibility=module_eligibility,
        restriction_statuses=restriction_statuses,
        hard_blocks=hard_blocks,
    )


@router.get("/import-batches", response_model=list[AccountImportBatchResponse])
async def import_batches(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[AccountImportBatchResponse]:
    accounts = await _workspace_active_accounts(session, workspace_id)
    if not accounts:
        return []
    proxy_ids = sorted({account.proxy_id for account in accounts if account.proxy_id})
    proxy_by_id: dict[str, Proxy] = {}
    if proxy_ids:
        proxy_result = await session.execute(
            select(Proxy).where(Proxy.workspace_id == workspace_id, Proxy.id.in_(proxy_ids))
        )
        proxy_by_id = {proxy.id: proxy for proxy in proxy_result.scalars().all()}
    batches: dict[str, list[TelegramAccount]] = {}
    for account in accounts:
        batch_id = account.import_batch_id or f"single:{account.id}"
        batches.setdefault(batch_id, []).append(account)

    responses = [
        _build_import_batch_response(batch_id, batch_accounts, proxy_by_id)
        for batch_id, batch_accounts in batches.items()
    ]
    return sorted(
        responses,
        key=lambda item: item.imported_at or "",
        reverse=True,
    )


@router.get("/immortal-candidates", response_model=list[ImmortalCandidateResponse])
async def immortal_candidates(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[dict]:
    accounts = await _workspace_active_accounts(session, workspace_id)
    proxy_ids = sorted({account.proxy_id for account in accounts if account.proxy_id})
    proxy_by_id: dict[str, Proxy] = {}
    if proxy_ids:
        proxy_result = await session.execute(
            select(Proxy).where(Proxy.workspace_id == workspace_id, Proxy.id.in_(proxy_ids))
        )
        proxy_by_id = {proxy.id: proxy for proxy in proxy_result.scalars().all()}

    candidates: list[tuple[int, list[str], TelegramAccount]] = []
    for account in accounts:
        if account.incubator_mode:
            continue
        score, reasons = _immortal_candidate_score(account, proxy_by_id.get(account.proxy_id or ""))
        if score >= 70:
            candidates.append((score, reasons, account))
    candidates.sort(key=lambda item: (item[0], item[2].real_age_days or item[2].system_age_days), reverse=True)
    return [
        {
            "account": await _account_response_with_trust(session, workspace_id, account),
            "candidate_score": score,
            "reasons": reasons,
        }
        for score, reasons, account in candidates[:limit]
    ]


@router.get("/risk-decisions", response_model=list[RiskDecisionResponse])
async def risk_decisions(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[RiskDecision]:
    result = await session.execute(
        select(RiskDecision)
        .where(RiskDecision.workspace_id == workspace_id)
        .order_by(RiskDecision.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.patch("/import-batches/{batch_id}", response_model=AccountImportBatchResponse)
async def update_import_batch(
    batch_id: str,
    payload: AccountImportBatchPatch,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountImportBatchResponse:
    accounts = [
        account
        for account in await _workspace_active_accounts(session, workspace_id)
        if (account.import_batch_id or f"single:{account.id}") == batch_id
    ]
    if not accounts:
        raise not_found("Import batch not found")

    data = payload.model_dump(exclude_unset=True)
    clean_data = {
        key: (value.strip() if isinstance(value, str) and value.strip() else None) for key, value in data.items()
    }
    for account in accounts:
        limits = dict(account.daily_limits or {})
        session_import = dict(limits.get("session_import") or {})
        cohort = dict(session_import.get("cohort") or {})
        cohort.update(clean_data)
        session_import["cohort"] = cohort
        limits["session_import"] = session_import
        account.daily_limits = limits
    await session.commit()

    proxy_ids = sorted({account.proxy_id for account in accounts if account.proxy_id})
    proxy_by_id: dict[str, Proxy] = {}
    if proxy_ids:
        proxy_result = await session.execute(
            select(Proxy).where(Proxy.workspace_id == workspace_id, Proxy.id.in_(proxy_ids))
        )
        proxy_by_id = {proxy.id: proxy for proxy in proxy_result.scalars().all()}
    return _build_import_batch_response(batch_id, accounts, proxy_by_id)


@router.post("/phone-login/start", response_model=TelegramPhoneLoginStartResponse)
async def phone_login_start(
    payload: TelegramPhoneLoginStart,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramPhoneLoginStartResponse:
    return await start_phone_login(session, workspace_id, payload)


@router.post("/phone-login/confirm-code", response_model=TelegramPhoneLoginCompleteResponse)
async def phone_login_confirm_code(
    payload: TelegramPhoneLoginCodeConfirm,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramPhoneLoginCompleteResponse:
    return await confirm_phone_login_code(session, workspace_id, payload)


@router.post("/phone-login/confirm-password", response_model=TelegramPhoneLoginCompleteResponse)
async def phone_login_confirm_password(
    payload: TelegramPhoneLoginPasswordConfirm,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramPhoneLoginCompleteResponse:
    return await confirm_phone_login_password(session, workspace_id, payload)


@router.post("/import-session", response_model=TelegramAccountResponse, status_code=201)
async def import_session(
    payload: TelegramAccountImport,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccount:
    return await import_session_account(session, workspace_id, payload)


@router.post("/import-session-file", response_model=TelegramAccountResponse, status_code=201)
async def import_session_file(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    telethon_session: Annotated[UploadFile, File(...)],
    phone: Annotated[str | None, Form()] = None,
    username: Annotated[str | None, Form()] = None,
    display_name: Annotated[str | None, Form()] = None,
    proxy_id: Annotated[str | None, Form()] = None,
    new_proxy_raw: Annotated[str | None, Form()] = None,
    new_proxy_max_accounts: Annotated[int, Form(ge=1, le=100)] = 1,
    proxy_required: Annotated[bool, Form()] = True,
    warmup_status: Annotated[str | None, Form()] = None,
    warmup_score: Annotated[int | None, Form(ge=0, le=100)] = None,
) -> TelegramAccount:
    meta = TelegramSessionFileImportMeta(
        phone=phone or None,
        username=username or None,
        display_name=display_name or None,
        proxy_id=proxy_id or None,
        new_proxy_raw=new_proxy_raw or None,
        new_proxy_max_accounts=new_proxy_max_accounts,
        proxy_required=proxy_required,
        warmup_status=warmup_status or None,
        warmup_score=warmup_score,
    )
    return await import_session_file_account(session, workspace_id, telethon_session, meta)


@router.post("/import-session-archive", response_model=TelegramSessionArchiveImportResponse, status_code=201)
async def import_session_archive(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    sessions_archive: Annotated[UploadFile, File(...)],
    proxy_id: Annotated[str | None, Form()] = None,
    proxy_ids: Annotated[list[str] | None, Form()] = None,
    new_proxy_raw_text: Annotated[str | None, Form()] = None,
    new_proxy_max_accounts: Annotated[int, Form(ge=1, le=100)] = 1,
    auto_assign_free_proxies: Annotated[bool, Form()] = False,
    proxy_required: Annotated[bool, Form()] = True,
    warmup_status: Annotated[str | None, Form()] = None,
    warmup_score: Annotated[int | None, Form(ge=0, le=100)] = None,
) -> dict:
    meta = TelegramSessionFileImportMeta(
        proxy_id=proxy_id or None,
        proxy_required=proxy_required,
        warmup_status=warmup_status or None,
        warmup_score=warmup_score,
    )
    clean_proxy_ids = [item for item in proxy_ids or [] if item]
    return await import_session_archive_accounts(
        session,
        workspace_id,
        sessions_archive,
        meta,
        clean_proxy_ids,
        new_proxy_raw_text=new_proxy_raw_text or None,
        new_proxy_max_accounts=new_proxy_max_accounts,
        auto_assign_free_proxies=auto_assign_free_proxies,
    )


@router.post("/import-tdata", response_model=TelegramAccountResponse, status_code=201)
async def import_tdata(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    tdata_archive: Annotated[UploadFile, File(...)],
    phone: Annotated[str | None, Form()] = None,
    username: Annotated[str | None, Form()] = None,
    display_name: Annotated[str | None, Form()] = None,
    proxy_id: Annotated[str | None, Form()] = None,
    new_proxy_raw: Annotated[str | None, Form()] = None,
    new_proxy_max_accounts: Annotated[int, Form(ge=1, le=100)] = 1,
    proxy_required: Annotated[bool, Form()] = True,
    password: Annotated[str | None, Form()] = None,
    warmup_status: Annotated[str | None, Form()] = None,
    warmup_score: Annotated[int | None, Form(ge=0, le=100)] = None,
) -> TelegramAccount:
    meta = TelegramTdataImportMeta(
        phone=phone or None,
        username=username or None,
        display_name=display_name or None,
        proxy_id=proxy_id or None,
        new_proxy_raw=new_proxy_raw or None,
        new_proxy_max_accounts=new_proxy_max_accounts,
        proxy_required=proxy_required,
        password=password or None,
        warmup_status=warmup_status or None,
        warmup_score=warmup_score,
    )
    return await import_tdata_account(session, workspace_id, tdata_archive, meta)


@router.post("/bulk/join-chats", response_model=AccountBulkActionResponse, status_code=202)
async def bulk_join_chats(
    payload: AccountBulkJoinChatsRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountBulkActionResponse:
    accounts = await _get_workspace_accounts(session, workspace_id, payload.account_ids)
    await _ensure_module_allowed_for_accounts(session, workspace_id, accounts, "bulk_join_chats")
    warnings = _account_bulk_warnings(accounts)
    request_id = str(uuid4())
    if payload.delay_seconds_max < payload.delay_seconds_min:
        raise bad_request("Максимальная задержка не может быть меньше минимальной")
    await _record_bulk_action(
        session,
        accounts,
        event_type="bulk_join_chats_requested",
        message=f"Запрошено вступление в {len(payload.target_refs)} чатов",
        raw={
            "target_refs": payload.target_refs,
            "delay_seconds_min": payload.delay_seconds_min,
            "delay_seconds_max": payload.delay_seconds_max,
            "split_folder_links": payload.split_folder_links,
            "bulk_request_id": request_id,
            "status": "queued",
        },
    )
    celery_app.send_task(
        "app.workers.tasks.bulk_join_chats",
        kwargs={
            "account_ids": [account.id for account in accounts],
            "target_refs": payload.target_refs,
            "delay_seconds_min": payload.delay_seconds_min,
            "delay_seconds_max": payload.delay_seconds_max,
            "split_folder_links": payload.split_folder_links,
            "request_id": request_id,
        },
        queue="telegram.actions",
    )
    return AccountBulkActionResponse(
        status="queued",
        action="join_chats",
        account_count=len(accounts),
        target_count=len(payload.target_refs),
        message="Задача вступления в чаты поставлена в очередь",
        warnings=warnings,
        request_id=request_id,
    )


@router.post("/bulk/clone-profile", response_model=AccountBulkActionResponse, status_code=202)
async def bulk_clone_profile(
    payload: AccountBulkCloneProfileRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountBulkActionResponse:
    accounts = await _get_workspace_accounts(session, workspace_id, payload.account_ids)
    await _ensure_module_allowed_for_accounts(session, workspace_id, accounts, "bulk_clone_profile")
    _ensure_safe_proxy_distribution(
        accounts,
        action_name="массовое копирование профиля",
        risky_fields=[
            payload.copy_name,
            payload.copy_bio,
            payload.copy_photo,
            payload.copy_stories,
            payload.pin_stories,
        ],
    )
    if not payload.source_ref and not payload.source_account_id:
        raise bad_request("Укажите источник профиля: ссылку/username или аккаунт-источник")
    if payload.source_account_id:
        await _get_workspace_account(session, workspace_id, payload.source_account_id)
    warnings = _account_bulk_warnings(accounts)
    request_id = str(uuid4())
    await _record_bulk_action(
        session,
        accounts,
        event_type="bulk_clone_profile_requested",
        message="Задача копирования профиля отправлена в фон",
        raw={
            "bulk_request_id": request_id,
            "source_ref": payload.source_ref,
            "source_account_id": payload.source_account_id,
            "copy_name": payload.copy_name,
            "copy_bio": payload.copy_bio,
            "copy_photo": payload.copy_photo,
            "copy_stories": payload.copy_stories,
            "pin_stories": payload.pin_stories,
            "delete_old_stories": payload.delete_old_stories,
            "story_limit": payload.story_limit,
        },
    )
    celery_app.send_task(
        "app.workers.tasks.bulk_clone_profile",
        kwargs={
            "account_ids": [account.id for account in accounts],
            "source_ref": payload.source_ref,
            "source_account_id": payload.source_account_id,
            "copy_name": payload.copy_name,
            "copy_bio": payload.copy_bio,
            "copy_photo": payload.copy_photo,
            "copy_stories": payload.copy_stories,
            "pin_stories": payload.pin_stories,
            "delete_old_stories": payload.delete_old_stories,
            "story_limit": payload.story_limit,
        },
    )
    return AccountBulkActionResponse(
        status="queued",
        action="clone_profile",
        account_count=len(accounts),
        target_count=1,
        message="Задача копирования профиля отправлена в фон",
        warnings=warnings,
        request_id=request_id,
    )


@router.post("/bulk/copy-stories", response_model=AccountBulkActionResponse, status_code=202)
async def bulk_copy_stories(
    payload: AccountBulkCopyStoriesRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountBulkActionResponse:
    accounts = await _get_workspace_accounts(session, workspace_id, payload.account_ids)
    await _ensure_module_allowed_for_accounts(session, workspace_id, accounts, "bulk_copy_stories")
    _ensure_safe_proxy_distribution(accounts, action_name="массовое копирование сторис")
    if not payload.source_ref and not payload.source_account_id:
        raise bad_request("Укажите источник сторис: ссылку/username или аккаунт-источник")
    if payload.source_account_id:
        await _get_workspace_account(session, workspace_id, payload.source_account_id)
    if payload.delay_seconds_max < payload.delay_seconds_min:
        raise bad_request("Максимальная задержка не может быть меньше минимальной")
    warnings = _account_bulk_warnings(accounts)
    await _record_bulk_action(
        session,
        accounts,
        event_type="bulk_copy_stories_requested",
        message="Запрошено копирование сторис",
        raw={
            "source_ref": payload.source_ref,
            "source_account_id": payload.source_account_id,
            "story_ids": payload.story_ids,
            "pin_on_top": payload.pin_on_top,
            "delete_old_stories": payload.delete_old_stories,
            "delay_seconds_min": payload.delay_seconds_min,
            "delay_seconds_max": payload.delay_seconds_max,
        },
    )
    story_limit = max(1, min(len(payload.story_ids) if payload.story_ids else 10, 20))
    celery_app.send_task(
        "app.workers.tasks.bulk_copy_stories",
        kwargs={
            "account_ids": [account.id for account in accounts],
            "source_ref": payload.source_ref,
            "source_account_id": payload.source_account_id,
            "story_limit": story_limit,
            "pin_on_top": payload.pin_on_top,
            "delete_old_stories": payload.delete_old_stories,
        },
    )
    return AccountBulkActionResponse(
        status="queued",
        action="copy_stories",
        account_count=len(accounts),
        target_count=story_limit,
        message="Задача копирования сторис поставлена в очередь",
        warnings=warnings,
    )


@router.post("/bulk/ai-profiles", response_model=AccountBulkActionResponse, status_code=202)
async def bulk_ai_profiles(
    payload: AccountBulkAiProfilesRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountBulkActionResponse:
    accounts = await _get_workspace_accounts(session, workspace_id, payload.account_ids)
    await _ensure_module_allowed_for_accounts(session, workspace_id, accounts, "bulk_ai_profiles")
    has_profile_work = any(
        [
            payload.generate_first_last_name,
            payload.generate_bio,
            payload.generate_photo,
            payload.custom_first_name,
            payload.custom_last_name,
            payload.custom_bio,
        ]
    )
    if not has_profile_work:
        raise bad_request("Выберите, какие данные профиля нужно создать или обновить")
    _ensure_safe_proxy_distribution(accounts, action_name="массовые AI профили")
    if payload.delay_seconds_max < payload.delay_seconds_min:
        raise bad_request("Максимальная задержка не может быть меньше минимальной")
    warnings = _account_bulk_warnings(accounts)
    request_id = str(uuid4())
    await _record_bulk_action(
        session,
        accounts,
        event_type="bulk_ai_profiles_requested",
        message="AI профили поставлены в очередь",
        raw={
            **payload.model_dump(mode="json"),
            "bulk_request_id": request_id,
            "status": "queued",
        },
    )
    celery_app.send_task(
        "app.workers.tasks.bulk_ai_profiles",
        kwargs={
            "workspace_id": workspace_id,
            "account_ids": [account.id for account in accounts],
            "request_id": request_id,
            "payload_data": payload.model_dump(mode="json"),
        },
    )
    return AccountBulkActionResponse(
        status="queued",
        action="ai_profiles",
        account_count=len(accounts),
        target_count=len(accounts),
        message="AI профили запущены в фоне",
        warnings=warnings,
        request_id=request_id,
    )


@router.post("/bulk/archive", response_model=AccountBulkActionResponse)
async def bulk_archive_accounts(
    payload: AccountBulkStatusRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountBulkActionResponse:
    accounts = await _get_workspace_accounts(session, workspace_id, payload.account_ids)
    for account in accounts:
        account.status = "archived"
    await _record_bulk_action(
        session,
        accounts,
        event_type="bulk_accounts_archived",
        message="Аккаунты массово отправлены в архив.",
        raw={"account_ids": [account.id for account in accounts]},
    )
    await session.commit()
    return AccountBulkActionResponse(
        status="completed",
        action="archive_accounts",
        account_count=len(accounts),
        target_count=0,
        message="Аккаунты отправлены в архив",
    )


@router.post("/bulk/delete", response_model=AccountBulkActionResponse)
async def bulk_delete_accounts(
    payload: AccountBulkStatusRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountBulkActionResponse:
    accounts = await _get_workspace_accounts(session, workspace_id, payload.account_ids)
    for account in accounts:
        account.status = "deleted"
    await _record_bulk_action(
        session,
        accounts,
        event_type="bulk_accounts_deleted",
        message="Аккаунты массово удалены из рабочего контура.",
        raw={"account_ids": [account.id for account in accounts]},
        severity="warning",
    )
    await session.commit()
    return AccountBulkActionResponse(
        status="completed",
        action="delete_accounts",
        account_count=len(accounts),
        target_count=0,
        message="Аккаунты удалены",
    )


@router.post("/bulk/detach-overloaded-proxies", response_model=AccountBulkActionResponse)
async def bulk_detach_overloaded_proxies(
    payload: AccountBulkDetachOverloadedProxiesRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountBulkActionResponse:
    requested_proxy_ids = list(dict.fromkeys(payload.proxy_ids))
    proxies_query = select(Proxy).where(
        Proxy.workspace_id == workspace_id,
        Proxy.status != "deleted",
    )
    if requested_proxy_ids:
        proxies_query = proxies_query.where(Proxy.id.in_(requested_proxy_ids))
    proxies = list((await session.execute(proxies_query)).scalars().all())
    if requested_proxy_ids and len(proxies) != len(requested_proxy_ids):
        raise not_found("Proxy not found")

    detached_accounts: list[TelegramAccount] = []
    processed_proxy_ids: list[str] = []
    for proxy in proxies:
        accounts_query = (
            select(TelegramAccount)
            .where(
                TelegramAccount.workspace_id == workspace_id,
                TelegramAccount.proxy_id == proxy.id,
                TelegramAccount.status.notin_(["archived", "deleted"]),
            )
            .order_by(TelegramAccount.created_at.asc(), TelegramAccount.id.asc())
        )
        assigned_accounts = list((await session.execute(accounts_query)).scalars().all())
        keep_count = max(0, proxy.max_accounts)
        if len(assigned_accounts) <= keep_count:
            continue
        processed_proxy_ids.append(proxy.id)
        for account in assigned_accounts[keep_count:]:
            account.proxy_id = None
            account.proxy_required = True
            detached_accounts.append(account)

    if detached_accounts:
        await _record_bulk_action(
            session,
            detached_accounts,
            event_type="bulk_proxy_overload_detached",
            message="Лишние аккаунты сняты с перегруженных прокси.",
            raw={
                "proxy_ids": processed_proxy_ids,
                "account_ids": [account.id for account in detached_accounts],
            },
            severity="warning",
        )
    await session.commit()
    return AccountBulkActionResponse(
        status="completed",
        action="detach_overloaded_proxies",
        account_count=len(detached_accounts),
        target_count=len(processed_proxy_ids),
        message="Лишние аккаунты сняты с перегруженных прокси",
    )


@router.post("/{account_id}/check", response_model=TelegramAccountResponse)
async def check(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    check_restrictions: Annotated[bool, Query()] = True,
    force_restriction_check: Annotated[bool, Query()] = False,
) -> TelegramAccount:
    account = await _get_workspace_account(session, workspace_id, account_id)
    return await check_account(
        session,
        account,
        check_restrictions=check_restrictions,
        force_restriction_check=force_restriction_check,
    )


@router.get("/{account_id}/risk", response_model=AccountRiskResponse)
async def account_risk(account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> dict:
    account = await _get_workspace_account(session, workspace_id, account_id)
    proxy: Proxy | None = None
    proxy_account_count: int | None = None
    if account.proxy_id:
        proxy = await session.get(Proxy, account.proxy_id)
        if proxy and proxy.workspace_id == workspace_id:
            proxy_account_count = await active_proxy_account_count(session, workspace_id, proxy.id)
        else:
            proxy = None
    profile = build_account_risk_profile(account, proxy=proxy, proxy_account_count=proxy_account_count)
    modules = [
        "warmup_validation",
        "warmup_safe",
        "bulk_join_chats",
        "bulk_clone_profile",
        "bulk_copy_stories",
        "bulk_ai_profiles",
        "neurocommenting",
        "neurochatting",
        "mass_reactions",
        "auto_join",
    ]
    return {
        "account_id": account.id,
        "risk_profile": profile.to_dict(),
        "module_eligibility": {
            module: get_module_eligibility(
                account,
                module,
                proxy=proxy,
                proxy_account_count=proxy_account_count,
            ).to_dict()
            for module in modules
        },
    }


@router.get("/{account_id}/trust", response_model=AccountTrustSnapshotResponse)
async def account_trust(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountTrustSnapshotResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    snapshot = await latest_account_trust_snapshot(session, workspace_id, account.id)
    if snapshot is None:
        snapshot = await recompute_account_trust(session, account, reason="api_initial_trust")
    return snapshot


@router.post("/{account_id}/trust/recompute", response_model=AccountTrustSnapshotResponse)
async def recompute_trust(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> AccountTrustSnapshotResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    return await recompute_account_trust(session, account, reason="manual_recompute")


@router.get("/{account_id}/trust/history", response_model=list[AccountTrustSnapshotResponse])
async def account_trust_history_endpoint(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list:
    await _get_workspace_account(session, workspace_id, account_id)
    return await account_trust_history(session, workspace_id, account_id, limit=limit)


@router.post("/{account_id}/technical-check", response_model=AccountTechnicalCheckResponse)
async def technical_check_account(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> dict:
    account = await _get_workspace_account(session, workspace_id, account_id)
    checked = await check_account(session, account, check_restrictions=False)
    snapshot = await recompute_account_trust(session, checked, reason="technical_check")
    return _technical_check_response(checked, snapshot)


@router.post("/technical-check-selected", response_model=list[AccountTechnicalCheckResponse])
async def technical_check_selected(
    payload: AccountBulkStatusRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> list[dict]:
    accounts = await _get_workspace_accounts(session, workspace_id, payload.account_ids)
    responses: list[dict] = []
    for account in accounts:
        checked = await check_account(session, account, check_restrictions=False)
        snapshot = await recompute_account_trust(session, checked, reason="technical_check_selected")
        responses.append(_technical_check_response(checked, snapshot))
    return responses


@router.post("/{account_id}/quality-checks", response_model=AccountQualityCheckResponse, status_code=201)
async def create_quality_check(
    account_id: str,
    payload: AccountQualityCheckCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> dict:
    account = await _get_workspace_account(session, workspace_id, account_id)
    check = AccountQualityCheck(
        account_id=account.id,
        provider=payload.provider,
        rating=payload.rating,
        potential_30d=payload.potential_30d,
        label=payload.label,
        potential_label=payload.potential_label,
        raw=payload.raw,
    )
    data = dict(account.daily_limits or {})
    data["quality_score"] = {
        "provider": payload.provider,
        "rating": payload.rating,
        "potential_30d": payload.potential_30d,
        "label": payload.label,
        "potential_label": payload.potential_label,
        "raw": payload.raw,
    }
    account.daily_limits = data
    session.add(check)
    await session.commit()
    await session.refresh(check)
    data["quality_score"]["checked_at"] = check.created_at.isoformat()
    account.daily_limits = data
    await session.commit()
    return {
        "id": check.id,
        "account_id": check.account_id,
        "provider": check.provider,
        "rating": check.rating,
        "potential_30d": check.potential_30d,
        "label": check.label,
        "potential_label": check.potential_label,
        "raw": check.raw,
        "checked_at": check.created_at,
    }


@router.patch("/{account_id}", response_model=TelegramAccountResponse)
async def update_account(
    account_id: str,
    payload: TelegramAccountPatch,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccount:
    account = await _get_workspace_account(session, workspace_id, account_id)
    data = payload.model_dump(exclude_unset=True)
    if "username" in data:
        account.username = data["username"]
    if "display_name" in data:
        account.display_name = data["display_name"]
    if "proxy_id" in data:
        if data["proxy_id"]:
            proxy = await session.get(Proxy, data["proxy_id"])
            if not proxy or proxy.workspace_id != workspace_id or proxy.status == "deleted":
                raise not_found("Proxy not found")
            await ensure_proxy_account_capacity(session, workspace_id, proxy, exclude_account_id=account.id)
        account.proxy_id = data["proxy_id"]
    if "proxy_required" in data:
        account.proxy_required = bool(data["proxy_required"])
    if "status" in data and data["status"] in {
        "new",
        "active",
        "warming",
        "incubator",
        "frozen",
        "quarantined",
        "archived",
        "deleted",
    }:
        account.status = data["status"]
    if "warmup_score" in data and data["warmup_score"] is not None:
        account.warmup_score = data["warmup_score"]
    if "warmup_status" in data and data["warmup_status"] is not None:
        account.warmup_status = data["warmup_status"]
    metadata_updates = {
        key: data[key]
        for key in ["origin", "claimed_age_days", "real_age_days", "incubator_mode", "incubator_until_at"]
        if key in data
    }
    if metadata_updates:
        _update_account_metadata(account, metadata_updates)
    await session.commit()
    await session.refresh(account)
    return account


@router.post("/{account_id}/archive", response_model=TelegramAccountResponse)
async def archive_account(account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> TelegramAccount:
    account = await _get_workspace_account(session, workspace_id, account_id)
    account.status = "archived"
    await session.commit()
    await session.refresh(account)
    return account


@router.post("/{account_id}/quarantine", response_model=TelegramAccountResponse)
async def quarantine_account(account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> TelegramAccount:
    account = await _get_workspace_account(session, workspace_id, account_id)
    account.status = "quarantined"
    if account.proxy_id:
        proxy = await session.get(Proxy, account.proxy_id)
        if proxy and proxy.workspace_id == workspace_id and proxy.status != "deleted":
            proxy.status = "tainted"
            proxy.last_error_code = "account_manually_quarantined"
            proxy.last_error_message = "Аккаунт на этом прокси вручную отправлен в карантин."
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type="account_quarantined",
            severity="warning",
            message="Аккаунт вручную отправлен в карантин. Массовые действия и кампании должны его пропускать.",
            raw_error={"reason": "manual_quarantine"},
        )
    )
    await session.commit()
    await session.refresh(account)
    return account


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> None:
    account = await _get_workspace_account(session, workspace_id, account_id)
    account.status = "deleted"
    await session.commit()


@router.get("/{account_id}/profile", response_model=TelegramAccountProfileResponse)
async def profile(account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> TelegramAccountProfileResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    return await get_account_profile(session, account)


@router.patch("/{account_id}/profile", response_model=TelegramAccountProfileResponse)
async def update_profile(
    account_id: str,
    payload: TelegramAccountProfilePatch,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccountProfileResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    await _ensure_module_allowed_for_accounts(session, workspace_id, [account], "profile_edit")
    return await update_account_profile(session, account, payload)


@router.post("/{account_id}/profile/queue", response_model=TelegramAccountProfileResponse, status_code=202)
async def queue_profile_update(
    account_id: str,
    payload: TelegramAccountProfilePatch,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccountProfileResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    request_id = str(uuid4())
    profile = await queue_account_profile_update(session, account, payload, request_id=request_id)
    celery_app.send_task(
        "app.workers.tasks.update_account_profile",
        kwargs={
            "account_id": account.id,
            "payload_data": payload.model_dump(mode="json"),
            "request_id": request_id,
        },
    )
    return profile


@router.post("/{account_id}/clone-profile", response_model=TelegramAccountCloneProfileResponse)
async def clone_profile(
    account_id: str,
    payload: TelegramAccountCloneProfileRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccountCloneProfileResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    source_account = None
    if payload.source_account_id:
        source_account = await _get_workspace_account(session, workspace_id, payload.source_account_id)
    return await clone_account_profile(session, account, payload, source_account=source_account)


@router.post("/{account_id}/profile-photo")
async def update_profile_photo(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    photo: Annotated[UploadFile, File(...)],
) -> dict[str, str]:
    account = await _get_workspace_account(session, workspace_id, account_id)
    return await update_account_profile_photo(session, account, photo)


@router.get("/{account_id}/profile/photos", response_model=TelegramAccountProfilePhotosResponse)
async def profile_photos(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccountProfilePhotosResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    return await list_account_profile_photos(session, account)


@router.delete("/{account_id}/profile/photos/{photo_id}", response_model=TelegramAccountProfilePhotosResponse)
async def delete_profile_photo(
    account_id: str,
    photo_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccountProfilePhotosResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    await _ensure_module_allowed_for_accounts(session, workspace_id, [account], "profile_edit")
    return await delete_account_profile_photo(session, account, photo_id=photo_id)


@router.post("/{account_id}/login-code", response_model=TelegramAccountLoginCodeResponse)
async def login_code(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccountLoginCodeResponse:
    account = await _get_workspace_account(session, workspace_id, account_id)
    return await request_account_login_code(session, account)


@router.post("/{account_id}/2fa")
async def update_2fa(
    account_id: str,
    payload: TelegramAccountTwoFaUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> dict[str, str]:
    account = await _get_workspace_account(session, workspace_id, account_id)
    return await update_account_2fa(session, account, payload)


@router.get("/{account_id}/health-events", response_model=list[AccountHealthEventResponse])
async def health_events(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> list[AccountHealthEvent]:
    await _get_workspace_account(session, workspace_id, account_id)
    result = await session.execute(
        select(AccountHealthEvent)
        .where(AccountHealthEvent.account_id == account_id)
        .order_by(AccountHealthEvent.created_at.desc())
    )
    return list(result.scalars().all())


async def _account_response_with_trust(
    session: SessionDep,
    workspace_id: str,
    account: TelegramAccount,
) -> dict:
    payload = TelegramAccountResponse.model_validate(account).model_dump()
    payload["latest_trust"] = await ensure_account_trust_snapshot(session, account, reason="account_list")
    from app.modules.telegram_safety.views import apply_safety_projection

    return await apply_safety_projection(session, account, payload)


def _update_account_metadata(account: TelegramAccount, updates: dict) -> None:
    limits = dict(account.daily_limits or {})
    metadata = dict(limits.get("account_metadata") or {})
    for key, value in updates.items():
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    limits["account_metadata"] = metadata
    account.daily_limits = limits


def _immortal_candidate_score(account: TelegramAccount, proxy: Proxy | None) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    age_days = account.real_age_days if account.real_age_days is not None else account.system_age_days
    if age_days >= 90:
        score += 35
        reasons.append("Возраст 90+ дней")
    elif age_days >= 60:
        score += 28
        reasons.append("Возраст 60+ дней")
    elif age_days >= 30:
        score += 20
        reasons.append("Возраст 30+ дней")

    if account.origin in {"manual_phone_registered", "sms_service_manual"}:
        score += 25
        reasons.append("Ручная история регистрации")
    elif account.origin in {"market_session", "tdata"}:
        score += 8
        reasons.append("Источник известен")

    if account.restriction_status == "clear":
        score += 15
        reasons.append("Нет runtime-ограничений")
    elif account.restriction_status in {"limited", "blocked", "frozen"}:
        return 0, ["Есть Telegram-ограничение"]

    if proxy and proxy.status in {"active", "new"}:
        score += 15
        reasons.append("Прокси чистый/активный")
    elif account.proxy_id:
        score += 5
        reasons.append("Прокси назначен, но требует проверки")

    if account.status in {"active", "warming"}:
        score += 10
    if account.warmup_score >= 80:
        score += 10
        reasons.append("Прогрев 80+")
    if account.incubator_mode:
        score -= 25
        reasons.append("Еще в инкубаторе")
    return max(0, min(100, score)), reasons


def _technical_check_response(
    account: TelegramAccount,
    snapshot: AccountTrustSnapshot,
) -> dict:
    recommendations = snapshot.recommendations or {}
    factors = [
        *(snapshot.positive_factors or [])[:4],
        *(snapshot.risk_factors or [])[:6],
    ]
    technical_status = "ok"
    if snapshot.safety_status in {"technical_only", "invalid"}:
        technical_status = "failed"
    elif account.status in {"failed", "frozen"}:
        technical_status = "warning"
    return {
        "account_id": account.id,
        "technical_status": technical_status,
        "safety_status": snapshot.safety_status,
        "trust_score": snapshot.score_100,
        "trust_label": snapshot.label,
        "confidence": snapshot.confidence,
        "next_best_action": recommendations.get("next_best_action", "review_account"),
        "factors": factors,
        "snapshot": snapshot,
    }


async def _get_workspace_account(session: SessionDep, workspace_id: str, account_id: str) -> TelegramAccount:
    account = await session.get(TelegramAccount, account_id)
    if not account or account.workspace_id != workspace_id:
        raise not_found("Telegram account not found")
    return account


async def _get_workspace_accounts(
    session: SessionDep,
    workspace_id: str,
    account_ids: list[str],
) -> list[TelegramAccount]:
    unique_ids = list(dict.fromkeys(account_ids))
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(unique_ids),
            TelegramAccount.status.not_in(["archived", "deleted"]),
        )
    )
    accounts = list(result.scalars().all())
    if len(accounts) != len(unique_ids):
        raise not_found("One or more Telegram accounts not found")
    return accounts


def _account_bulk_warnings(accounts: list[TelegramAccount]) -> list[str]:
    warnings: list[str] = []
    invalid = [
        account.display_name or account.username or account.id[:8]
        for account in accounts
        if account.status not in {"active", "warming", "ready"}
    ]
    frozen = [
        account.display_name or account.username or account.id[:8] for account in accounts if account.status == "frozen"
    ]
    no_proxy = [
        account.display_name or account.username or account.id[:8] for account in accounts if not account.proxy_id
    ]
    if frozen:
        warnings.append("Есть замороженные Telegram-аккаунты: " + ", ".join(frozen[:5]))
    if invalid:
        warnings.append("Есть аккаунты не в статусе valid/active: " + ", ".join(invalid[:5]))
    if no_proxy:
        warnings.append("Есть аккаунты без привязанного прокси: " + ", ".join(no_proxy[:5]))
    return warnings


def _ensure_safe_proxy_distribution(
    accounts: list[TelegramAccount],
    *,
    action_name: str,
    risky_fields: list[bool] | None = None,
) -> None:
    if len(accounts) <= 1:
        return
    if risky_fields is not None and not any(risky_fields):
        return

    by_network: dict[str, list[str]] = {}
    for account in accounts:
        network_key = account.proxy_id or "__direct_backend_ip__"
        by_network.setdefault(network_key, []).append(account.display_name or account.username or account.id[:8])

    collisions = [names for names in by_network.values() if len(names) > 1]
    if not collisions:
        return

    examples = ", ".join(collisions[0][:4])
    raise bad_request(
        f"Нельзя запускать {action_name} на нескольких аккаунтах с одним прокси или без прокси. "
        f"Риск спамблока слишком высокий. Разбейте запуск по одному аккаунту на прокси. Пример группы: {examples}."
    )


async def _ensure_module_allowed_for_accounts(
    session: SessionDep,
    workspace_id: str,
    accounts: list[TelegramAccount],
    module: str,
) -> None:
    blocked: list[str] = []
    decisions: list[tuple[TelegramAccount, object]] = []
    for account in accounts:
        eligibility = get_module_eligibility(account, module)
        decisions.append((account, eligibility))
        if eligibility.status == "blocked":
            label = account.display_name or account.username or account.id[:8]
            blocked.append(f"{label}: {eligibility.message or eligibility.reason or 'blocked'}")
    await _record_risk_decisions(session, workspace_id, module, decisions)
    if blocked:
        await session.commit()
        raise bad_request("Модуль заблокирован для части аккаунтов. " + "; ".join(blocked[:5]))


async def _record_risk_decisions(
    session: SessionDep,
    workspace_id: str,
    module: str,
    decisions: list[tuple[TelegramAccount, object]],
) -> None:
    for account, eligibility in decisions:
        risk_profile = getattr(eligibility, "risk_profile", None)
        reasons = list(getattr(risk_profile, "reasons", []) or [])
        reason = getattr(eligibility, "reason", None)
        if reason:
            reasons.insert(0, reason)
        session.add(
            RiskDecision(
                workspace_id=workspace_id,
                account_id=account.id,
                module=module,
                decision=getattr(eligibility, "status", "blocked"),
                reasons=list(dict.fromkeys(reasons)),
                limits=getattr(eligibility, "allowed_limits", None),
            )
        )


async def _workspace_active_accounts(session: SessionDep, workspace_id: str) -> list[TelegramAccount]:
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.status.not_in(["archived", "deleted"]),
        )
    )
    return list(result.scalars().all())


async def _proxy_context_for_accounts(
    session: SessionDep,
    workspace_id: str,
    accounts: list[TelegramAccount],
) -> tuple[dict[str, Proxy], dict[str, int]]:
    proxy_ids = sorted({account.proxy_id for account in accounts if account.proxy_id})
    if not proxy_ids:
        return {}, {}
    proxies_result = await session.execute(
        select(Proxy).where(
            Proxy.workspace_id == workspace_id,
            Proxy.id.in_(proxy_ids),
            Proxy.status != "deleted",
        )
    )
    proxy_by_id = {proxy.id: proxy for proxy in proxies_result.scalars().all()}
    proxy_counts = {
        proxy_id: await active_proxy_account_count(session, workspace_id, proxy_id) for proxy_id in proxy_ids
    }
    return proxy_by_id, proxy_counts


def _build_import_batch_response(
    batch_id: str,
    accounts: list[TelegramAccount],
    proxy_by_id: dict[str, Proxy],
) -> AccountImportBatchResponse:
    restriction_counts: dict[str, int] = {}
    for account in accounts:
        restriction = account.restriction_status or "unknown"
        restriction_counts[restriction] = restriction_counts.get(restriction, 0) + 1
    invalid_statuses = {"banned", "blocked", "deleted", "frozen", "invalid", "quarantined", "spam_blocked"}
    invalid_count = sum(1 for account in accounts if account.status in invalid_statuses)
    active_count = sum(1 for account in accounts if account.status in {"active", "ready", "warming"})
    limited_count = restriction_counts.get("limited", 0)
    blocked_count = restriction_counts.get("blocked", 0)
    frozen_count = restriction_counts.get("frozen", 0)
    account_count = len(accounts)
    spam_block_rate = (limited_count + blocked_count + frozen_count) / account_count if account_count else 0
    survival_rate = active_count / account_count if account_count else 0
    proxy_providers = sorted(
        {
            proxy.provider
            for account in accounts
            if account.proxy_id
            for proxy in [proxy_by_id.get(account.proxy_id)]
            if proxy and proxy.provider
        }
    )
    imported_at = next((account.import_batch_at for account in accounts if account.import_batch_at), None)
    declared_count = next((account.import_batch_size for account in accounts if account.import_batch_size), None)
    cohort = _import_batch_cohort_metadata(accounts)
    return AccountImportBatchResponse(
        batch_id=batch_id,
        imported_at=imported_at,
        declared_count=declared_count,
        seller=cohort.get("seller"),
        purchase_date=cohort.get("purchase_date"),
        declared_geo=cohort.get("declared_geo"),
        declared_age=cohort.get("declared_age"),
        notes=cohort.get("notes"),
        status=cohort.get("status"),
        account_count=account_count,
        clear_count=restriction_counts.get("clear", 0),
        limited_count=limited_count,
        blocked_count=blocked_count,
        unknown_count=restriction_counts.get("unknown", 0),
        invalid_count=invalid_count,
        quarantined_count=sum(1 for account in accounts if account.status == "quarantined"),
        active_count=active_count,
        spam_block_rate=round(spam_block_rate, 4),
        survival_rate=round(survival_rate, 4),
        proxy_providers=proxy_providers,
        verdict=_import_batch_verdict(spam_block_rate, survival_rate, account_count),
    )


def _import_batch_cohort_metadata(accounts: list[TelegramAccount]) -> dict[str, str | None]:
    for account in accounts:
        data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
        session_import = data.get("session_import")
        if not isinstance(session_import, dict):
            continue
        cohort = session_import.get("cohort")
        if isinstance(cohort, dict):
            return {
                key: str(value) if value is not None else None
                for key, value in cohort.items()
                if key in {"seller", "purchase_date", "declared_geo", "declared_age", "notes", "status"}
            }
    return {}


def _import_batch_verdict(spam_block_rate: float, survival_rate: float, account_count: int) -> str:
    if account_count <= 1:
        return "single_account"
    if spam_block_rate >= 0.5 or survival_rate < 0.5:
        return "toxic_do_not_scale"
    if spam_block_rate >= 0.2 or survival_rate < 0.8:
        return "risky_test_only"
    return "healthy_candidate"


async def _record_bulk_action(
    session: SessionDep,
    accounts: list[TelegramAccount],
    event_type: str,
    message: str,
    raw: dict,
    severity: str = "info",
) -> None:
    await record_bulk_action_for_account_ids(
        session,
        [account.id for account in accounts],
        event_type=event_type,
        message=message,
        raw=raw,
        severity=severity,
    )
