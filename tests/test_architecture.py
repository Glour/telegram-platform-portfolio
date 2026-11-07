import ast
import re
from pathlib import Path

from app.core.config import Settings
from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "app"
SKIPPED_SEARCH_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "qa-artifacts",
    "artifacts",
    "data",
    "tgstat-browser-profile",
}
OLD_BRAND_PATTERNS = [
    re.compile(r"tg\.?\s*farm", re.IGNORECASE),
    re.compile("tg" + "farm", re.IGNORECASE),
    re.compile(r"telegram[-_\s]+farm", re.IGNORECASE),
]


def test_settings_defaults() -> None:
    settings = Settings()

    assert settings.api_v1_prefix == "/api/v1"
    assert settings.app_name


def test_fastapi_app_is_configured() -> None:
    routes = set(app.openapi()["paths"])

    assert "/health" in routes
    assert any(path.startswith("/api/v1") for path in routes)


def test_architecture_documentation_exists() -> None:
    architecture_doc = PROJECT_ROOT / "docs" / "architecture.md"

    assert architecture_doc.exists()
    text = architecture_doc.read_text(encoding="utf-8")
    assert "Modular monolith" in text
    assert "Gateway / adapter" in text
    assert "Repository" in text


def test_legacy_tg_farm_branding_is_removed() -> None:
    offenders: list[str] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or SKIPPED_SEARCH_PARTS.intersection(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in OLD_BRAND_PATTERNS:
            if pattern.search(text):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
                break

    assert offenders == []


def test_modules_do_not_import_other_modules_api_layers() -> None:
    offenders: list[str] = []
    for path in (SRC_ROOT / "modules").rglob("*.py"):
        if SKIPPED_SEARCH_PARTS.intersection(path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("app.modules.") and node.module.endswith(".api"):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)} imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.modules.") and alias.name.endswith(".api"):
                        offenders.append(f"{path.relative_to(PROJECT_ROOT)} imports {alias.name}")

    assert offenders == []


def test_external_telegram_sdk_usage_stays_at_integration_edges() -> None:
    allowed = {
        "src/app/modules/telegram_gateway/service.py",
        "src/app/modules/telegram_gateway/contacts.py",
        "src/app/modules/telegram_safety/service.py",
        # Migration allowlist: these still contain Telethon-specific glue and should move behind the gateway over time.
        "src/app/modules/parsing/service.py",
        "src/app/modules/proxies/service.py",
        "src/app/modules/telegram_accounts/service.py",
        "src/app/workers/live_comment_listener.py",
    }
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if SKIPPED_SEARCH_PARTS.intersection(path.parts):
            continue
        rel = str(path.relative_to(PROJECT_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_telethon = any(
            (
                isinstance(node, ast.Import)
                and any(alias.name == "telethon" or alias.name.startswith("telethon.") for alias in node.names)
            )
            or (isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("telethon"))
            for node in ast.walk(tree)
        )
        if imports_telethon and rel not in allowed:
            offenders.append(rel)

    assert offenders == []


def test_telegram_gateway_contracts_are_importable() -> None:
    from app.modules.telegram_gateway.contracts import TelegramGatewayError, TelegramSendResult
    from app.modules.telegram_gateway.ports import TelegramGatewayPort

    error = TelegramGatewayError("EXAMPLE", "Example")
    result = TelegramSendResult(tg_message_id="1", raw={})

    assert error.code == "EXAMPLE"
    assert result.tg_message_id == "1"
    assert TelegramGatewayPort


def test_campaign_service_facade_preserves_existing_imports() -> None:
    from app.modules.campaigns import service

    expected_exports = [
        "build_campaign_comment_report",
        "campaign_comment_report_csv",
        "generate_comment_text",
        "process_live_comment_message",
        "process_live_neuro_message",
        "request_stop_campaign_run",
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

    missing = [name for name in expected_exports if not hasattr(service, name)]

    assert missing == []
