from app.modules.telegram_gateway.service import TelegramGatewayError
from fastapi import APIRouter, Query, Response
from sqlalchemy import case, select

from app.api.deps import SessionDep, WorkspaceIdDep
from app.api.entitlements import require_entitlement
from app.core.errors import bad_request, not_found
from app.modules.parsing.models import ParsingJob, ParsingJobLog, ParsingResult
from app.modules.parsing.schemas import (
    ChannelSearchRequest,
    ChannelSearchResponse,
    ChannelValidateRequest,
    GroupSearchRequest,
    GroupSearchResponse,
    ParseCommentsRequest,
    ParseCommentsResponse,
    ParseDialogsRequest,
    ParseDialogsResponse,
    ParseMessageUsersRequest,
    ParseMessageUsersResponse,
    ParseSourceRequest,
    ParseSourceResponse,
    ParseUsersRequest,
    ParseUsersResponse,
    ParsingJobCreateRequest,
    ParsingJobDetailResponse,
    ParsingJobResponse,
    ParsingResultResponse,
)
from app.modules.parsing.service import (
    add_parsing_job_log,
    channel_search_mixed,
    complete_parsing_job,
    create_mock_posts,
    create_parsing_job,
    export_parsing_results,
    fail_parsing_job,
    fetch_real_posts,
    group_search_mixed,
    parse_account_dialogs,
    parse_message_users,
    parse_source_comments,
    parse_source_users,
    persist_parsing_results,
    source_ref_for_source,
    start_parsing_job,
    validate_channel_sources,
)
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount, TelegramSession

router = APIRouter()


@router.post("/jobs", response_model=ParsingJobResponse, status_code=201)
async def create_job(
    payload: ParsingJobCreateRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ParsingJob:
    account = (
        await _resolve_account(session, workspace_id, payload.telegram_account_id)
        if payload.telegram_account_id
        else None
    )
    return await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type=payload.type,
        account=account,
        input_payload=payload.input,
        filters=payload.filters,
        limits=payload.limits,
    )


@router.get("/jobs", response_model=list[ParsingJobResponse])
async def list_jobs(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    job_type: str | None = Query(default=None, alias="type"),
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ParsingJob]:
    statement = select(ParsingJob).where(ParsingJob.workspace_id == workspace_id)
    if job_type:
        statement = statement.where(ParsingJob.type == job_type)
    if status:
        statement = statement.where(ParsingJob.status == status)
    result = await session.execute(statement.order_by(ParsingJob.created_at.desc()).limit(limit))
    return list(result.scalars().all())


@router.get("/jobs/{job_id}", response_model=ParsingJobDetailResponse)
async def get_job(job_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> dict:
    job = await _get_job(session, workspace_id, job_id)
    logs_result = await session.execute(
        select(ParsingJobLog).where(ParsingJobLog.job_id == job.id).order_by(ParsingJobLog.created_at.asc())
    )
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "type": job.type,
        "status": job.status,
        "account_id": job.account_id,
        "input": job.input,
        "filters": job.filters,
        "limits": job.limits,
        "progress_total": job.progress_total,
        "progress_done": job.progress_done,
        "found_count": job.found_count,
        "saved_count": job.saved_count,
        "skipped_count": job.skipped_count,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "logs": list(logs_result.scalars().all()),
    }


@router.post("/jobs/{job_id}/cancel", response_model=ParsingJobResponse)
async def cancel_job(job_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> ParsingJob:
    job = await _get_job(session, workspace_id, job_id)
    if job.status in {"completed", "failed", "cancelled"}:
        return job
    job.status = "cancelled"
    await add_parsing_job_log(session, job, "warning", "Задача отменена пользователем")
    await session.commit()
    await session.refresh(job)
    return job


@router.get("/jobs/{job_id}/results", response_model=list[ParsingResultResponse])
async def job_results(
    job_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    entity_type: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[ParsingResult]:
    job = await _get_job(session, workspace_id, job_id)
    statement = select(ParsingResult).where(ParsingResult.workspace_id == workspace_id, ParsingResult.job_id == job.id)
    if entity_type:
        statement = statement.where(ParsingResult.entity_type == entity_type)
    result = await session.execute(statement.order_by(ParsingResult.created_at.asc()).limit(limit))
    return list(result.scalars().all())


@router.get("/jobs/{job_id}/export")
async def export_job_results(
    job_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    format: str = Query(default="csv", pattern="^(csv|json|txt)$"),
) -> Response:
    job = await _get_job(session, workspace_id, job_id)
    media_type, filename, content = await export_parsing_results(session, job, export_format=format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/jobs/mock-source",
    response_model=ParseSourceResponse,
    status_code=201,
    dependencies=[require_entitlement("module.parsing.posts")],
)
async def mock_parse_source(
    payload: ParseSourceRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ParseSourceResponse:
    source = await session.get(TelegramSource, payload.source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")

    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="source_posts",
        input_payload={"source_id": source.id, "mode": "mock"},
        limits={"limit": payload.limit},
        progress_total=payload.limit,
    )
    await start_parsing_job(session, job)
    messages = await create_mock_posts(
        session=session,
        workspace_id=workspace_id,
        source=source,
        limit=payload.limit,
        seed_text=payload.seed_text,
    )
    await persist_parsing_results(
        session,
        job=job,
        entity_type="message",
        source_id=source.id,
        items=[_message_result_payload(message) for message in messages],
    )
    await complete_parsing_job(session, job, found_count=len(messages))
    return ParseSourceResponse(job_id=job.id, source_id=source.id, created=len(messages), messages=messages)


@router.post(
    "/jobs/source",
    response_model=ParseSourceResponse,
    status_code=201,
    dependencies=[require_entitlement("module.parsing.posts")],
)
async def parse_source(
    payload: ParseSourceRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ParseSourceResponse:
    source = await session.get(TelegramSource, payload.source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")

    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="source_posts",
        account=account,
        input_payload={"source_id": source.id, "source_ref": source_ref_for_source(source)},
        filters={"ensure_membership": payload.ensure_membership, "join_discussion": payload.join_discussion},
        limits={"limit": payload.limit},
        progress_total=payload.limit,
    )
    await start_parsing_job(session, job)
    try:
        messages = await fetch_real_posts(
            session=session,
            workspace_id=workspace_id,
            source=source,
            account=account,
            limit=payload.limit,
            ensure_membership=payload.ensure_membership,
            join_discussion=payload.join_discussion,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc

    await persist_parsing_results(
        session,
        job=job,
        entity_type="message",
        source_id=source.id,
        items=[_message_result_payload(message) for message in messages],
    )
    await complete_parsing_job(session, job, found_count=len(messages))
    return ParseSourceResponse(job_id=job.id, source_id=source.id, created=len(messages), messages=messages)


@router.post(
    "/dialogs",
    response_model=ParseDialogsResponse,
    dependencies=[require_entitlement("module.parsing.dialogs")],
)
async def parse_dialogs(
    payload: ParseDialogsRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ParseDialogsResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="dialogs",
        account=account,
        filters={
            "entity_type": payload.entity_type,
            "keywords": payload.keywords,
            "min_participants": payload.min_participants,
            "max_participants": payload.max_participants,
            "active_days": payload.active_days,
        },
        limits={"limit": payload.limit, **_delay_limits(payload)},
        progress_total=payload.limit,
    )
    await start_parsing_job(session, job)
    try:
        items = await parse_account_dialogs(
            session,
            account,
            entity_type=payload.entity_type,
            limit=payload.limit,
            keywords=payload.keywords,
            min_participants=payload.min_participants,
            max_participants=payload.max_participants,
            active_days=payload.active_days,
            delay_seconds_min=payload.delay_seconds_min,
            delay_seconds_max=payload.delay_seconds_max,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc
    await persist_parsing_results(session, job=job, entity_type="source", items=items)
    await complete_parsing_job(session, job, found_count=len(items))
    return ParseDialogsResponse(
        job_id=job.id,
        account_id=account.id,
        entity_type=payload.entity_type,
        count=len(items),
        items=items,
    )


@router.post("/users", response_model=ParseUsersResponse, dependencies=[require_entitlement("module.parsing.users")])
@router.post(
    "/source-users",
    response_model=ParseUsersResponse,
    dependencies=[require_entitlement("module.parsing.users")],
)
async def parse_users(
    payload: ParseUsersRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ParseUsersResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    source_ref = await _resolve_source_ref(session, workspace_id, payload.source_id, payload.source_ref)
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="source_users",
        account=account,
        input_payload={"source_id": payload.source_id, "source_ref": source_ref},
        filters={
            "min_comments": payload.min_comments,
            "min_messages": payload.min_messages,
            "username_required": payload.username_required,
            "active_days": payload.active_days,
            "keywords": payload.keywords,
        },
        limits={"limit": payload.limit, **_delay_limits(payload)},
        progress_total=payload.limit,
    )
    await start_parsing_job(session, job)
    try:
        users = await parse_source_users(
            session,
            workspace_id,
            account,
            source_ref,
            limit=payload.limit,
            min_comments=payload.min_comments,
            min_messages=payload.min_messages,
            username_required=payload.username_required,
            active_days=payload.active_days,
            keywords=payload.keywords,
            delay_seconds_min=payload.delay_seconds_min,
            delay_seconds_max=payload.delay_seconds_max,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc
    await persist_parsing_results(session, job=job, entity_type="user", items=users, source_id=payload.source_id)
    await complete_parsing_job(session, job, found_count=len(users))
    return ParseUsersResponse(
        job_id=job.id,
        account_id=account.id,
        source_ref=source_ref,
        count=len(users),
        users=users,
    )


@router.post(
    "/comments",
    response_model=ParseCommentsResponse,
    dependencies=[require_entitlement("module.parsing.comments")],
)
@router.post(
    "/source-comments",
    response_model=ParseCommentsResponse,
    dependencies=[require_entitlement("module.parsing.comments")],
)
async def parse_comments(
    payload: ParseCommentsRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ParseCommentsResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    source_ref = await _resolve_source_ref(session, workspace_id, payload.source_id, payload.source_ref)
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="source_comments",
        account=account,
        input_payload={"source_id": payload.source_id, "source_ref": source_ref},
        filters={"keywords": payload.keywords, "date_from": payload.date_from, "date_to": payload.date_to},
        limits={
            "post_limit": payload.post_limit,
            "comments_per_post": payload.comments_per_post,
            **_delay_limits(payload),
        },
        progress_total=payload.post_limit * payload.comments_per_post,
    )
    await start_parsing_job(session, job)
    try:
        comments = await parse_source_comments(
            session,
            account,
            source_ref,
            post_tg_message_id=payload.post_tg_message_id,
            post_limit=payload.post_limit,
            comments_per_post=payload.comments_per_post,
            keywords=payload.keywords,
            date_from=payload.date_from,
            date_to=payload.date_to,
            workspace_id=workspace_id,
            source_id=payload.source_id,
            job_id=job.id,
            ensure_membership=payload.ensure_membership,
            delay_seconds_min=payload.delay_seconds_min,
            delay_seconds_max=payload.delay_seconds_max,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc
    await persist_parsing_results(session, job=job, entity_type="comment", items=comments, source_id=payload.source_id)
    await complete_parsing_job(session, job, found_count=len(comments))
    return ParseCommentsResponse(
        job_id=job.id,
        account_id=account.id,
        source_ref=source_ref,
        count=len(comments),
        comments=comments,
    )


@router.post(
    "/message-users",
    response_model=ParseMessageUsersResponse,
    dependencies=[require_entitlement("module.parsing.users")],
)
async def message_users(
    payload: ParseMessageUsersRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ParseMessageUsersResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    source_ref = await _resolve_source_ref(session, workspace_id, payload.source_id, payload.source_ref)
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="message_users",
        account=account,
        input_payload={"source_id": payload.source_id, "source_ref": source_ref},
        filters={
            "keywords": payload.keywords,
            "keywords_mode": payload.keywords_mode,
            "date_from": payload.date_from,
            "date_to": payload.date_to,
            "min_messages": payload.min_messages,
            "username_required": payload.username_required,
        },
        limits={
            "message_limit": payload.message_limit,
            "user_limit": payload.user_limit,
            **_delay_limits(payload),
        },
        progress_total=payload.message_limit,
    )
    await start_parsing_job(session, job)
    try:
        users = await parse_message_users(
            session,
            workspace_id,
            account,
            source_ref,
            keywords=payload.keywords,
            keywords_mode=payload.keywords_mode,
            date_from=payload.date_from,
            date_to=payload.date_to,
            message_limit=payload.message_limit,
            user_limit=payload.user_limit,
            min_messages=payload.min_messages,
            username_required=payload.username_required,
            delay_seconds_min=payload.delay_seconds_min,
            delay_seconds_max=payload.delay_seconds_max,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc
    await persist_parsing_results(session, job=job, entity_type="user", items=users, source_id=payload.source_id)
    await complete_parsing_job(session, job, found_count=len(users), progress_done=payload.message_limit)
    return ParseMessageUsersResponse(
        job_id=job.id,
        account_id=account.id,
        source_ref=source_ref,
        count=len(users),
        users=users,
    )


@router.post(
    "/channel-search",
    response_model=ChannelSearchResponse,
    dependencies=[require_entitlement("module.parsing.dialogs")],
)
async def channel_search(
    payload: ChannelSearchRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ChannelSearchResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    comments_mode = "open" if payload.comments_required else payload.comments_mode
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="channel_search",
        account=account,
        input_payload={"keywords": payload.keywords, "source": "mixed"},
        filters={**payload.model_dump(exclude={"limit"}), "source": "mixed", "comments_mode": comments_mode},
        limits={"limit": payload.limit, **_delay_limits(payload)},
        progress_total=payload.limit,
    )
    await start_parsing_job(session, job)
    try:
        items = await channel_search_mixed(
            session,
            workspace_id,
            account,
            keywords=payload.keywords,
            keyword_suffixes=payload.keyword_suffixes,
            category_id=payload.category_id,
            subcategory_id=payload.subcategory_id,
            min_participants=payload.min_participants,
            max_participants=payload.max_participants,
            active_days=payload.active_days,
            language_code=payload.language_code,
            country_code=payload.country_code,
            comments_required=payload.comments_required,
            comments_mode=comments_mode,
            min_engagement_score=payload.min_engagement_score,
            limit=payload.limit,
            delay_seconds_min=payload.delay_seconds_min,
            delay_seconds_max=payload.delay_seconds_max,
            job=job,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc
    return ChannelSearchResponse(job_id=job.id, count=len(items), items=items)


@router.post(
    "/channel-validate",
    response_model=ChannelSearchResponse,
    dependencies=[require_entitlement("module.parsing.dialogs")],
)
async def channel_validate(
    payload: ChannelValidateRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ChannelSearchResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    comments_required = payload.comments_required or payload.comments_mode == "open"
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="channel_validate",
        account=account,
        input_payload={"source_ids": payload.source_ids, "refs": payload.refs},
        filters={
            "comments_required": comments_required,
            "comments_mode": "open" if comments_required else payload.comments_mode,
            "min_participants": payload.min_participants,
            "active_days": payload.active_days,
        },
        limits={"limit": payload.limit},
        progress_total=payload.limit,
    )
    await start_parsing_job(session, job)
    try:
        items = await validate_channel_sources(
            session,
            workspace_id,
            account,
            source_ids=payload.source_ids,
            refs=payload.refs,
            comments_required=comments_required,
            min_participants=payload.min_participants,
            active_days=payload.active_days,
            limit=payload.limit,
            job=job,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc
    return ChannelSearchResponse(job_id=job.id, count=len(items), items=items)


@router.post(
    "/group-search",
    response_model=GroupSearchResponse,
    dependencies=[require_entitlement("module.parsing.dialogs")],
)
async def group_search(
    payload: GroupSearchRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> GroupSearchResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    job = await create_parsing_job(
        session,
        workspace_id=workspace_id,
        job_type="group_search",
        account=account,
        input_payload={"source": "mixed", "keywords": payload.keywords},
        filters={
            "min_participants": payload.min_participants,
            "max_participants": payload.max_participants,
            "active_days": payload.active_days,
            "keyword_suffixes": payload.keyword_suffixes,
            "min_activity_score": payload.min_activity_score,
            "access_type": payload.access_type,
            "ai_suggestions_enabled": payload.ai_suggestions_enabled,
        },
        limits={"limit": payload.limit, **_delay_limits(payload)},
        progress_total=payload.limit,
    )
    await start_parsing_job(session, job)
    try:
        items = await group_search_mixed(
            session,
            account,
            keywords=payload.keywords,
            keyword_suffixes=payload.keyword_suffixes,
            min_participants=payload.min_participants,
            max_participants=payload.max_participants,
            active_days=payload.active_days,
            min_activity_score=payload.min_activity_score,
            access_type=payload.access_type,
            limit=payload.limit,
            delay_seconds_min=payload.delay_seconds_min,
            delay_seconds_max=payload.delay_seconds_max,
            job=job,
        )
    except Exception as exc:
        await fail_parsing_job(session, job, exc, _friendly_parsing_error(exc))
        raise bad_request(_friendly_parsing_error(exc)) from exc
    return GroupSearchResponse(job_id=job.id, count=len(items), items=items)


async def _resolve_account(
    session: SessionDep,
    workspace_id: str,
    account_id: str | None,
) -> TelegramAccount:
    if account_id:
        account = await session.get(TelegramAccount, account_id)
        if not account or account.workspace_id != workspace_id:
            raise not_found("Telegram account not found")
        return account

    status_rank = case(
        (TelegramAccount.status == "active", 0),
        (TelegramAccount.status == "warming", 1),
        (TelegramAccount.status == "new", 2),
        else_=9,
    )
    result = await session.execute(
        select(TelegramAccount)
        .join(TelegramSession, TelegramSession.account_id == TelegramAccount.id)
        .where(TelegramAccount.workspace_id == workspace_id, TelegramAccount.status.in_(["active", "warming", "new"]))
        .order_by(status_rank, TelegramAccount.health_score.desc(), TelegramAccount.created_at.asc())
        .limit(1)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise bad_request("Telegram account is required for real parsing")
    return account


async def _get_job(session: SessionDep, workspace_id: str, job_id: str) -> ParsingJob:
    job = await session.get(ParsingJob, job_id)
    if not job or job.workspace_id != workspace_id:
        raise not_found("Parsing job not found")
    return job


async def _resolve_source_ref(
    session: SessionDep,
    workspace_id: str,
    source_id: str | None,
    source_ref: str | None,
) -> str:
    if source_id:
        source = await session.get(TelegramSource, source_id)
        if not source or source.workspace_id != workspace_id:
            raise not_found("Source not found")
        return source_ref_for_source(source)
    if source_ref and source_ref.strip():
        return source_ref.strip()
    raise bad_request("Source is required")


def _delay_limits(payload: object) -> dict[str, float]:
    delay_min = getattr(payload, "delay_seconds_min", None)
    delay_max = getattr(payload, "delay_seconds_max", None)
    result: dict[str, float] = {}
    if delay_min is not None:
        result["delay_seconds_min"] = float(delay_min)
    if delay_max is not None:
        result["delay_seconds_max"] = float(delay_max)
    return result


def _friendly_parsing_error(error: Exception) -> str:
    code = error.code if isinstance(error, TelegramGatewayError) else type(error).__name__
    message = str(error)
    text = f"{code} {message}".lower()
    seconds = getattr(error, "raw", {}).get("seconds") if isinstance(error, TelegramGatewayError) else None

    if code in {"SESSION_INVALID", "SESSION_EMPTY", "SESSION_NOT_FOUND"}:
        return "Сессия Telegram аккаунта повреждена или не найдена. Переподключите аккаунт или выберите другой."
    if code == "SESSION_UNAUTHORIZED":
        return "Telegram сессия больше не авторизована. Нужно заново подключить аккаунт."
    if code == "SESSION_2FA_REQUIRED":
        return "Для аккаунта требуется пароль 2FA. Переподключите аккаунт с паролем."
    if code in {"PROXY_NOT_FOUND"} or "proxy" in text:
        return "Прокси аккаунта недоступен. Проверьте прокси или выберите другой аккаунт."
    if code == "FLOOD_WAIT" or "flood" in text or "wait of" in text:
        suffix = f" Подождите примерно {seconds} сек." if seconds else ""
        return f"Telegram временно ограничил частые действия аккаунта.{suffix}"
    if "chatadminrequired" in text or "getparticipantsrequest" in text or "admin privileges" in text:
        return (
            "Telegram не дает получить полный список участников без прав администратора. "
            "Для таких каналов используем сбор активной аудитории из комментариев и сообщений."
        )
    if "channelprivate" in text or "usernotparticipant" in text or "not a participant" in text:
        return "У аккаунта нет доступа к этому источнику. Проверьте доступ или выберите другой аккаунт."
    if "no user has" in text or "could not find" in text or "cannot find" in text:
        return "Источник не найден в Telegram. Проверьте username или ссылку."
    if "source is required" in text:
        return "Выберите источник или вставьте ссылку/username."
    if "telegram account" in text and "required" in text:
        return "Выберите рабочий Telegram аккаунт для парсинга."
    return "Не удалось выполнить парсинг. Проверьте аккаунт, источник и доступ к нему."


def _message_result_payload(message: object) -> dict:
    return {
        "id": getattr(message, "id", None),
        "source_id": getattr(message, "source_id", None),
        "tg_message_id": getattr(message, "tg_message_id", None),
        "text": getattr(message, "text", None),
        "published_at": getattr(message, "published_at", None),
        "views_count": getattr(message, "views_count", None),
        "forwards_count": getattr(message, "forwards_count", None),
        "replies_count": getattr(message, "replies_count", None),
    }
