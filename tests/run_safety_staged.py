"""Offline release check against a disposable database; never uses Telegram."""

import os

import pytest
import telethon
from sqlalchemy.engine import make_url

from app.core.config import get_settings

if __name__ == "__main__":
    test_url = make_url(get_settings().database_url).set(database="safety_transport_test_20261003")
    os.environ["TELEGRAM_SAFETY_TEST_DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    os.environ["TELEGRAM_SAFETY_TEST_DB_DISPOSABLE"] = "yes"
    print(f"Offline safety checks; Telethon {telethon.__version__}; disposable test database only.")
    raise SystemExit(
        pytest.main(
            [
                "-q",
                "--tb=short",
                "/safety/tests/test_telegram_safety_transport.py",
                "/safety/tests/test_telegram_safety_api.py",
                "/safety/tests/test_conservative_account_protection.py",
                "/safety/tests/test_scoped_safety_behavior.py",
                "/safety/tests/test_warmup_rest_safety.py",
                "/safety/tests/test_architecture.py",
                "/safety/tests/test_saved_contact_broadcasts.py",
                "/safety/tests/test_content_factory.py::test_publish_stops_after_first_telegram_connection_failure",
            ]
        )
    )
