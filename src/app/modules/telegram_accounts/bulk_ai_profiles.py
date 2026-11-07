import asyncio
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.service import generate_profile_avatar_image
from app.modules.telegram_accounts.models import AccountHealthEvent, TelegramAccount
from app.modules.telegram_accounts.schemas import (
    AccountBulkAiProfilesRequest,
    TelegramAccountProfilePatch,
)
from app.modules.telegram_accounts.service import (
    generated_profile_for_account,
    update_account_profile,
    update_account_profile_photo_bytes,
)

AI_PROFILE_SAFE_STATUSES = {"active", "warming", "ready"}


async def complete_bulk_ai_profiles(
    session: AsyncSession,
    *,
    workspace_id: str,
    account_ids: list[str],
    request_id: str,
    payload_data: dict,
) -> dict:
    completed = 0
    failed: list[dict[str, str]] = []
    ordered_account_ids: list[str] = []
    payload = AccountBulkAiProfilesRequest.model_validate(payload_data)
    try:
        accounts = await _get_workspace_accounts(session, workspace_id, account_ids)
        accounts_by_id = {account.id: account for account in accounts}
        ordered_account_ids = [account_id for account_id in account_ids if account_id in accounts_by_id]
        total = len(ordered_account_ids)

        for index, account_id in enumerate(ordered_account_ids):
            await record_bulk_action_for_account_ids(
                session,
                [account_id],
                event_type="bulk_ai_profile_processing",
                message="AI профиль обновляется.",
                raw={
                    "bulk_request_id": request_id,
                    "status": "processing",
                    "index": index + 1,
                    "total": total,
                },
            )
            try:
                account = await _get_workspace_account(session, workspace_id, account_id)
                if account.status not in AI_PROFILE_SAFE_STATUSES:
                    raise RuntimeError(
                        "Аккаунт не подходит для AI профиля: "
                        f"текущий статус {account.status}. Проверьте ограничения перед изменением профиля."
                    )

                profile = generated_profile_for_account(
                    account,
                    index=index,
                    style_prompt=payload.style_prompt,
                    profile_gender=payload.profile_gender,
                )
                first_name = payload.custom_first_name or (
                    profile["first_name"] if payload.generate_first_last_name else None
                )
                last_name = payload.custom_last_name or (
                    profile["last_name"] if payload.generate_first_last_name else None
                )
                bio = payload.custom_bio or (profile["bio"] if payload.generate_bio else None)
                if first_name or last_name or bio:
                    await update_account_profile(
                        session,
                        account,
                        TelegramAccountProfilePatch(
                            first_name=str(first_name or ""),
                            last_name=str(last_name or ""),
                            about=str(bio or ""),
                        ),
                    )
                if payload.generate_photo:
                    photo, photo_meta = await generate_profile_avatar_image(
                        build_ai_profile_avatar_prompt(profile, payload.style_prompt)
                    )
                    await update_account_profile_photo_bytes(
                        session,
                        account,
                        photo=photo,
                        file_name=f"demo-platform-profile-openai-{index + 1}.jpg",
                        content_type="image/jpeg",
                    )
                    add_ai_profile_photo_event(session, account, request_id, photo_meta)
                    await session.commit()
                completed += 1
                await record_bulk_action_for_account_ids(
                    session,
                    [account_id],
                    event_type="bulk_ai_profile_completed",
                    message="AI профиль обновлен.",
                    raw={
                        "bulk_request_id": request_id,
                        "status": "completed",
                        "index": index + 1,
                        "total": total,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - keep processing other accounts.
                await session.rollback()
                error_text = f"{type(exc).__name__}: {exc}"
                failed.append({"account_id": account_id, "error": error_text})
                await record_bulk_action_for_account_ids(
                    session,
                    [account_id],
                    event_type="bulk_ai_profile_failed",
                    severity="error",
                    message="Не удалось применить AI профиль.",
                    raw={
                        "bulk_request_id": request_id,
                        "status": "failed",
                        "error": error_text,
                        "index": index + 1,
                        "total": total,
                    },
                )

            if index + 1 < total:
                delay = random.randint(payload.delay_seconds_min, payload.delay_seconds_max)
                await asyncio.sleep(delay)

        await record_bulk_action_for_account_ids(
            session,
            ordered_account_ids,
            event_type="bulk_ai_profiles_completed",
            message="Фоновая задача AI профилей завершена",
            raw={
                "bulk_request_id": request_id,
                "status": "completed",
                "account_count": len(ordered_account_ids),
                "completed": completed,
                "failed": failed,
            },
        )
        return {
            "status": "completed",
            "requested": len(ordered_account_ids),
            "completed": completed,
            "failed": len(failed),
            "failed_accounts": failed,
        }
    except Exception as exc:
        await session.rollback()
        if not ordered_account_ids:
            result = await session.execute(
                select(TelegramAccount.id).where(
                    TelegramAccount.workspace_id == workspace_id,
                    TelegramAccount.id.in_(account_ids),
                )
            )
            ordered_account_ids = [str(account_id) for account_id in result.scalars().all()]
        if ordered_account_ids:
            await record_bulk_action_for_account_ids(
                session,
                ordered_account_ids,
                event_type="bulk_ai_profiles_failed",
                severity="error",
                message="Фоновая задача AI профилей завершилась с ошибкой",
                raw={
                    "bulk_request_id": request_id,
                    "status": "failed",
                    "error": str(exc),
                },
            )
        raise


async def record_bulk_action_for_account_ids(
    session: AsyncSession,
    account_ids: list[str],
    *,
    event_type: str,
    message: str,
    raw: dict,
    severity: str = "info",
) -> None:
    for account_id in account_ids:
        session.add(
            AccountHealthEvent(
                account_id=account_id,
                event_type=event_type,
                severity=severity,
                message=message,
                raw_error=raw,
            )
        )
    await session.commit()


def build_ai_profile_avatar_prompt(profile: dict[str, str | bytes], style_prompt: str) -> str:
    gender = "woman" if profile.get("gender") == "female" else "man"
    name = " ".join(
        part for part in [str(profile.get("first_name") or ""), str(profile.get("last_name") or "")] if part
    ).strip()
    style = style_prompt.strip() or "естественный живой профиль"
    return (
        f"Photorealistic Telegram profile avatar of a fictional adult {gender}. "
        "Natural smartphone-style headshot, face clearly visible, shoulders visible, "
        "friendly neutral expression, modern casual clothes, soft daylight, simple real-life background. "
        f"Profile style: {style}. "
        f"Fictional name hint: {name or 'regular person'}. "
        "No text, no watermark, no logo, no celebrity, no public figure, "
        "no nudity, no child, no exaggerated retouching. "
        "Square composition, suitable for a small Telegram avatar."
    )


def add_ai_profile_photo_event(
    session: AsyncSession,
    account: TelegramAccount,
    request_id: str,
    photo_meta: dict,
) -> None:
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type="bulk_ai_profile_photo_generated",
            severity="info",
            message="Фото AI профиля сгенерировано и применено.",
            raw_error={
                "bulk_request_id": request_id,
                "generation": photo_meta,
            },
        )
    )


async def _get_workspace_account(session: AsyncSession, workspace_id: str, account_id: str) -> TelegramAccount:
    account = await session.get(TelegramAccount, account_id)
    if not account or account.workspace_id != workspace_id:
        raise RuntimeError("Telegram account not found")
    return account


async def _get_workspace_accounts(
    session: AsyncSession,
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
        raise RuntimeError("One or more Telegram accounts not found")
    return accounts
