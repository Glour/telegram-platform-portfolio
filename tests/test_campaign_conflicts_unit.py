from app.modules.campaigns.conflicts import campaign_account_conflict_keys


def test_conflicts_are_keyed_by_campaign_type_and_account() -> None:
    keys = campaign_account_conflict_keys("comments", ["one", "two", "one"])
    assert keys == {("comments", "one"), ("comments", "two")}
    assert keys.isdisjoint(campaign_account_conflict_keys("reactions", ["one"]))
    assert not campaign_account_conflict_keys("comments", [])
