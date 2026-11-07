from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.config import get_settings
from app.core.errors import bad_request, not_found
from app.modules.account_warmup.models import WarmupPlan, WarmupRun, WarmupStep
from app.modules.account_warmup.schemas import (
    WarmupPlanCreate,
    WarmupPlanResponse,
    WarmupPlanUpdate,
    WarmupPresetResponse,
    WarmupRunResponse,
    WarmupStepResponse,
)
from app.modules.account_warmup.service import (
    WARMUP_PRESETS,
    delete_warmup_plan,
    start_warmup_plan,
    stop_warmup_plan,
    tick_active_warmup_runs,
)
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount

router = APIRouter()

PRESET_LABELS: dict[str, tuple[str, str]] = {
    "primary_warmup": (
        "Первичный прогрев",
        (
            "30 минут чтения до 5 действий/час, 12 часов отлежки, "
            "30 минут до 10 действий/час и снова 12 часов отлежки. "
            "Отлежку после импорта 12-24 часа делаем вручную до запуска пресета."
        ),
    ),
    "new_account_rest": (
        "Отлежка новых 24-48ч",
        "Нулевая Telegram-активность после импорта: только ожидание на чистом прокси.",
    ),
    "new_account_validation": (
        "Валидация новых",
        "Первые сутки без активных действий: один поздний профиль-чек для оценки качества аккаунта.",
    ),
    "new_account_safe": (
        "Щадящий для новых",
        "Семидневный мягкий рост без реакций, подписок и сообщений в первые дни.",
    ),
    "incubator_safe_v2": (
        "Инкубатор 14 дней",
        "Внутренняя схема: отлежка, короткая проверка, сессии чтения и мягкая готовность к кампаниям.",
    ),
    "controlled_source_join": (
        "Осторожный join источников",
        "Двухнедельный режим с поздними и редкими подписками на источники: максимум несколько join за план.",
    ),
    "balanced_human": (
        "Средний человеческий",
        "Плавный прогрев с чтением, просмотрами, редкими реакциями и сообщениями после паузы.",
    ),
    "trusted_account_growth": (
        "Рост для доверенных",
        "Осторожный режим только с чтением для аккаунтов с warmup score от 80 и без runtime-ограничений.",
    ),
}


@router.get("/presets", response_model=list[WarmupPresetResponse])
async def list_warmup_presets() -> list[WarmupPresetResponse]:
    presets: list[WarmupPresetResponse] = []
    for preset_id, preset in WARMUP_PRESETS.items():
        name, description = PRESET_LABELS.get(preset_id, (preset_id, ""))
        presets.append(
            WarmupPresetResponse(
                id=preset_id,
                name=name,
                description=description,
                duration_days=int(preset.get("duration_days") or 3),
                config={**dict(preset.get("config") or {}), "preset": preset_id},
                policy=dict(preset.get("policy") or {}),
            )
        )
    return presets


@router.get("/plans", response_model=list[WarmupPlanResponse])
async def list_plans(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupPlan]:
    result = await session.execute(
        select(WarmupPlan).where(WarmupPlan.workspace_id == workspace_id).order_by(WarmupPlan.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/plans", response_model=WarmupPlanResponse, status_code=201)
async def create_plan(
    payload: WarmupPlanCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> WarmupPlan:
    config = {**payload.config}
    if payload.account_ids:
        await _get_workspace_accounts(session, workspace_id, payload.account_ids)
        config["account_ids"] = payload.account_ids
    if payload.scenario is not None:
        config["scenario"] = payload.scenario
    await _materialize_warmup_sources(session, workspace_id, config)

    plan = WarmupPlan(
        workspace_id=workspace_id,
        created_by=current_user.id,
        name=payload.name,
        duration_days=payload.duration_days,
        config=config,
        policy=payload.policy,
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.patch("/plans/{plan_id}", response_model=WarmupPlanResponse)
async def update_plan(
    plan_id: str,
    payload: WarmupPlanUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> WarmupPlan:
    plan = await _get_plan(session, workspace_id, plan_id)
    if payload.name is not None:
        plan.name = payload.name.strip() or plan.name
    if payload.status is not None:
        plan.status = payload.status
    if payload.duration_days is not None:
        plan.duration_days = payload.duration_days
    if payload.policy is not None:
        plan.policy = payload.policy
    if payload.config is not None or payload.account_ids is not None or payload.scenario is not None:
        config = {**(plan.config or {})}
        if payload.config is not None:
            config.update(payload.config)
        if payload.account_ids is not None:
            await _get_workspace_accounts(session, workspace_id, payload.account_ids)
            config["account_ids"] = payload.account_ids
        if payload.scenario is not None:
            config["scenario"] = payload.scenario
        await _materialize_warmup_sources(session, workspace_id, config)
        plan.config = config
    await session.commit()
    await session.refresh(plan)
    return plan


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(plan_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> None:
    plan = await _get_plan(session, workspace_id, plan_id)
    await delete_warmup_plan(session, workspace_id, plan)
    await session.commit()


@router.get("/plans/{plan_id}/runs", response_model=list[WarmupRunResponse])
async def list_plan_runs(plan_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupRun]:
    plan = await _get_plan(session, workspace_id, plan_id)
    result = await session.execute(
        select(WarmupRun)
        .where(WarmupRun.workspace_id == workspace_id, WarmupRun.warmup_plan_id == plan.id)
        .order_by(WarmupRun.created_at.desc(), WarmupRun.current_day.desc(), WarmupRun.id.desc())
    )
    return list(result.scalars().all())


@router.get("/plans/{plan_id}/steps", response_model=list[WarmupStepResponse])
async def list_plan_steps(plan_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupStep]:
    plan = await _get_plan(session, workspace_id, plan_id)
    runs = list(
        (
            await session.execute(
                select(WarmupRun).where(WarmupRun.workspace_id == workspace_id, WarmupRun.warmup_plan_id == plan.id)
            )
        )
        .scalars()
        .all()
    )
    if not runs:
        return []

    run_ids = [run.id for run in runs]
    result = await session.execute(
        select(WarmupStep)
        .where(WarmupStep.warmup_run_id.in_(run_ids))
        .order_by(WarmupStep.planned_at.desc().nullslast(), WarmupStep.created_at.desc())
        .limit(1500)
    )
    return list(result.scalars().all())


@router.post("/plans/{plan_id}/runs", response_model=list[WarmupRunResponse], status_code=201)
async def create_plan_runs(plan_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupRun]:
    return await _start_plan(plan_id, session, workspace_id)


@router.post("/plans/{plan_id}/start", response_model=list[WarmupRunResponse], status_code=201)
async def start_plan(plan_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupRun]:
    return await _start_plan(plan_id, session, workspace_id)


@router.post("/plans/{plan_id}/stop", response_model=list[WarmupRunResponse])
async def stop_plan(plan_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupRun]:
    plan = await _get_plan(session, workspace_id, plan_id)
    runs = await stop_warmup_plan(session, workspace_id, plan)
    await session.commit()
    return runs


@router.post("/tick", response_model=list[WarmupRunResponse])
async def tick_warmup(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupRun]:
    runs = await tick_active_warmup_runs(session, workspace_id, limit=20)
    await session.commit()
    return runs


async def _start_plan(plan_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> list[WarmupRun]:
    plan = await _get_plan(session, workspace_id, plan_id)
    config = {**(plan.config or {})}
    await _materialize_warmup_sources(session, workspace_id, config)
    plan.config = config
    try:
        runs = await start_warmup_plan(session, workspace_id, plan)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    await session.commit()
    for run in runs:
        await session.refresh(run)
    return runs


async def _get_plan(session: SessionDep, workspace_id: str, plan_id: str) -> WarmupPlan:
    plan = await session.get(WarmupPlan, plan_id)
    if not plan or plan.workspace_id != workspace_id:
        raise not_found("Warmup plan not found")
    return plan


async def _get_workspace_accounts(
    session: SessionDep,
    workspace_id: str,
    account_ids: list[str],
) -> list[TelegramAccount]:
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(account_ids),
        )
    )
    accounts_by_id = {account.id: account for account in result.scalars().all()}
    if len(accounts_by_id) != len(set(account_ids)):
        raise not_found("One or more Telegram accounts not found")
    return [accounts_by_id[account_id] for account_id in account_ids]


async def _materialize_warmup_sources(session: SessionDep, workspace_id: str, config: dict) -> None:
    system_refs = _system_warmup_source_refs(config)
    manual_refs = _warmup_source_refs(config.get("warmup_source_refs"))
    refs = list(dict.fromkeys([*system_refs, *manual_refs]))
    if not refs:
        return
    system_keys = {_source_key(*_normalize_warmup_source_ref(ref)) for ref in system_refs}
    existing_result = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.access_status != "archived",
        )
    )
    existing = list(existing_result.scalars().all())
    by_key = {_source_key(source.username, source.url): source for source in existing}
    source_ids: list[str] = []
    for ref in refs:
        username, url = _normalize_warmup_source_ref(ref)
        if not username and not url:
            continue
        key = _source_key(username, url)
        source = by_key.get(key)
        if source is None:
            source = TelegramSource(
                workspace_id=workspace_id,
                type="channel",
                username=username,
                url=url,
                title=username or url,
                access_status="pending_check",
                source_metadata={},
            )
            session.add(source)
            await session.flush()
            by_key[key] = source
        metadata = dict(source.source_metadata or {})
        metadata["warmup_pool"] = True
        if key in system_keys:
            metadata["warmup_pool_source"] = "system_catalog"
        else:
            metadata["warmup_pool_source"] = "warmup_plan"
        source.source_metadata = metadata
        source_ids.append(source.id)
    if source_ids:
        current_ids = [item for item in config.get("warmup_source_ids", []) if isinstance(item, str) and item]
        config["warmup_source_ids"] = list(dict.fromkeys([*current_ids, *source_ids]))
        config["source_strategy"] = "warmup_pool"


def _system_warmup_source_refs(config: dict) -> list[str]:
    if str(config.get("preset") or "") != "primary_warmup":
        return []
    return _warmup_source_refs(get_settings().warmup_system_source_refs)


def _warmup_source_refs(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]
    return []


def _normalize_warmup_source_ref(ref: str) -> tuple[str | None, str | None]:
    clean = ref.strip()
    if not clean:
        return None, None
    if clean.startswith("https://t.me/") or clean.startswith("http://t.me/"):
        slug = clean.rstrip("/").split("/")[-1]
        if slug and not slug.startswith("+") and slug != "joinchat":
            return slug.lstrip("@"), clean
        return None, clean
    if clean.startswith("t.me/"):
        slug = clean.rstrip("/").split("/")[-1]
        return (slug.lstrip("@") if slug else None), f"https://{clean}"
    return clean.lstrip("@"), None


def _source_key(username: str | None, url: str | None) -> tuple[str, str]:
    return ((username or "").lower().lstrip("@"), (url or "").rstrip("/").lower())
