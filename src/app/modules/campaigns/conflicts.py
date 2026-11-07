"""Campaign account scheduling rules."""

from collections.abc import Iterable


def campaign_account_conflict_keys(campaign_type: str, account_ids: Iterable[str]) -> set[tuple[str, str]]:
    """Identify accounts that cannot be scheduled twice for the same campaign type."""
    return {(campaign_type, account_id) for account_id in account_ids}
