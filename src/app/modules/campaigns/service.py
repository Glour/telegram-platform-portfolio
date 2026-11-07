from app.modules.telegram_gateway.service import (
    fetch_chat_messages_with_account,
    react_to_message_with_account,
    real_telegram_enabled,
    send_chat_reply_with_account,
    send_comment_with_account,
    source_ref_from_fields,
)

from app.modules.ai.service import generate_chat_reply_text, generate_comment_text
from app.modules.campaigns import engine as _engine
from app.modules.campaigns.domain import (
    PROGRESS_COUNTER_KEYS,
    RUN_ACTIVE_STATUSES,
    RUN_TERMINAL_STATUSES,
    RUN_TICKABLE_STATUSES,
    ChatTickSummary,
    CommentTickSummary,
)
from app.modules.telegram_gateway.contracts import TelegramChatMessageInfo, TelegramGatewayError


def _sync_patchable_dependencies() -> None:
    _engine.generate_chat_reply_text = generate_chat_reply_text
    _engine.generate_comment_text = generate_comment_text
    _engine.real_telegram_enabled = real_telegram_enabled
    _engine.fetch_chat_messages_with_account = fetch_chat_messages_with_account
    _engine.react_to_message_with_account = react_to_message_with_account
    _engine.send_chat_reply_with_account = send_chat_reply_with_account
    _engine.send_comment_with_account = send_comment_with_account
    _engine.source_ref_from_fields = source_ref_from_fields


async def start_comment_posts_campaign(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.start_comment_posts_campaign(*args, **kwargs)


async def start_comment_posts_campaign_run(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.start_comment_posts_campaign_run(*args, **kwargs)


async def tick_comment_posts_campaign_run(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.tick_comment_posts_campaign_run(*args, **kwargs)


async def process_live_comment_message(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.process_live_comment_message(*args, **kwargs)


async def process_live_neuro_message(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.process_live_neuro_message(*args, **kwargs)


async def start_generic_campaign_run(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.start_generic_campaign_run(*args, **kwargs)


async def tick_generic_campaign_run(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.tick_generic_campaign_run(*args, **kwargs)


async def tick_active_campaign_runs(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.tick_active_campaign_runs(*args, **kwargs)


async def tick_active_comment_posts_campaign_runs(*args, **kwargs):
    _sync_patchable_dependencies()
    return await _engine.tick_active_comment_posts_campaign_runs(*args, **kwargs)


async def request_stop_campaign_run(*args, **kwargs):
    return await _engine.request_stop_campaign_run(*args, **kwargs)


async def build_campaign_comment_report(*args, **kwargs):
    return await _engine.build_campaign_comment_report(*args, **kwargs)


async def build_campaign_reaction_report(*args, **kwargs):
    return await _engine.build_campaign_reaction_report(*args, **kwargs)


def campaign_comment_report_csv(*args, **kwargs):
    return _engine.campaign_comment_report_csv(*args, **kwargs)


def campaign_reaction_report_csv(*args, **kwargs):
    return _engine.campaign_reaction_report_csv(*args, **kwargs)


def live_comment_delay_seconds(*args, **kwargs):
    return _engine.live_comment_delay_seconds(*args, **kwargs)


_account_is_actionable = _engine._account_is_actionable
_policy_auto_join_daily_limit = _engine._policy_auto_join_daily_limit
_policy_auto_join_interval_seconds = _engine._policy_auto_join_interval_seconds
_policy_auto_join_max_per_tick = _engine._policy_auto_join_max_per_tick
_should_ensure_source_membership = _engine._should_ensure_source_membership

__all__ = [
    "PROGRESS_COUNTER_KEYS",
    "RUN_ACTIVE_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "RUN_TICKABLE_STATUSES",
    "ChatTickSummary",
    "CommentTickSummary",
    "TelegramChatMessageInfo",
    "TelegramGatewayError",
    "build_campaign_comment_report",
    "build_campaign_reaction_report",
    "campaign_comment_report_csv",
    "campaign_reaction_report_csv",
    "fetch_chat_messages_with_account",
    "generate_chat_reply_text",
    "generate_comment_text",
    "live_comment_delay_seconds",
    "process_live_comment_message",
    "process_live_neuro_message",
    "real_telegram_enabled",
    "react_to_message_with_account",
    "request_stop_campaign_run",
    "send_chat_reply_with_account",
    "send_comment_with_account",
    "source_ref_from_fields",
    "start_comment_posts_campaign",
    "start_comment_posts_campaign_run",
    "start_generic_campaign_run",
    "tick_active_campaign_runs",
    "tick_active_comment_posts_campaign_runs",
    "tick_comment_posts_campaign_run",
    "tick_generic_campaign_run",
    "_account_is_actionable",
    "_policy_auto_join_daily_limit",
    "_policy_auto_join_interval_seconds",
    "_policy_auto_join_max_per_tick",
    "_should_ensure_source_membership",
]
