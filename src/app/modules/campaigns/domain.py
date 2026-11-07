from dataclasses import dataclass
from typing import Any

RUN_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
RUN_ACTIVE_STATUSES = {"pending", "running", "paused", "stopping"}
RUN_TICKABLE_STATUSES = {"pending", "running", "stopping"}

PROGRESS_COUNTER_KEYS = {
    "sources_checked",
    "sources_skipped",
    "messages_seen",
    "new_posts_found",
    "duplicate_posts_skipped",
    "comments_generated",
    "comments_sent",
    "comments_drafted",
    "comments_failed",
    "actions_created",
    "successful_actions",
    "cursors_initialized",
    "messages_scored",
    "messages_selected",
    "replies_generated",
    "replies_sent",
    "replies_drafted",
    "replies_failed",
    "broadcast_targets",
    "broadcast_sent",
    "broadcast_failed",
    "reaction_targets",
    "reactions_planned",
    "reactions_sent",
    "reactions_failed",
    "reactions_skipped_probability",
    "reactions_skipped_duplicate",
    "reactions_skipped_daily_limit",
    "reaction_cursors_initialized",
}


@dataclass
class CommentTickSummary:
    sources_checked: int = 0
    sources_skipped: int = 0
    messages_seen: int = 0
    new_posts_found: int = 0
    duplicate_posts_skipped: int = 0
    comments_generated: int = 0
    comments_sent: int = 0
    comments_drafted: int = 0
    comments_failed: int = 0
    actions_created: int = 0
    successful_actions: int = 0
    cursors_initialized: int = 0
    stop_reason: str | None = None

    def as_progress(self) -> dict[str, Any]:
        return {
            "sources_checked": self.sources_checked,
            "sources_skipped": self.sources_skipped,
            "messages_seen": self.messages_seen,
            "new_posts_found": self.new_posts_found,
            "duplicate_posts_skipped": self.duplicate_posts_skipped,
            "comments_generated": self.comments_generated,
            "comments_sent": self.comments_sent,
            "comments_drafted": self.comments_drafted,
            "comments_failed": self.comments_failed,
            "actions_created": self.actions_created,
            "successful_actions": self.successful_actions,
            "cursors_initialized": self.cursors_initialized,
            "stop_reason": self.stop_reason,
        }


@dataclass
class ChatTickSummary:
    sources_checked: int = 0
    messages_seen: int = 0
    messages_scored: int = 0
    messages_selected: int = 0
    replies_generated: int = 0
    replies_sent: int = 0
    replies_drafted: int = 0
    replies_failed: int = 0
    broadcast_targets: int = 0
    broadcast_sent: int = 0
    broadcast_failed: int = 0
    successful_actions: int = 0
    actions_created: int = 0
    stop_reason: str | None = None

    def as_progress(self) -> dict[str, Any]:
        return {
            "sources_checked": self.sources_checked,
            "messages_seen": self.messages_seen,
            "messages_scored": self.messages_scored,
            "messages_selected": self.messages_selected,
            "replies_generated": self.replies_generated,
            "replies_sent": self.replies_sent,
            "replies_drafted": self.replies_drafted,
            "replies_failed": self.replies_failed,
            "broadcast_targets": self.broadcast_targets,
            "broadcast_sent": self.broadcast_sent,
            "broadcast_failed": self.broadcast_failed,
            "successful_actions": self.successful_actions,
            "actions_created": self.actions_created,
            "stop_reason": self.stop_reason,
        }
