# ruff: noqa: E501
import csv
from collections.abc import Iterable
from datetime import UTC, datetime
from io import BytesIO, StringIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from app.modules.telegram_gateway.service import (
    TelegramGatewayError,
    real_telegram_enabled,
    send_comment_with_account,
    source_ref_from_fields,
)
from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.errors import bad_request, not_found
from app.modules.ai.models import AiProfile, CommentPromptTemplate
from app.modules.ai.service import AiProviderError
from app.modules.campaigns.invite_tracking import (
    get_campaign_invite_tracking_metric,
    invite_tracking_config_dict,
    invite_tracking_response,
    invite_tracking_summary,
    refresh_campaign_invite_tracking_metric,
    upsert_campaign_invite_tracking_metric,
)
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignEvent, CampaignInviteLinkMetric, CampaignRun
from app.modules.campaigns.schemas import (
    CampaignAccountReplacementRequest,
    CampaignActionResponse,
    CampaignCommentReport,
    CampaignDryRunResponse,
    CampaignEventResponse,
    CampaignInviteTrackingResponse,
    CampaignReactionReport,
    CampaignResponse,
    CampaignRunResponse,
    CampaignRuntimeLogResponse,
    CampaignSafetyResponse,
    CommentPostsCampaignCreate,
    CommentPostsCampaignUpdate,
    MassReactionsCampaignCreate,
    MessageBroadcastCampaignCreate,
    NeuroChatCampaignCreate,
    NeuroDialogCampaignCreate,
)
from app.modules.campaigns.service import (
    build_campaign_comment_report,
    build_campaign_reaction_report,
    request_stop_campaign_run,
    start_comment_posts_campaign,
    start_comment_posts_campaign_run,
    start_generic_campaign_run,
    tick_generic_campaign_run,
)
from app.modules.campaigns.service import (
    campaign_comment_report_csv as render_campaign_comment_report_csv,
)
from app.modules.campaigns.service import (
    campaign_reaction_report_csv as render_campaign_reaction_report_csv,
)
from app.modules.campaigns.service import (
    tick_active_campaign_runs as tick_active_campaign_runs_service,
)
from app.modules.comments.models import TelegramComment
from app.modules.operations.service import record_operational_event
from app.modules.proxies.models import Proxy
from app.modules.sources.models import TelegramMessage, TelegramSource, TelegramSourceFolder
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility

router = APIRouter()


async def _record_campaign_created(session: SessionDep, campaign: Campaign) -> None:
    await record_operational_event(
        session,
        workspace_id=campaign.workspace_id,
        severity="info",
        category="product",
        event_type="campaign_created",
        title="Создана кампания",
        message=f"Создана черновая кампания: {campaign.name}.",
        source="campaigns.api",
        entity_type="campaign",
        entity_id=campaign.id,
        payload={"campaign_type": campaign.type, "status": campaign.status},
    )


MASS_REACTION_RUNTIME_CONFIG_KEYS = {
    "source_ids",
    "account_ids",
    "reactions",
    "target_scope",
    "reaction_weights",
    "comments_per_post",
    "messages_per_source",
    "source_assignment_mode",
    "source_assignment",
    "post_selection_mode",
    "latest_posts_per_source",
    "reaction_probability",
}
MASS_REACTION_RUNTIME_POLICY_KEYS = {
    "daily_limit_per_account",
    "delay_seconds_min",
    "delay_seconds_max",
    "reaction_rotation",
}


async def _reset_mass_reaction_runtime_state(
    session: SessionDep,
    campaign: Campaign,
    *,
    reset_cursors: bool,
) -> None:
    await session.execute(
        delete(CampaignAction).where(
            CampaignAction.campaign_id == campaign.id,
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status == "queued",
        )
    )
    config = {**_json_dict(campaign.config)}
    if reset_cursors:
        config.pop("source_cursors", None)
    config["reaction_rotation_index"] = 0
    campaign.config = config


def _xlsx_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_text_cell(reference: str, value: str) -> str:
    escaped = escape(value)
    space_attr = ' xml:space="preserve"' if value != value.strip() else ""
    return f'<c r="{reference}" t="inlineStr"><is><t{space_attr}>{escaped}</t></is></c>'


def _render_xlsx_from_rows(rows: list[list[str]]) -> bytes:
    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            reference = f"{_xlsx_column_name(column_index)}{row_index}"
            cells.append(_xlsx_text_cell(reference, value))
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    column_count = max((len(row) for row in rows), default=1)
    row_count = max(len(rows), 1)
    dimension = f"A1:{_xlsx_column_name(column_count)}{row_count}"
    column_widths = [24, 13, 12, 14, 42, 18, 72, 72, 28]
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(column_widths[:column_count], start=1)
    )
    worksheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="{dimension}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{columns}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
</worksheet>"""

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Комментарии" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def _render_campaign_comment_report_xlsx(report: CampaignCommentReport) -> bytes:
    csv_text = render_campaign_comment_report_csv(report).lstrip("\ufeff")
    rows = list(csv.reader(StringIO(csv_text)))
    return _render_xlsx_from_rows(rows)


def _render_campaigns_comment_report_xlsx(reports: list[CampaignCommentReport]) -> bytes:
    rows = [
        [
            "Кампания",
            "Канал",
            "Дата",
            "Время",
            "Статус",
            "Ссылка на пост",
            "ID поста",
            "Текст поста",
            "Комментарий",
            "Аккаунт",
        ]
    ]
    for report in reports:
        csv_rows = list(csv.reader(StringIO(render_campaign_comment_report_csv(report).lstrip("\ufeff"))))
        for row in csv_rows[1:]:
            rows.append([report.campaign_name, *row])
    return _render_xlsx_from_rows(rows)


def _campaign_folder_name(campaign: Campaign) -> str:
    config = campaign.config if isinstance(campaign.config, dict) else {}
    folder = str(config.get("folder") or "").strip()
    return folder or "Основная"


def _comment_ai_profile_system_prompt(tone: str) -> str:
    return " ".join(
        [
            "Пиши короткие человеческие комментарии на русском для Telegram.",
            f"Манера подачи: {tone}.",
            "Каждый комментарий должен цепляться за конкретную мысль поста и добавлять новую реплику: вывод, вопрос, нюанс, практическое наблюдение или мягкое несогласие.",
            "Не пересказывай пост, не продавай в лоб, не используй AI-глянец, канцелярит, шаблонные похвалы, универсальные фразы и длинные вступления.",
            "Если выбран вопросительный стиль, задавай один точный вопрос по сути. Если выбран утвердительный стиль, формулируй короткий самостоятельный вывод.",
            "Комментарий должен выглядеть как живой ответ реального человека в обсуждении.",
        ]
    )


DEFAULT_COMMENT_STOP_WORDS = [
    "реклама",
    "рекламный пост",
    "рекламная интеграция",
    "промокод",
    "скидка",
    "акция",
    "розыгрыш",
    "giveaway",
    "конкурс",
    "вебинар",
    "марафон",
    "оффер",
    "купить",
    "продам",
    "прайс",
    "рассрочка",
]
DEFAULT_COMMENT_MIN_POST_CHARS = 200
DEFAULT_COMMENT_MIN_MEDIA_POST_CHARS = 20


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[CampaignResponse]:
    sent_action_types = [
        "generate_and_send_comment",
        "neuro_chat_reply",
        "neuro_dialog_reply",
        "mass_reaction",
        "message_broadcast",
        "send_reaction",
        "set_reaction",
    ]
    result = await session.execute(
        select(Campaign, func.count(CampaignAction.id).label("sent_count"))
        .outerjoin(
            CampaignAction,
            (CampaignAction.campaign_id == Campaign.id)
            & (CampaignAction.status.in_(["sent", "succeeded"]))
            & (CampaignAction.action_type.in_(sent_action_types)),
        )
        .where(Campaign.workspace_id == workspace_id, Campaign.status != "deleted")
        .group_by(Campaign.id)
        .order_by(Campaign.created_at.desc())
    )
    rows = result.all()
    campaign_ids = [campaign.id for campaign, _sent_count in rows]
    active_run_status_by_campaign_id: dict[str, str] = {}
    latest_run_by_campaign_id: dict[str, CampaignRun] = {}
    if campaign_ids:
        active_runs = await session.execute(
            select(CampaignRun.campaign_id, CampaignRun.status)
            .where(
                CampaignRun.workspace_id == workspace_id,
                CampaignRun.campaign_id.in_(campaign_ids),
                CampaignRun.status.in_(["pending", "running", "stopping"]),
            )
            .order_by(CampaignRun.created_at.desc())
        )
        for campaign_id, run_status in active_runs.all():
            active_run_status_by_campaign_id.setdefault(campaign_id, run_status)
        latest_runs = await session.execute(
            select(CampaignRun)
            .where(
                CampaignRun.workspace_id == workspace_id,
                CampaignRun.campaign_id.in_(campaign_ids),
            )
            .order_by(CampaignRun.campaign_id, CampaignRun.created_at.desc())
        )
        for run in latest_runs.scalars().all():
            latest_run_by_campaign_id.setdefault(run.campaign_id, run)
    invite_tracking_by_campaign_id: dict[str, dict] = {}
    if campaign_ids:
        metrics = await session.execute(
            select(CampaignInviteLinkMetric).where(
                CampaignInviteLinkMetric.workspace_id == workspace_id,
                CampaignInviteLinkMetric.campaign_id.in_(campaign_ids),
            )
        )
        invite_tracking_by_campaign_id = {
            metric.campaign_id: summary
            for metric in metrics.scalars().all()
            if (summary := invite_tracking_summary(metric)) is not None
        }
    prompt_ids = {
        str(prompt_id)
        for campaign, _sent_count in rows
        if (prompt_id := _json_dict(campaign.config).get("comment_prompt_template_id"))
    }
    prompts_by_id: dict[str, CommentPromptTemplate] = {}
    if prompt_ids:
        prompt_rows = await session.execute(
            select(CommentPromptTemplate).where(
                CommentPromptTemplate.workspace_id == workspace_id,
                CommentPromptTemplate.id.in_(prompt_ids),
            )
        )
        prompts_by_id = {prompt.id: prompt for prompt in prompt_rows.scalars().all()}

    responses: list[CampaignResponse] = []
    for campaign, sent_count in rows:
        config = _json_dict(campaign.config)
        prompt_id = str(config.get("comment_prompt_template_id") or "")
        prompt = prompts_by_id.get(prompt_id)
        if prompt:
            config = {
                **config,
                "prompt_text": prompt.prompt_text,
                "product_context": prompt.product_context,
                "prompt_name": prompt.name,
            }
        latest_run = latest_run_by_campaign_id.get(campaign.id)
        responses.append(
            CampaignResponse.model_validate(campaign).model_copy(
                update={
                    "config": config,
                    "sent_count": int(sent_count or 0),
                    "active_run_status": active_run_status_by_campaign_id.get(campaign.id),
                    "last_run_status": latest_run.status if latest_run else None,
                    "last_error_code": latest_run.error_code if latest_run else None,
                    "last_error_message": latest_run.error_message if latest_run else None,
                    "invite_tracking": invite_tracking_by_campaign_id.get(campaign.id),
                }
            )
        )
    return responses


@router.post("/comment-posts", response_model=CampaignResponse, status_code=201)
async def create_comment_posts_campaign(
    payload: CommentPostsCampaignCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> Campaign:
    source_ids = await _resolve_campaign_source_ids(
        session, workspace_id, payload.source_ids, payload.source_folder_ids
    )
    prompt = await session.get(CommentPromptTemplate, payload.comment_prompt_template_id)
    if not prompt or prompt.workspace_id != workspace_id:
        raise not_found("Comment prompt not found")

    sources = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(source_ids),
        )
    )
    source_list = list(sources.scalars().all())
    if len(source_list) != len(set(source_ids)):
        raise not_found("One or more sources not found")
    _ensure_comment_sources(source_list)
    await _ensure_sources_not_used_by_other_campaigns(
        session,
        workspace_id,
        "comment_posts",
        source_ids,
    )

    accounts = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(payload.account_ids),
        )
    )
    account_list = list(accounts.scalars().all())
    if len(account_list) != len(set(payload.account_ids)):
        raise not_found("One or more accounts not found")
    _ensure_accounts_allowed(account_list, _module_for_campaign_type("comment_posts"))

    policy = _apply_comment_campaign_safety_policy(
        _comment_posts_policy_with_defaults(payload.policy),
        account_list,
    )
    campaign = Campaign(
        workspace_id=workspace_id,
        type="comment_posts",
        name=payload.name,
        status="draft",
        created_by=current_user.id,
        config={
            "source_ids": source_ids,
            "source_folder_ids": payload.source_folder_ids,
            "account_ids": payload.account_ids,
            **_source_assignment_config(payload.source_assignment_mode, payload.account_ids, source_ids),
            "comment_prompt_template_id": payload.comment_prompt_template_id,
            "author_gender": payload.author_gender,
            "latest_posts_per_source": payload.latest_posts_per_source,
            "post_selection_mode": payload.post_selection_mode,
            "delivery_mode": payload.delivery_mode,
            "mode": payload.mode or _legacy_campaign_mode(payload.post_selection_mode, payload.delivery_mode),
            "stop_conditions": payload.stop_conditions.model_dump(mode="json", exclude_none=True),
            "folder": (payload.folder or "").strip(),
            "invite_tracking": invite_tracking_config_dict(payload.invite_tracking),
        },
        policy=policy,
    )
    session.add(campaign)
    await session.flush()
    await upsert_campaign_invite_tracking_metric(session, workspace_id, campaign, payload.invite_tracking)
    await _record_campaign_created(session, campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.post("/neuro-chat", response_model=CampaignResponse, status_code=201)
async def create_neuro_chat_campaign(
    payload: NeuroChatCampaignCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> Campaign:
    source_ids = await _resolve_campaign_source_ids(
        session, workspace_id, payload.source_ids, payload.source_folder_ids
    )
    sources = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(source_ids),
        )
    )
    source_list = list(sources.scalars().all())
    if len(source_list) != len(set(source_ids)):
        raise not_found("One or more sources not found")
    _ensure_sources_type(source_list, {"group"}, "AI-чат можно запускать только на источники типа группа")
    await _ensure_sources_not_used_by_other_campaigns(
        session,
        workspace_id,
        "neuro_chat",
        source_ids,
    )
    accounts = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(payload.account_ids),
        )
    )
    account_list = list(accounts.scalars().all())
    if len(account_list) != len(set(payload.account_ids)):
        raise not_found("One or more accounts not found")
    _ensure_accounts_allowed(account_list, _module_for_campaign_type("neuro_chat"))

    campaign = Campaign(
        workspace_id=workspace_id,
        type="neuro_chat",
        name=payload.name,
        status="draft",
        created_by=current_user.id,
        config={
            "source_ids": source_ids,
            "source_folder_ids": payload.source_folder_ids,
            "account_ids": payload.account_ids,
            **_source_assignment_config(payload.source_assignment_mode, payload.account_ids, source_ids),
            "folder": (payload.folder or "").strip(),
            "prompt_text": payload.prompt_text,
            "author_gender": payload.author_gender,
            "tone": payload.tone,
            "delivery_mode": payload.delivery_mode,
            "trigger_keywords": _clean_trigger_keywords(payload.trigger_keywords),
            "keyword_mode": payload.keyword_mode,
            "messages_per_source": payload.messages_per_source,
            "max_replies_per_tick": payload.max_replies_per_tick,
            "min_quality_score": payload.min_quality_score,
            "reply_to_only": payload.reply_to_only,
            "continuous": True,
            "stop_conditions": payload.stop_conditions.model_dump(mode="json", exclude_none=True),
            "invite_tracking": invite_tracking_config_dict(payload.invite_tracking),
        },
        policy={"moderation": "manual_review", **payload.policy},
    )
    session.add(campaign)
    await session.flush()
    await upsert_campaign_invite_tracking_metric(session, workspace_id, campaign, payload.invite_tracking)
    await _record_campaign_created(session, campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.post("/neuro-dialogs", response_model=CampaignResponse, status_code=201)
async def create_neuro_dialog_campaign(
    payload: NeuroDialogCampaignCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> Campaign:
    source_ids = await _resolve_campaign_source_ids(
        session, workspace_id, payload.source_ids, payload.source_folder_ids
    )
    target_refs = [ref for ref in payload.target_refs if ref.strip()]
    if not source_ids and not target_refs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose at least one dialog source",
        )
    if source_ids:
        sources = await session.execute(
            select(TelegramSource).where(
                TelegramSource.workspace_id == workspace_id,
                TelegramSource.id.in_(source_ids),
            )
        )
        source_list = list(sources.scalars().all())
        if len(source_list) != len(set(source_ids)):
            raise not_found("One or more sources not found")
        _ensure_sources_type(source_list, {"dialog"}, "AI диалоги можно запускать только на источники типа диалог")
        await _ensure_sources_not_used_by_other_campaigns(
            session,
            workspace_id,
            "neuro_dialog",
            source_ids,
        )
        target_refs.extend(source_ref_from_fields(source.username, source.url, source.tg_id) for source in source_list)

    accounts = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(payload.account_ids),
        )
    )
    account_list = list(accounts.scalars().all())
    if len(account_list) != len(set(payload.account_ids)):
        raise not_found("One or more accounts not found")
    _ensure_accounts_allowed(account_list, _module_for_campaign_type("neuro_dialog"))

    campaign = Campaign(
        workspace_id=workspace_id,
        type="neuro_dialog",
        name=payload.name,
        status="draft",
        created_by=current_user.id,
        config={
            "source_ids": source_ids,
            "source_folder_ids": payload.source_folder_ids,
            "target_refs": target_refs,
            "account_ids": payload.account_ids,
            **_source_assignment_config(payload.source_assignment_mode, payload.account_ids, source_ids),
            "folder": (payload.folder or "").strip(),
            "prompt_text": payload.prompt_text,
            "author_gender": payload.author_gender,
            "tone": payload.tone,
            "delivery_mode": payload.delivery_mode,
            "messages_per_dialog": payload.messages_per_dialog,
            "max_replies_per_tick": payload.max_replies_per_tick,
            "min_quality_score": payload.min_quality_score,
            "only_reply_to_incoming": payload.only_reply_to_incoming,
            "continuous": True,
            "stop_conditions": payload.stop_conditions.model_dump(mode="json", exclude_none=True),
            "invite_tracking": invite_tracking_config_dict(payload.invite_tracking),
        },
        policy={
            **payload.policy,
            "moderation": str(payload.policy.get("moderation") or "manual_review"),
            "delay_seconds_min": payload.delay_seconds_min,
            "delay_seconds_max": payload.delay_seconds_max,
        },
    )
    session.add(campaign)
    await session.flush()
    await upsert_campaign_invite_tracking_metric(session, workspace_id, campaign, payload.invite_tracking)
    await _record_campaign_created(session, campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.post("/mass-reactions", response_model=CampaignResponse, status_code=201)
async def create_mass_reactions_campaign(
    payload: MassReactionsCampaignCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> Campaign:
    source_ids = await _resolve_campaign_source_ids(
        session, workspace_id, payload.source_ids, payload.source_folder_ids
    )
    sources = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(source_ids),
        )
    )
    source_list = list(sources.scalars().all())
    if len(source_list) != len(set(source_ids)):
        raise not_found("One or more sources not found")
    _ensure_sources_type(
        source_list,
        {"channel", "group"},
        "AI реакции можно запускать только на источники типа канал или группа",
    )
    await _ensure_sources_not_used_by_other_campaigns(
        session,
        workspace_id,
        "mass_reactions",
        source_ids,
    )

    accounts = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(payload.account_ids),
        )
    )
    account_list = list(accounts.scalars().all())
    if len(account_list) != len(set(payload.account_ids)):
        raise not_found("One or more accounts not found")
    _ensure_accounts_allowed(account_list, _module_for_campaign_type("mass_reactions"))

    campaign = Campaign(
        workspace_id=workspace_id,
        type="mass_reactions",
        name=payload.name,
        status="draft",
        created_by=current_user.id,
        config={
            "source_ids": source_ids,
            "source_folder_ids": payload.source_folder_ids,
            "account_ids": payload.account_ids,
            **_source_assignment_config(payload.source_assignment_mode, payload.account_ids, source_ids),
            "folder": (payload.folder or "").strip(),
            "reactions": payload.reactions,
            "target_scope": payload.target_scope,
            "reaction_weights": payload.reaction_weights,
            "comments_per_post": payload.comments_per_post,
            "messages_per_source": payload.messages_per_source,
            "react_as": payload.react_as,
            "post_selection_mode": payload.post_selection_mode,
            "latest_posts_per_source": payload.latest_posts_per_source,
            "reaction_probability": payload.reaction_probability,
            "stop_conditions": {
                "max_reactions": payload.max_reactions,
                "max_comments": payload.max_reactions,
                "run_for_minutes": payload.run_for_minutes,
                "manual_stop": True,
            },
            "continuous": payload.post_selection_mode == "new_posts",
            "invite_tracking": invite_tracking_config_dict(payload.invite_tracking),
        },
        policy={
            **payload.policy,
            "delay_seconds_min": payload.delay_seconds_min,
            "delay_seconds_max": payload.delay_seconds_max,
            "reaction_rotation": payload.reaction_mode,
        },
    )
    session.add(campaign)
    await session.flush()
    await upsert_campaign_invite_tracking_metric(session, workspace_id, campaign, payload.invite_tracking)
    await _record_campaign_created(session, campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.post("/message-broadcasts", response_model=CampaignResponse, status_code=201)
async def create_message_broadcast_campaign(
    payload: MessageBroadcastCampaignCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> Campaign:
    source_ids = await _resolve_campaign_source_ids(
        session, workspace_id, payload.source_ids, payload.source_folder_ids
    )
    sources = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(source_ids),
        )
    )
    source_list = list(sources.scalars().all())
    if len(source_list) != len(set(source_ids)):
        raise not_found("One or more sources not found")
    _ensure_sources_type(source_list, {"dialog"}, "Рассылку можно запускать только на источники типа диалог")
    await _ensure_sources_not_used_by_other_campaigns(
        session,
        workspace_id,
        "message_broadcast",
        source_ids,
    )

    accounts = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(payload.account_ids),
        )
    )
    account_list = list(accounts.scalars().all())
    if len(account_list) != len(set(payload.account_ids)):
        raise not_found("One or more accounts not found")
    _ensure_accounts_allowed(account_list, _module_for_campaign_type("message_broadcast"))

    campaign = Campaign(
        workspace_id=workspace_id,
        type="message_broadcast",
        name=payload.name,
        status="draft",
        created_by=current_user.id,
        config={
            "source_ids": source_ids,
            "source_folder_ids": payload.source_folder_ids,
            "target_refs": [
                source_ref_from_fields(source.username, source.url, source.tg_id) for source in source_list
            ],
            "account_ids": payload.account_ids,
            **_source_assignment_config(payload.source_assignment_mode, payload.account_ids, source_ids),
            "folder": (payload.folder or "").strip(),
            "message_text": payload.message_text,
            "author_gender": payload.author_gender,
            "delivery_mode": "generate_and_send",
            "continuous": False,
            "stop_conditions": {
                "max_messages": payload.max_messages,
                "max_comments": payload.max_messages,
                "run_for_minutes": payload.run_for_minutes,
                "manual_stop": True,
            },
        },
        policy={
            "daily_limit_per_account": payload.daily_limit_per_account,
            "delay_seconds_min": payload.delay_seconds_min,
            "delay_seconds_max": payload.delay_seconds_max,
            "rotation": "round_robin",
            **payload.policy,
        },
    )
    session.add(campaign)
    await session.flush()
    await _record_campaign_created(session, campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.get("/{campaign_id}/account-replacement")
async def get_account_replacement(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    return campaign.config.get("account_replacement", {})


@router.post("/{campaign_id}/cancel-account-replacement", response_model=CampaignResponse)
async def cancel_campaign_account_replacement(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> Campaign:
    from app.modules.campaigns.account_replacement import cancel_replacement

    campaign = await _get_campaign(session, workspace_id, campaign_id)
    return await cancel_replacement(session, campaign, current_user.id)


@router.get("/{campaign_id}/replacement-accounts")
async def get_replacement_accounts(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    from app.modules.campaigns.account_replacement import replacement_accounts

    campaign = await _get_campaign(session, workspace_id, campaign_id)
    return await replacement_accounts(session, campaign)


@router.post("/{campaign_id}/replace-account", response_model=CampaignResponse)
async def replace_campaign_account(
    campaign_id: str,
    payload: CampaignAccountReplacementRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> Campaign:
    from app.modules.campaigns.account_replacement import request_replacement

    campaign = await _get_campaign(session, workspace_id, campaign_id)
    return await request_replacement(session, campaign, payload, current_user.id)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: str,
    payload: CommentPostsCampaignUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> Campaign:
    campaign = await _get_campaign(session, workspace_id, campaign_id)

    if campaign.config.get("recipient_mode") == "saved_contacts":
        raise bad_request("Для изменения рассылки по контактам создайте новый предпросмотр и черновик.")

    data = payload.model_dump(exclude_unset=True)
    if campaign.type == "comment_posts":
        from app.modules.campaigns.account_replacement import replacement_pending

        if replacement_pending(campaign):
            raise HTTPException(409, "Сначала завершите подготовку источников после замены аккаунта.")
        if "account_ids" in data and data["account_ids"] != campaign.config.get("account_ids", []):
            raise HTTPException(
                409, "Для смены аккаунта используйте меню «Заменить аккаунт»: источники будут подготовлены с задержкой."
            )
    if campaign.type != "comment_posts":
        if campaign.type == "mass_reactions":
            data.pop("daily_limit_per_account", None)
            if isinstance(data.get("policy"), dict):
                data["policy"] = {
                    key: value for key, value in data["policy"].items() if key != "daily_limit_per_account"
                }
        previous_config = _json_dict(campaign.config)
        previous_policy = _json_dict(campaign.policy)
        if "name" in data:
            campaign.name = data["name"]
        allowed_config_keys = {
            "source_ids",
            "source_folder_ids",
            "source_assignment_mode",
            "source_assignment",
            "folder",
            "account_ids",
            "delivery_mode",
            "stop_conditions",
            "prompt_text",
            "author_gender",
            "tone",
            "trigger_keywords",
            "keyword_mode",
            "messages_per_source",
            "messages_per_dialog",
            "max_replies_per_tick",
            "min_quality_score",
            "reply_to_only",
            "only_reply_to_incoming",
            "reactions",
            "target_scope",
            "reaction_weights",
            "comments_per_post",
            "react_as",
            "post_selection_mode",
            "latest_posts_per_source",
            "reaction_probability",
            "message_text",
            "invite_tracking",
        }
        config_updates = {key: value for key, value in data.items() if key in allowed_config_keys}
        if "invite_tracking" in config_updates:
            config_updates["invite_tracking"] = invite_tracking_config_dict(config_updates["invite_tracking"])
        if "trigger_keywords" in config_updates:
            config_updates["trigger_keywords"] = _clean_trigger_keywords(config_updates["trigger_keywords"])
        if "source_ids" in config_updates or "source_folder_ids" in config_updates:
            current = _json_dict(campaign.config)
            source_ids = await _resolve_campaign_source_ids(
                session,
                workspace_id,
                config_updates.get("source_ids", current.get("source_ids", [])),
                config_updates.get("source_folder_ids", current.get("source_folder_ids", [])),
            )
            config_updates["source_ids"] = source_ids
            sources = await session.execute(
                select(TelegramSource).where(
                    TelegramSource.workspace_id == workspace_id,
                    TelegramSource.id.in_(source_ids),
                )
            )
            source_list = list(sources.scalars().all())
            if len(source_list) != len(set(source_ids)):
                raise not_found("One or more sources not found")
            if campaign.type == "neuro_chat":
                _ensure_sources_type(source_list, {"group"}, "AI-чат можно запускать только на источники типа группа")
            elif campaign.type == "neuro_dialog":
                _ensure_sources_type(
                    source_list, {"dialog"}, "AI диалоги можно запускать только на источники типа диалог"
                )
            elif campaign.type == "mass_reactions":
                _ensure_sources_type(
                    source_list,
                    {"channel", "group"},
                    "AI реакции можно запускать только на источники типа канал или группа",
                )
            elif campaign.type == "message_broadcast":
                _ensure_sources_type(
                    source_list, {"dialog"}, "Рассылку можно запускать только на источники типа диалог"
                )
            await _ensure_sources_not_used_by_other_campaigns(
                session,
                workspace_id,
                campaign.type,
                source_ids,
                current_campaign_id=campaign.id,
            )
            if campaign.type == "neuro_dialog":
                config_updates["target_refs"] = [
                    source_ref_from_fields(source.username, source.url, source.tg_id) for source in source_list
                ]
        if (
            "source_assignment_mode" in config_updates
            or "source_assignment" in config_updates
            or "source_ids" in config_updates
            or "account_ids" in config_updates
        ):
            next_config = {**_json_dict(campaign.config), **config_updates}
            config_updates.update(
                _source_assignment_config(
                    str(next_config.get("source_assignment_mode") or "standard"),
                    [str(item) for item in next_config.get("account_ids", []) if isinstance(item, str)],
                    [str(item) for item in next_config.get("source_ids", []) if isinstance(item, str)],
                    _json_dict(next_config.get("source_assignment")),
                )
            )
            if campaign.type == "message_broadcast":
                if "source_list" not in locals():
                    sources = await session.execute(
                        select(TelegramSource).where(
                            TelegramSource.workspace_id == workspace_id,
                            TelegramSource.id.in_(
                                [str(item) for item in next_config.get("source_ids", []) if isinstance(item, str)]
                            ),
                        )
                    )
                    source_list = list(sources.scalars().all())
                config_updates["target_refs"] = [
                    source_ref_from_fields(source.username, source.url, source.tg_id) for source in source_list
                ]
        if "max_messages" in data:
            stop_conditions = {
                **_json_dict(campaign.config).get("stop_conditions", {}),
                "max_messages": data["max_messages"],
                "max_comments": data["max_messages"],
                "manual_stop": True,
            }
            config_updates["stop_conditions"] = stop_conditions
        if "max_reactions" in data:
            stop_conditions = {
                **_json_dict(campaign.config).get("stop_conditions", {}),
                "max_reactions": data["max_reactions"],
                "max_comments": data["max_reactions"],
                "manual_stop": True,
            }
            config_updates["stop_conditions"] = stop_conditions
        if "run_for_minutes" in data:
            stop_conditions = {
                **_json_dict(campaign.config).get("stop_conditions", {}),
                "run_for_minutes": data["run_for_minutes"],
                "manual_stop": True,
            }
            config_updates["stop_conditions"] = stop_conditions
        policy_updates = {}
        for key in ["daily_limit_per_account", "delay_seconds_min", "delay_seconds_max"]:
            if key in data:
                policy_updates[key] = data[key]
        if "reaction_mode" in data:
            policy_updates["reaction_rotation"] = data["reaction_mode"]
        if "account_ids" in config_updates:
            accounts = await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.workspace_id == workspace_id,
                    TelegramAccount.id.in_(config_updates["account_ids"]),
                )
            )
            account_list = list(accounts.scalars().all())
            if len(account_list) != len(set(config_updates["account_ids"])):
                raise not_found("One or more accounts not found")
            _ensure_accounts_allowed(account_list, _module_for_campaign_type(campaign.type))
        if "policy" in data:
            campaign.policy = data["policy"] or {}
        if policy_updates:
            campaign.policy = {**_json_dict(campaign.policy), **policy_updates}
        if config_updates:
            campaign.config = {**_json_dict(campaign.config), **config_updates}
        if "invite_tracking" in data:
            await upsert_campaign_invite_tracking_metric(
                session, workspace_id, campaign, config_updates.get("invite_tracking")
            )
        if campaign.type == "mass_reactions":
            next_config = _json_dict(campaign.config)
            next_policy = _json_dict(campaign.policy)
            runtime_config_changed = any(
                key in config_updates and previous_config.get(key) != next_config.get(key)
                for key in MASS_REACTION_RUNTIME_CONFIG_KEYS
            )
            runtime_policy_changed = any(
                previous_policy.get(key) != next_policy.get(key) for key in MASS_REACTION_RUNTIME_POLICY_KEYS
            )
            if runtime_config_changed or runtime_policy_changed:
                reset_cursors = any(
                    key in config_updates and previous_config.get(key) != next_config.get(key)
                    for key in {
                        "source_ids",
                        "target_scope",
                        "post_selection_mode",
                        "latest_posts_per_source",
                        "comments_per_post",
                        "messages_per_source",
                    }
                )
                await _reset_mass_reaction_runtime_state(
                    session,
                    campaign,
                    reset_cursors=reset_cursors,
                )
        await session.commit()
        await session.refresh(campaign)
        return campaign

    current_config = _json_dict(campaign.config)
    runtime_changed = _comment_campaign_runtime_changed(campaign, data)
    if "source_ids" in data or "source_folder_ids" in data:
        data["source_ids"] = await _resolve_campaign_source_ids(
            session,
            workspace_id,
            data.get("source_ids", current_config.get("source_ids", [])),
            data.get("source_folder_ids", current_config.get("source_folder_ids", [])),
        )
    if "source_assignment_mode" in data or "source_assignment" in data or "source_ids" in data or "account_ids" in data:
        next_config_preview = {**current_config, **data}
        data.update(
            _source_assignment_config(
                str(next_config_preview.get("source_assignment_mode") or "standard"),
                [str(item) for item in next_config_preview.get("account_ids", []) if isinstance(item, str)],
                [str(item) for item in next_config_preview.get("source_ids", []) if isinstance(item, str)],
                _json_dict(next_config_preview.get("source_assignment")),
            )
        )
    source_ids_changed = "source_ids" in data and data["source_ids"] != current_config.get("source_ids")
    should_restart_after_update = (
        runtime_changed and campaign.status in {"pending", "running"} and not campaign.config.get("controlled_launch")
    )

    prompt: CommentPromptTemplate | None = None
    prompt_id = data.get("comment_prompt_template_id") or _json_dict(campaign.config).get("comment_prompt_template_id")
    if prompt_id:
        prompt = await session.get(CommentPromptTemplate, prompt_id)
        if not prompt or prompt.workspace_id != workspace_id:
            raise not_found("Comment prompt not found")

    if "source_ids" in data:
        sources = await session.execute(
            select(TelegramSource).where(
                TelegramSource.workspace_id == workspace_id,
                TelegramSource.id.in_(data["source_ids"]),
            )
        )
        source_list = list(sources.scalars().all())
        if len(source_list) != len(set(data["source_ids"])):
            raise not_found("One or more sources not found")
        if source_ids_changed:
            _ensure_comment_sources(source_list)
            await _ensure_sources_not_used_by_other_campaigns(
                session,
                workspace_id,
                "comment_posts",
                data["source_ids"],
                current_campaign_id=campaign.id,
            )

    if "account_ids" in data:
        accounts = await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.workspace_id == workspace_id,
                TelegramAccount.id.in_(data["account_ids"]),
            )
        )
        account_list = list(accounts.scalars().all())
        if len(account_list) != len(set(data["account_ids"])):
            raise not_found("One or more accounts not found")
        if data["account_ids"] != campaign.config.get("account_ids", []):
            _ensure_accounts_allowed(account_list, _module_for_campaign_type("comment_posts"))

    if "name" in data:
        campaign.name = data["name"]
    if "policy" in data:
        campaign.policy = data["policy"] or {}
    if "prompt_tone" in data and data["prompt_tone"]:
        prompt_tone = str(data["prompt_tone"])
        campaign.policy = {**(campaign.policy or {}), "prompt_tone": prompt_tone}
        if prompt and prompt.ai_profile_id:
            profile = await session.get(AiProfile, prompt.ai_profile_id)
            if profile and profile.workspace_id == workspace_id:
                profile.tone = prompt_tone
                profile.system_prompt = _comment_ai_profile_system_prompt(prompt_tone)
    if prompt:
        if "prompt_text" in data and data["prompt_text"]:
            prompt.prompt_text = data["prompt_text"]
        if "prompt_name" in data and data["prompt_name"]:
            prompt.name = data["prompt_name"]
        if "product_context" in data:
            prompt.product_context = data["product_context"]
        if "max_length" in data:
            prompt.max_length = data["max_length"]
            campaign.policy = {**_json_dict(campaign.policy), "max_length": data["max_length"]}

    if should_restart_after_update:
        await _stop_active_campaign_runs(
            session,
            workspace_id,
            campaign,
            reason="config_updated",
            event_type="run_restarted_after_config_update",
            event_message="Campaign settings changed; active run was cancelled before restart.",
            campaign_status="pending",
        )

    config = dict(campaign.config or {})
    for key in [
        "folder",
        "source_ids",
        "source_folder_ids",
        "source_assignment_mode",
        "source_assignment",
        "sources_per_account",
        "account_ids",
        "comment_prompt_template_id",
        "author_gender",
        "latest_posts_per_source",
        "post_selection_mode",
        "delivery_mode",
        "mode",
        "stop_conditions",
        "invite_tracking",
    ]:
        if key in data:
            config[key] = invite_tracking_config_dict(data[key]) if key == "invite_tracking" else data[key]
    if runtime_changed:
        await _reset_comment_campaign_runtime_state(session, workspace_id, campaign, config)
    campaign.config = config
    if "invite_tracking" in data:
        await upsert_campaign_invite_tracking_metric(session, workspace_id, campaign, config.get("invite_tracking"))
    effective_account_ids = config.get("account_ids") if isinstance(config.get("account_ids"), list) else []
    should_apply_safety_policy = isinstance(campaign.policy, dict) and (
        "daily_limit_per_account" in campaign.policy or "safety_preset" in campaign.policy
    )
    if effective_account_ids and should_apply_safety_policy:
        accounts = await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.workspace_id == workspace_id,
                TelegramAccount.id.in_(effective_account_ids),
            )
        )
        campaign.policy = _apply_comment_campaign_safety_policy(
            _comment_posts_policy_with_defaults(campaign.policy),
            list(accounts.scalars().all()),
        )

    await session.commit()
    await session.refresh(campaign)
    if should_restart_after_update:
        await start_comment_posts_campaign_run(session, workspace_id, campaign)
        await session.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/stop", response_model=CampaignResponse)
async def stop_campaign(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> Campaign:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    await _stop_active_campaign_runs(session, workspace_id, campaign)
    campaign.status = "stopped"
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/archive", response_model=CampaignResponse)
async def archive_campaign(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> Campaign:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    await _stop_active_campaign_runs(
        session,
        workspace_id,
        campaign,
        reason="campaign_archived",
        event_type="campaign_archived_stop_requested",
        event_message="Campaign was archived; active run was cancelled.",
        campaign_status="archived",
    )
    campaign.status = "archived"
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.delete("/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> Response:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    await _stop_active_campaign_runs(
        session,
        workspace_id,
        campaign,
        reason="campaign_deleted",
        event_type="campaign_deleted_stop_requested",
        event_message="Campaign was deleted; active run was cancelled.",
        campaign_status="deleted",
    )
    campaign.status = "deleted"
    await session.commit()
    return Response(status_code=204)


@router.get("/comment-report.xlsx")
async def campaigns_comment_report_xlsx(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    campaign_type: str = Query(default="comment_posts", alias="type"),
    folder: str | None = Query(default=None),
    comment_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=5000, ge=1, le=5000),
) -> Response:
    result = await session.execute(
        select(Campaign)
        .where(
            Campaign.workspace_id == workspace_id,
            Campaign.type == campaign_type,
            Campaign.status.notin_(["archived", "deleted"]),
        )
        .order_by(Campaign.created_at.desc())
    )
    campaigns = list(result.scalars().all())
    normalized_folder = (folder or "").strip()
    if normalized_folder:
        campaigns = [campaign for campaign in campaigns if _campaign_folder_name(campaign) == normalized_folder]

    reports = [
        await build_campaign_comment_report(
            session,
            workspace_id,
            campaign,
            comment_status,
            limit,
        )
        for campaign in campaigns
    ]
    return Response(
        content=_render_campaigns_comment_report_xlsx(reports),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="campaigns-comment-report.xlsx"'},
    )


@router.get("/{campaign_id}/comment-report", response_model=CampaignCommentReport)
async def campaign_comment_report(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    comment_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> CampaignCommentReport:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    return await build_campaign_comment_report(session, workspace_id, campaign, comment_status, limit)


@router.get("/{campaign_id}/safety", response_model=CampaignSafetyResponse)
async def campaign_safety(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> CampaignSafetyResponse:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    return await _campaign_safety(session, workspace_id, campaign)


@router.get("/{campaign_id}/invite-tracking", response_model=CampaignInviteTrackingResponse | None)
async def campaign_invite_tracking(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> CampaignInviteTrackingResponse | None:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    metric = await get_campaign_invite_tracking_metric(session, workspace_id, campaign.id)
    return invite_tracking_response(metric)


@router.post("/{campaign_id}/invite-tracking/refresh", response_model=CampaignInviteTrackingResponse | None)
async def refresh_campaign_invite_tracking(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> CampaignInviteTrackingResponse | None:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    metric = await refresh_campaign_invite_tracking_metric(session, workspace_id, campaign)
    await session.commit()
    return invite_tracking_response(metric)


@router.post("/{campaign_id}/dry-run", response_model=CampaignDryRunResponse)
async def campaign_dry_run(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> CampaignDryRunResponse:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    safety = await _campaign_safety(session, workspace_id, campaign)
    return _campaign_dry_run_response(campaign, safety)


@router.get("/{campaign_id}/comment-report.csv")
async def campaign_comment_report_csv(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    comment_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> Response:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    report = await build_campaign_comment_report(session, workspace_id, campaign, comment_status, limit)
    return Response(
        content=render_campaign_comment_report_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="campaign-comment-report-{campaign_id}.csv"'},
    )


@router.get("/{campaign_id}/comment-report.xlsx")
async def campaign_comment_report_xlsx(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    comment_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> Response:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    report = await build_campaign_comment_report(session, workspace_id, campaign, comment_status, limit)
    return Response(
        content=_render_campaign_comment_report_xlsx(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="campaign-comment-report-{campaign_id}.xlsx"'},
    )


@router.get("/{campaign_id}/reaction-report", response_model=CampaignReactionReport)
async def campaign_reaction_report(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    reaction_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> CampaignReactionReport:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    return await build_campaign_reaction_report(session, workspace_id, campaign, reaction_status, limit)


@router.get("/{campaign_id}/reaction-report.csv")
async def campaign_reaction_report_csv(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    reaction_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> Response:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    report = await build_campaign_reaction_report(session, workspace_id, campaign, reaction_status, limit)
    return Response(
        content=render_campaign_reaction_report_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="campaign-reaction-report-{campaign_id}.csv"'},
    )


@router.get("/{campaign_id}/reaction-report.xlsx")
async def campaign_reaction_report_xlsx(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    reaction_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> Response:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    report = await build_campaign_reaction_report(session, workspace_id, campaign, reaction_status, limit)
    rows = list(csv.reader(StringIO(render_campaign_reaction_report_csv(report).lstrip("\ufeff"))))
    return Response(
        content=_render_xlsx_from_rows(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="campaign-reaction-report-{campaign_id}.xlsx"'},
    )


@router.post("/{campaign_id}/comments/{comment_id}/send", status_code=204)
async def send_campaign_draft_comment(
    campaign_id: str,
    comment_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> Response:
    campaign = await _get_campaign(session, workspace_id, campaign_id)
    comment = await session.get(TelegramComment, comment_id)
    if not comment or comment.workspace_id != workspace_id or comment.campaign_id != campaign.id:
        raise not_found("Comment not found")
    from app.modules.campaigns.account_replacement import replacement_blocks_launch

    if replacement_blocks_launch(campaign) or comment.telegram_account_id not in campaign.config.get("account_ids", []):
        raise HTTPException(409, "Черновик относится к прежнему аккаунту или кампания ещё готовит источники.")
    if comment.status != "draft":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only draft comments can be sent")

    source = await session.get(TelegramSource, comment.source_id)
    message = await session.get(TelegramMessage, comment.post_message_id)
    account = await session.get(TelegramAccount, comment.telegram_account_id) if comment.telegram_account_id else None
    if not source or source.workspace_id != workspace_id or not message or message.workspace_id != workspace_id:
        raise not_found("Comment source or post not found")
    if not account or account.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Telegram account is required")
    _ensure_accounts_allowed([account], _module_for_campaign_type(campaign.type))
    if not message.tg_message_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Telegram post id is missing")

    action = await session.get(CampaignAction, comment.campaign_action_id) if comment.campaign_action_id else None
    now = datetime.now(UTC)
    try:
        if real_telegram_enabled():
            sent = await send_comment_with_account(
                session=session,
                account=account,
                source_ref=source_ref_from_fields(source.username, source.url, source.tg_id),
                post_tg_message_id=message.tg_message_id,
                text=comment.text,
            )
            comment.tg_comment_message_id = sent.tg_message_id
            comment.raw = sent.raw
        else:
            comment.tg_comment_message_id = f"mock-comment-{comment.id[:8]}"
            comment.raw = {"mode": "mock_send"}
    except TelegramGatewayError as exc:
        comment.status = "failed"
        comment.error_code = exc.code
        comment.error_message = str(exc)
        comment.raw = {"mode": "telethon", "error": {"code": exc.code, **exc.raw}}
        if action:
            action.status = "failed"
            action.error_code = exc.code
            action.error_message = str(exc)
            action.executed_at = now
        await session.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    comment.status = "sent"
    comment.sent_at = now
    comment.error_code = None
    comment.error_message = None
    if action:
        action.status = "succeeded"
        action.action_type = "generate_and_send_comment"
        action.executed_at = now
        action.error_code = None
        action.error_message = None
    await session.commit()
    return Response(status_code=204)


@router.post("/{campaign_id}/runs", response_model=CampaignRunResponse, status_code=201)
async def start_campaign_run(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> CampaignRun:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        raise not_found("Campaign not found")
    from app.modules.campaigns.account_replacement import replacement_blocks_launch

    if replacement_blocks_launch(campaign):
        raise HTTPException(
            409, "Не завершена подготовка источников после замены аккаунта. Дождитесь завершения очереди."
        )
    await _ensure_campaign_can_start(session, workspace_id, campaign)
    await refresh_campaign_invite_tracking_metric(session, workspace_id, campaign)
    if campaign.type == "comment_posts":
        return await start_comment_posts_campaign_run(session, workspace_id, campaign)
    if campaign.type in {"neuro_chat", "neuro_dialog", "message_broadcast", "mass_reactions"}:
        return await start_generic_campaign_run(session, workspace_id, campaign)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Campaign runs are not supported for {campaign.type}",
    )


@router.get("/{campaign_id}/runs", response_model=list[CampaignRunResponse])
async def campaign_runs(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> list[CampaignRun]:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        raise not_found("Campaign not found")
    result = await session.execute(
        select(CampaignRun)
        .where(CampaignRun.campaign_id == campaign_id, CampaignRun.workspace_id == workspace_id)
        .order_by(CampaignRun.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/runs/active", response_model=list[CampaignRunResponse])
async def active_campaign_runs(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[CampaignRun]:
    result = await session.execute(
        select(CampaignRun)
        .where(CampaignRun.workspace_id == workspace_id, CampaignRun.status.in_(["pending", "running", "stopping"]))
        .order_by(CampaignRun.created_at.desc())
        .limit(20)
    )
    return list(result.scalars().all())


@router.post("/runs/tick-active", response_model=list[CampaignRunResponse])
async def tick_active_campaign_runs(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[CampaignRun]:
    return await tick_active_campaign_runs_service(session, workspace_id)


@router.get("/runs/{run_id}", response_model=CampaignRunResponse)
async def get_campaign_run(run_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> CampaignRun:
    return await _get_run(session, workspace_id, run_id)


@router.post("/runs/{run_id}/tick", response_model=CampaignRunResponse)
async def tick_campaign_run(run_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> CampaignRun:
    run = await _get_run(session, workspace_id, run_id)
    return await tick_generic_campaign_run(session, workspace_id, run)


@router.post("/runs/{run_id}/stop", response_model=CampaignRunResponse)
async def stop_campaign_run(run_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> CampaignRun:
    run = await _get_run(session, workspace_id, run_id)
    return await request_stop_campaign_run(session, workspace_id, run)


@router.get("/runs/{run_id}/events", response_model=list[CampaignEventResponse])
async def campaign_run_events(
    run_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> list[CampaignEvent]:
    run = await _get_run(session, workspace_id, run_id)
    result = await session.execute(
        select(CampaignEvent).where(CampaignEvent.run_id == run.id).order_by(CampaignEvent.created_at.asc())
    )
    return list(result.scalars().all())


@router.get("/runs/{run_id}/logs", response_model=list[CampaignRuntimeLogResponse])
async def campaign_run_logs(
    run_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    level: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    after_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[CampaignRuntimeLogResponse]:
    run = await _get_run(session, workspace_id, run_id)
    statement = select(CampaignEvent).where(CampaignEvent.run_id == run.id)
    if level and level != "all":
        statement = statement.where(CampaignEvent.level == level)
    if event_type:
        statement = statement.where(CampaignEvent.event_type == event_type)
    if after_id:
        after_event = await session.get(CampaignEvent, after_id)
        if after_event and after_event.run_id == run.id:
            statement = statement.where(CampaignEvent.created_at > after_event.created_at)
    if q:
        like = f"%{q.strip()}%"
        statement = statement.where(CampaignEvent.message.ilike(like) | CampaignEvent.event_type.ilike(like))
    result = await session.execute(statement.order_by(CampaignEvent.created_at.desc()).limit(limit))
    events = list(reversed(result.scalars().all()))
    return [_runtime_log_response(event) for event in events]


@router.get("/runs/{run_id}/report")
async def campaign_run_report(run_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> dict:
    run = await _get_run(session, workspace_id, run_id)
    return run.report


@router.post("/{campaign_id}/start", response_model=CampaignResponse)
async def start_campaign(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        raise not_found("Campaign not found")
    await _ensure_campaign_can_start(session, workspace_id, campaign)
    await refresh_campaign_invite_tracking_metric(session, workspace_id, campaign)
    if campaign.type == "comment_posts":
        campaign.status = "running"
        await session.flush()
        try:
            return await start_comment_posts_campaign(session, workspace_id, campaign)
        except AiProviderError as exc:
            campaign.status = "failed"
            await session.commit()
            raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail=str(exc)) from exc

    if campaign.type in {"neuro_chat", "neuro_dialog", "mass_reactions", "message_broadcast"}:
        await start_generic_campaign_run(session, workspace_id, campaign)
        await session.refresh(campaign)
        return campaign

    campaign.status = "running"
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.get("/{campaign_id}/actions", response_model=list[CampaignActionResponse])
async def campaign_actions(
    campaign_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> list[CampaignAction]:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        raise not_found("Campaign not found")
    result = await session.execute(
        select(CampaignAction)
        .where(CampaignAction.campaign_id == campaign_id)
        .order_by(CampaignAction.created_at.desc())
    )
    return list(result.scalars().all())


async def _ensure_campaign_can_start(session: SessionDep, workspace_id: str, campaign: Campaign) -> None:
    from app.modules.campaigns.account_replacement import replacement_blocks_launch

    if replacement_blocks_launch(campaign):
        raise HTTPException(409, "Подготовка источников после замены аккаунта не завершена.")
    if campaign.config.get("recipient_mode") == "saved_contacts":
        from app.modules.contact_broadcasts.service import ensure_can_start

        await ensure_can_start(session, campaign)
        return
    safety = await _campaign_safety(session, workspace_id, campaign)
    if not safety.can_start:
        blocked = [account for account in safety.accounts if account.status == "blocked"]
        labels = [
            f"{account.account_label}: {account.message or account.reason or 'blocked'}" for account in blocked[:5]
        ]
        blocked_sources = [source for source in safety.sources if source.status == "blocked"]
        labels.extend(
            f"{source.source_label}: {source.message or source.reason or 'blocked'}"
            for source in blocked_sources[: max(0, 5 - len(labels))]
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Кампания не прошла safety preflight. " + "; ".join(labels or ["нет доступных причин блокировки"]),
        )


async def _campaign_safety(session: SessionDep, workspace_id: str, campaign: Campaign) -> CampaignSafetyResponse:
    module = _module_for_campaign_type(campaign.type)
    account_ids = [
        account_id
        for account_id in _json_dict(campaign.config).get("account_ids", [])
        if isinstance(account_id, str) and account_id
    ]
    account_list = await _campaign_accounts(session, workspace_id, account_ids)
    proxy_by_id, proxy_counts = await _proxy_context_for_accounts(session, workspace_id, account_list)
    rows = []
    for account in account_list:
        proxy = proxy_by_id.get(account.proxy_id or "")
        eligibility = get_module_eligibility(
            account,
            module,
            proxy=proxy,
            proxy_account_count=proxy_counts.get(account.proxy_id or ""),
        )
        profile = eligibility.risk_profile
        rows.append(
            {
                "account_id": account.id,
                "account_label": _account_label(account),
                "module": module,
                "status": eligibility.status,
                "reason": eligibility.reason,
                "message": eligibility.message,
                "risk_level": profile.risk_level if profile else "unknown",
                "risk_score": profile.score if profile else 0,
                "recommended_next_step": profile.recommended_next_step if profile else None,
            }
        )
    source_rows = await _campaign_source_safety(session, workspace_id, campaign)
    blocked_count = sum(1 for row in rows if row["status"] == "blocked") + sum(
        1 for row in source_rows if row["status"] == "blocked"
    )
    limited_count = sum(1 for row in rows if row["status"] == "limited") + sum(
        1 for row in source_rows if row["status"] == "limited"
    )
    return CampaignSafetyResponse(
        campaign_id=campaign.id,
        module=module,
        status="blocked" if blocked_count else "limited" if limited_count else "allowed",
        can_start=blocked_count == 0,
        blocked_count=blocked_count,
        limited_count=limited_count,
        accounts=rows,
        sources=source_rows,
    )


def _campaign_dry_run_response(campaign: Campaign, safety: CampaignSafetyResponse) -> CampaignDryRunResponse:
    account_counts = _status_counts(row.status for row in safety.accounts)
    source_counts = _source_status_counts(safety.sources)
    blocking_reasons: list[str] = []
    warnings: list[str] = []

    for account in safety.accounts:
        line = f"{account.account_label}: {account.message or account.reason or account.status}"
        if account.status == "blocked":
            blocking_reasons.append(line)
        elif account.status == "limited":
            warnings.append(line)
    for source in safety.sources:
        line = f"{source.source_label}: {source.message or source.reason or source.status}"
        if source.status == "blocked":
            blocking_reasons.append(line)
        elif source.status == "limited":
            warnings.append(line)

    return CampaignDryRunResponse(
        campaign_id=campaign.id,
        campaign_type=campaign.type,
        can_start=safety.can_start,
        accounts=account_counts,
        sources=source_counts,
        effective_policy=_json_dict(campaign.policy),
        blocking_reasons=blocking_reasons,
        warnings=warnings,
        safety=safety,
    )


def _status_counts(statuses: Iterable[str]) -> dict[str, int]:
    counts = {"allowed": 0, "limited": 0, "blocked": 0}
    for status_value in statuses:
        if status_value in counts:
            counts[str(status_value)] += 1
    return counts


def _source_status_counts(sources: list[object]) -> dict[str, int]:
    counts = {
        "safe": 0,
        "limited": 0,
        "blocked": 0,
        "invalid": 0,
        "unknown": 0,
    }
    for source in sources:
        status_value = getattr(source, "status", "")
        risk_level = getattr(source, "risk_level", "")
        reason = str(getattr(source, "reason", "") or "")
        if status_value == "allowed":
            counts["safe"] += 1
        elif status_value == "limited":
            counts["limited"] += 1
        elif status_value == "blocked":
            counts["blocked"] += 1
        if risk_level == "unknown" or reason.startswith("source_status_"):
            counts["unknown"] += 1
        if risk_level == "critical":
            counts["invalid"] += 1
    return counts


async def _campaign_accounts(
    session: SessionDep,
    workspace_id: str,
    account_ids: list[str],
) -> list[TelegramAccount]:
    if not account_ids:
        return []
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(account_ids),
        )
    )
    accounts = list(result.scalars().all())
    if len(accounts) != len(set(account_ids)):
        raise not_found("One or more accounts not found")
    order = {account_id: index for index, account_id in enumerate(account_ids)}
    return sorted(accounts, key=lambda account: order.get(account.id, 0))


async def _campaign_source_safety(
    session: SessionDep,
    workspace_id: str,
    campaign: Campaign,
) -> list[dict[str, object]]:
    if campaign.type not in {"comment_posts", "mass_reactions"}:
        return []
    source_ids = [
        source_id
        for source_id in _json_dict(campaign.config).get("source_ids", [])
        if isinstance(source_id, str) and source_id
    ]
    if not source_ids:
        return []
    result = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(source_ids),
        )
    )
    sources = list(result.scalars().all())
    order = {source_id: index for index, source_id in enumerate(source_ids)}
    rows: list[dict[str, object]] = []
    for source in sorted(sources, key=lambda item: order.get(item.id, 0)):
        risk_level, reason = _source_risk_for_campaign(source, campaign.type)
        status_value = (
            "blocked" if risk_level == "critical" else "limited" if risk_level in {"medium", "unknown"} else "allowed"
        )
        rows.append(
            {
                "source_id": source.id,
                "source_label": source.title or source.username or source.url or source.id[:8],
                "status": status_value,
                "reason": reason,
                "message": _source_safety_message(reason),
                "risk_level": risk_level,
                "recommended_next_step": "archive_source"
                if risk_level == "critical"
                else "check_source"
                if risk_level == "unknown"
                else None,
            }
        )
    if len(sources) != len(set(source_ids)):
        rows.append(
            {
                "source_id": "missing",
                "source_label": "Missing source",
                "status": "blocked",
                "reason": "source_missing",
                "message": "В кампании есть источник, которого больше нет в базе.",
                "risk_level": "critical",
                "recommended_next_step": "edit_campaign_sources",
            }
        )
    return rows


def _source_risk_for_campaign(source: TelegramSource, campaign_type: str) -> tuple[str, str | None]:
    metadata = _json_dict(source.source_metadata)
    if source.access_status == "blacklisted" or metadata.get("blacklisted") is True:
        return "critical", str(metadata.get("blacklist_reason") or "source_blacklisted")
    if source.access_status == "archived":
        return "critical", "source_archived"
    if source.access_status.startswith("invalid_") or metadata.get("invalid") is True:
        return "critical", str(metadata.get("invalid_reason") or source.access_status)
    if source.is_private:
        return "critical", "invalid_private"
    if source.is_join_request_required:
        return "critical", "invalid_join_request"
    if campaign_type == "comment_posts":
        if source.type != "channel":
            return "critical", "source_not_channel"
        if source.access_status in {"unknown", "pending_check", "failed"}:
            return "unknown", f"source_status_{source.access_status}"
    if campaign_type == "mass_reactions" and source.type not in {"channel", "group"}:
        return "critical", "source_type_not_allowed"
    if campaign_type == "mass_reactions" and source.access_status in {"unknown", "pending_check", "failed", "blocked"}:
        return "unknown", f"source_status_{source.access_status}"
    return "low", None


def _source_safety_message(reason: str | None) -> str | None:
    if not reason:
        return None
    if reason in {"source_comments_not_available", "invalid_comments_closed"}:
        return "У источника закрыты или недоступны комментарии."
    if reason in {"source_not_accessible", "invalid_private"}:
        return "Источник приватный или недоступен аккаунту."
    if reason in {"discussion_join_approval_required", "invalid_join_request"}:
        return "Discussion требует вступления или заявки. Такой источник опасен для новых аккаунтов."
    if reason in {"account_cannot_write_comments", "invalid_write_forbidden"}:
        return "Аккаунт не может писать комментарии в этот источник."
    if reason in {"source_blacklisted", "blacklisted"}:
        return "Источник находится в черном списке и исключен из запусков."
    if reason.startswith("source_status_"):
        return "Источник еще не проверен или последняя проверка упала."
    return "Источник не подходит для безопасного запуска кампании."


def _ensure_accounts_allowed(accounts: list[TelegramAccount], module: str) -> None:
    blocked = []
    for account in accounts:
        eligibility = get_module_eligibility(account, module)
        if eligibility.status == "blocked":
            blocked.append(f"{_account_label(account)}: {eligibility.message or eligibility.reason or 'blocked'}")
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Аккаунты не прошли safety preflight. " + "; ".join(blocked[:5]),
        )


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
    counts_result = await session.execute(
        select(TelegramAccount.proxy_id, func.count(TelegramAccount.id))
        .where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.proxy_id.in_(proxy_ids),
            TelegramAccount.status.notin_(["archived", "deleted"]),
        )
        .group_by(TelegramAccount.proxy_id)
    )
    proxy_counts = {proxy_id: int(account_count) for proxy_id, account_count in counts_result.all() if proxy_id}
    return proxy_by_id, proxy_counts


def _module_for_campaign_type(campaign_type: str) -> str:
    if campaign_type == "comment_posts":
        return "neurocommenting"
    if campaign_type in {"neuro_chat", "neuro_dialog"}:
        return "neurochatting"
    if campaign_type == "mass_reactions":
        return "mass_reactions"
    if campaign_type == "message_broadcast":
        return "message_broadcast"
    return campaign_type


def _account_label(account: TelegramAccount) -> str:
    if account.display_name:
        return account.display_name
    if account.username:
        return f"@{account.username.lstrip('@')}"
    if account.phone:
        return account.phone
    return account.id[:8]


async def _get_run(session: SessionDep, workspace_id: str, run_id: str) -> CampaignRun:
    run = await session.get(CampaignRun, run_id)
    if not run or run.workspace_id != workspace_id:
        raise not_found("Campaign run not found")
    return run


async def _get_campaign(session: SessionDep, workspace_id: str, campaign_id: str) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        raise not_found("Campaign not found")
    return campaign


def _runtime_log_response(event: CampaignEvent) -> CampaignRuntimeLogResponse:
    payload = _json_dict(event.payload)
    return CampaignRuntimeLogResponse.model_validate(event).model_copy(
        update={
            "account_id": _optional_string(payload.get("account_id")),
            "account_name": _optional_string(payload.get("account_name")),
            "source_id": _optional_string(payload.get("source_id")),
            "source_title": _optional_string(payload.get("source_title")),
            "source_ref": _optional_string(payload.get("source_ref")),
        }
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _stop_active_campaign_runs(
    session: SessionDep,
    workspace_id: str,
    campaign: Campaign,
    *,
    reason: str = "manual_stop",
    event_type: str = "manual_stop_requested",
    event_message: str = "Manual stop was requested; run was cancelled by the API.",
    campaign_status: str | None = "cancelled",
) -> None:
    result = await session.execute(
        select(CampaignRun).where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.status.in_(["pending", "running", "paused", "stopping"]),
        )
    )
    for run in result.scalars().all():
        await request_stop_campaign_run(
            session,
            workspace_id,
            run,
            reason=reason,
            event_type=event_type,
            event_message=event_message,
            campaign_status=campaign_status,
        )


async def _reset_comment_campaign_runtime_state(
    session: SessionDep,
    workspace_id: str,
    campaign: Campaign,
    config: dict,
) -> None:
    for key in [
        "blocked_sources",
        "auto_join_wait_until",
        "last_auto_join_attempt_at",
        "last_auto_join_flood_wait_at",
    ]:
        config.pop(key, None)

    source_ids = [str(source_id) for source_id in config.get("source_ids", []) if isinstance(source_id, str)]
    if not source_ids:
        return

    result = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(source_ids),
        )
    )
    for source in result.scalars().all():
        metadata = _json_dict(source.source_metadata)
        blocked = _json_dict(metadata.get("blocked_in_campaign"))
        if blocked.get("campaign_id") != campaign.id:
            continue
        metadata.pop("blocked_in_campaign", None)
        source.source_metadata = metadata
        if source.access_status == "blocked":
            source.access_status = "available" if source.comments_enabled else "pending_check"


def _json_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _clean_trigger_keywords(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for item in value:
        keyword = " ".join(str(item or "").split()).strip(" ,;")
        if not keyword:
            continue
        lowered = keyword.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        keywords.append(keyword[:120])
    return keywords[:100]


def _comment_posts_policy_with_defaults(policy: dict | None) -> dict:
    policy_dict = _json_dict(policy)
    return {
        **policy_dict,
        "stop_words_enabled": policy_dict.get("stop_words_enabled", True),
        "stop_words": policy_dict.get("stop_words", DEFAULT_COMMENT_STOP_WORDS),
        "min_post_chars": policy_dict.get("min_post_chars", DEFAULT_COMMENT_MIN_POST_CHARS),
        "min_media_post_chars": policy_dict.get("min_media_post_chars", DEFAULT_COMMENT_MIN_MEDIA_POST_CHARS),
        "allow_short_media_posts": policy_dict.get("allow_short_media_posts", True),
    }


COMMENT_SAFETY_PRESETS: dict[str, dict[str, int | bool | str]] = {
    "new_batch_draft": {
        "daily_limit_per_account": 0,
        "delay_seconds_min": 0,
        "delay_seconds_max": 0,
        "auto_join_sources": False,
        "auto_join_discussions": False,
        "delivery_mode": "draft_only",
    },
    "first_launch": {
        "daily_limit_per_account": 3,
        "delay_seconds_min": 600,
        "delay_seconds_max": 1800,
        "auto_join_sources": False,
        "auto_join_discussions": False,
    },
    "gentle": {
        "daily_limit_per_account": 5,
        "delay_seconds_min": 600,
        "delay_seconds_max": 1500,
        "auto_join_sources": False,
        "auto_join_discussions": False,
    },
    "standard": {
        "daily_limit_per_account": 10,
        "delay_seconds_min": 300,
        "delay_seconds_max": 1200,
        "auto_join_sources": False,
    },
    "growth": {
        "daily_limit_per_account": 20,
        "delay_seconds_min": 180,
        "delay_seconds_max": 900,
    },
}


def _apply_comment_campaign_safety_policy(policy: dict, accounts: list[TelegramAccount]) -> dict:
    next_policy = dict(policy)
    requested_preset = str(next_policy.get("safety_preset") or "").strip()
    if requested_preset == "standard_with_join":
        requested_preset = "standard"
    has_new_or_warming = any(_is_new_or_warming_account(account) for account in accounts)
    preset_id = requested_preset or ("first_launch" if has_new_or_warming else "")
    preset = COMMENT_SAFETY_PRESETS.get(preset_id)
    next_policy.setdefault("auto_join_sources", False)
    next_policy.setdefault("auto_join_discussions", False)
    if not preset:
        return next_policy

    original = dict(next_policy)
    if "daily_limit_per_account" not in next_policy:
        next_policy["daily_limit_per_account"] = int(preset["daily_limit_per_account"])

    min_delay = int(preset["delay_seconds_min"])
    max_delay = int(preset["delay_seconds_max"])
    if "delay_seconds_min" not in next_policy:
        next_policy["delay_seconds_min"] = min_delay
    if "delay_seconds_max" not in next_policy:
        next_policy["delay_seconds_max"] = max_delay
    for key in ["auto_join_interval_seconds", "auto_join_daily_limit", "auto_join_max_per_tick"]:
        if key in preset and key not in next_policy:
            next_policy[key] = int(preset[key])
    next_policy["safety_preset"] = preset_id
    if any(original.get(key) != next_policy.get(key) for key in next_policy):
        adjustments = list(next_policy.get("safety_adjustments") or [])
        adjustments.append(
            {
                "preset": preset_id,
                "reason": "new_or_warming_accounts" if has_new_or_warming else "campaign_safety_preset",
                "applied_at": datetime.now(UTC).isoformat(),
            }
        )
        next_policy["safety_adjustments"] = adjustments[-10:]
    return next_policy


def _is_new_or_warming_account(account: TelegramAccount) -> bool:
    age_days = max(0, (datetime.now(UTC) - account.created_at).days)
    return (
        age_days < 7
        or account.status in {"new", "warming"}
        or account.warmup_status in {"not_started", "running", "warming"}
        or account.warmup_score < 80
    )


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _comment_campaign_runtime_changed(campaign: Campaign, data: dict) -> bool:
    config = _json_dict(campaign.config)
    for key in [
        "source_ids",
        "source_folder_ids",
        "source_assignment_mode",
        "account_ids",
        "comment_prompt_template_id",
        "latest_posts_per_source",
        "post_selection_mode",
        "delivery_mode",
        "mode",
        "stop_conditions",
    ]:
        if key in data and data[key] != config.get(key):
            return True
    return "policy" in data and (data["policy"] or {}) != _json_dict(campaign.policy)


async def _resolve_campaign_source_ids(
    session: SessionDep,
    workspace_id: str,
    source_ids: list[str],
    source_folder_ids: list[str],
) -> list[str]:
    cleaned_source_ids = [str(source_id).strip() for source_id in source_ids if str(source_id).strip()]
    cleaned_folder_ids = [str(folder_id).strip() for folder_id in source_folder_ids if str(folder_id).strip()]
    resolved_ids = list(dict.fromkeys(cleaned_source_ids))
    if cleaned_folder_ids:
        folders = await session.execute(
            select(TelegramSourceFolder.id).where(
                TelegramSourceFolder.workspace_id == workspace_id,
                TelegramSourceFolder.id.in_(cleaned_folder_ids),
            )
        )
        found_folder_ids = {folder_id for folder_id in folders.scalars().all()}
        if len(found_folder_ids) != len(set(cleaned_folder_ids)):
            raise not_found("One or more source folders not found")
        folder_sources = await session.execute(
            select(TelegramSource.id)
            .where(
                TelegramSource.workspace_id == workspace_id,
                TelegramSource.folder_id.in_(cleaned_folder_ids),
                TelegramSource.access_status.notin_(["archived", "blacklisted"]),
            )
            .order_by(TelegramSource.created_at.asc())
        )
        resolved_ids.extend(str(source_id) for source_id in folder_sources.scalars().all())
    resolved_ids = list(dict.fromkeys(resolved_ids))
    if not resolved_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Выберите источники или папку источников для кампании.",
        )
    return resolved_ids


def _source_assignment_config(
    mode: str,
    account_ids: list[str],
    source_ids: list[str],
    manual_assignment: dict | None = None,
) -> dict:
    normalized_mode = (
        mode if mode in {"standard", "parallel_pool", "multi_account_split", "manual_pinned"} else "standard"
    )
    if normalized_mode == "standard":
        return {
            "source_assignment_mode": "standard",
            "source_assignment": {},
            "sources_per_account": None,
        }
    assignment = (
        _sanitize_source_assignment(manual_assignment or {}, account_ids, source_ids)
        if normalized_mode == "manual_pinned"
        else _build_parallel_source_assignment(account_ids, source_ids)
        if normalized_mode == "parallel_pool"
        else _build_source_assignment(account_ids, source_ids)
    )
    non_empty = [items for items in assignment.values() if items]
    max_sources = max((len(items) for items in non_empty), default=0)
    min_sources = min((len(items) for items in non_empty), default=0)
    return {
        "source_assignment_mode": normalized_mode,
        "source_assignment": assignment,
        "sources_per_account": {
            "min": min_sources,
            "max": max_sources,
            "accounts": len(account_ids),
            "sources": len(source_ids),
        },
    }


def _sanitize_source_assignment(
    manual_assignment: dict,
    account_ids: list[str],
    source_ids: list[str],
) -> dict[str, list[str]]:
    cleaned_accounts = [account_id for account_id in dict.fromkeys(account_ids) if account_id]
    cleaned_sources = [source_id for source_id in dict.fromkeys(source_ids) if source_id]
    allowed_accounts = set(cleaned_accounts)
    allowed_sources = set(cleaned_sources)
    assignment: dict[str, list[str]] = {account_id: [] for account_id in cleaned_accounts}
    used_sources: set[str] = set()

    for account_id, values in manual_assignment.items():
        account_key = str(account_id)
        if account_key not in allowed_accounts or not isinstance(values, list):
            continue
        for source_id in values:
            source_key = str(source_id)
            if source_key not in allowed_sources or source_key in used_sources:
                continue
            assignment[account_key].append(source_key)
            used_sources.add(source_key)

    unassigned = [source_id for source_id in cleaned_sources if source_id not in used_sources]
    if unassigned and cleaned_accounts:
        account_load = {account_id: len(assignment[account_id]) for account_id in cleaned_accounts}
        for source_id in unassigned:
            account_id = min(cleaned_accounts, key=lambda item: account_load[item])
            assignment[account_id].append(source_id)
            account_load[account_id] += 1
    return assignment


def _build_source_assignment(account_ids: list[str], source_ids: list[str]) -> dict[str, list[str]]:
    cleaned_accounts = [account_id for account_id in dict.fromkeys(account_ids) if account_id]
    cleaned_sources = [source_id for source_id in dict.fromkeys(source_ids) if source_id]
    if not cleaned_accounts:
        return {}
    assignment = {account_id: [] for account_id in cleaned_accounts}
    for index, source_id in enumerate(cleaned_sources):
        account_id = cleaned_accounts[index % len(cleaned_accounts)]
        assignment[account_id].append(source_id)
    return assignment


def _build_parallel_source_assignment(account_ids: list[str], source_ids: list[str]) -> dict[str, list[str]]:
    cleaned_accounts = [account_id for account_id in dict.fromkeys(account_ids) if account_id]
    cleaned_sources = [source_id for source_id in dict.fromkeys(source_ids) if source_id]
    return {account_id: list(cleaned_sources) for account_id in cleaned_accounts}


def _legacy_campaign_mode(post_selection_mode: str, delivery_mode: str) -> str:
    if delivery_mode == "generate_and_send":
        return "generate_and_send"
    if post_selection_mode == "new_posts":
        return "comment_new_posts"
    return "comment_recent_posts"


def _ensure_comment_sources(sources: list[TelegramSource]) -> None:
    if any(
        source.access_status == "blacklisted" or _json_dict(source.source_metadata).get("blacklisted") is True
        for source in sources
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Источник находится в черном списке. Снимите блокировку перед запуском кампании.",
        )
    invalid = [
        source.title or source.username or source.id
        for source in sources
        if source.type != "channel"
        or source.access_status == "archived"
        or source.access_status.startswith("invalid_")
        or source.is_private
        or source.is_join_request_required
    ]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AI-комментарии можно запускать только на доступные Telegram-каналы",
        )


def _ensure_sources_type(sources: list[TelegramSource], allowed_types: set[str], detail: str) -> None:
    if any(
        source.access_status == "blacklisted" or _json_dict(source.source_metadata).get("blacklisted") is True
        for source in sources
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Источник находится в черном списке. Снимите блокировку перед запуском кампании.",
        )
    if any(source.type not in allowed_types for source in sources):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def _ensure_sources_not_used_by_other_campaigns(
    session: SessionDep,
    workspace_id: str,
    campaign_type: str,
    source_ids: list[str],
    *,
    current_campaign_id: str | None = None,
) -> None:
    requested_ids = {source_id for source_id in source_ids if source_id}
    if not requested_ids:
        return

    result = await session.execute(
        select(Campaign).where(
            Campaign.workspace_id == workspace_id,
            Campaign.type == campaign_type,
            Campaign.status.notin_(["archived", "deleted"]),
        )
    )
    conflicts: list[str] = []
    for campaign in result.scalars().all():
        if current_campaign_id and campaign.id == current_campaign_id:
            continue
        used_ids = {
            source_id for source_id in _json_dict(campaign.config).get("source_ids", []) if isinstance(source_id, str)
        }
        if requested_ids & used_ids:
            conflicts.append(campaign.name)

    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Источник уже выбран в другой кампании: {', '.join(conflicts[:3])}",
        )
