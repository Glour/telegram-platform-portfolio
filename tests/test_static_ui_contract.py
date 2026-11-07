from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "test-ui" / "index.html"
APP_JS = ROOT / "test-ui" / "app.js"
DASHBOARD_APP = ROOT / "frontend" / "src" / "DashboardApp.tsx"


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)


class TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.text.append(stripped)


def html_ids() -> list[str]:
    parser = IdCollector()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return parser.ids


def visible_text() -> str:
    parser = TextCollector()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return "\n".join(parser.text)


def dashboard_content_factory_detail_tabs_block() -> str:
    source = DASHBOARD_APP.read_text(encoding="utf-8")
    tabs_start = source.index("<Tabs value={activeTab} onValueChange={setActiveTab}")
    overview_start = source.index('<TabsContent value="overview"', tabs_start)
    return source[tabs_start:overview_start]


def test_content_factory_detail_tabs_have_mobile_safe_scroll_and_height_contract() -> None:
    block = dashboard_content_factory_detail_tabs_block()

    assert "content-factory-detail-tabs-scroll" in block
    assert "overflow-x-auto" in block
    assert any(
        token in block
        for token in (
            "[&_[role=tab]]:h-9",
            "[&_[role=tab]]:h-10",
            "[&_[role=tab]]:min-h-9",
            "[&_[role=tab]]:min-h-10",
        )
    )


def test_static_dom_contract_has_no_missing_refs_or_duplicate_ids() -> None:
    ids = html_ids()
    html_id_set = set(ids)
    app_js = APP_JS.read_text(encoding="utf-8")
    patterns = [
        re.compile(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)'),
        re.compile(
            r'\b(?:setText|setClass|show|on|setButtonBusy|setAccountNotice|setSourceNotice|setLaunchNotice)\("([A-Za-z][A-Za-z0-9_-]*)"'
        ),
    ]

    refs = {match.group(1) for pattern in patterns for match in pattern.finditer(app_js)}
    missing = sorted(ref for ref in refs if ref not in html_id_set)
    duplicates = sorted({item for item in ids if ids.count(item) > 1})

    assert missing == [], f"missing_dom_refs={len(missing)} {missing}"
    assert duplicates == [], f"duplicate_ids={len(duplicates)} {duplicates}"


def test_primary_shell_labels_are_russian() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    text = visible_text()

    labels = {
        "dashboard": "Обзор",
        "accounts": "Аккаунты",
        "sources": "Источники",
        "neuro": "Запуск",
        "warmup": "Прогрев",
        "reports": "Отчеты",
        "settings": "Настройки",
    }

    for target, label in labels.items():
        assert re.search(rf'data-tab-target="{target}"[\s\S]*?<span>{label}</span>', html)
        assert re.search(rf'{target}: {{ eyebrow: "[^"]+", title: "{label}" }}', app_js)

    assert '<h1 id="pageTitle">Обзор</h1>' in html
    assert "<strong>Пульт управления</strong>" in html

    forbidden_primary_labels = [
        "Operator Console",
        "Dashboard",
        "Accounts",
        "Sources",
        "Neuro",
        "Reports",
        "Settings",
        "Backend",
        "Output",
        "History",
        "Event log",
        "Preflight",
        "Workspace API",
        "Command center",
        "Account health",
        "Campaign state",
    ]
    for label in forbidden_primary_labels:
        assert label not in text


def test_settings_keeps_api_endpoint_hidden_but_health_check_wired() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    text = visible_text()

    assert re.search(r'<input(?=[^>]*id="apiBase")(?=[^>]*type="hidden")[^>]*>', html)
    assert "API base" not in text
    assert "Адрес сервера не редактируется в кабинете." in text
    assert '<button class="secondary" id="checkServerBtn" type="button">Проверить сервер</button>' in html
    assert 'on("checkServerBtn", "click", checkServer)' in app_js
    assert "function apiRoot()" in app_js
    assert 'value("apiBase") || defaultApiBase()' in app_js
    assert 'on("apiBase", "change", () => localStorage.setItem("tf_api_base", value("apiBase")))' in app_js


def test_launch_preflight_contract_is_wired() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")

    assert 'id="launchNotice"' in html
    for label in ["Подключите аккаунт", "Проверьте источник", "Запустите кампанию", "Смотрите отчет"]:
        assert label in html
    assert 'Следующий шаг: подключите аккаунт во вкладке "Аккаунты". После этого проверьте источник.' in html
    assert re.search(r'<button[^>]*id="goAccountSetupBtn"[^>]*>\s*Добавить аккаунт\s*</button>', html)
    assert re.search(r'<button[^>]*id="goSourceSetupBtn"[^>]*>\s*Добавить источник\s*</button>', html)
    assert "function updateLaunchReadiness" in app_js
    assert "runBtn.disabled = state.launchBusy || !readiness.ready;" in app_js
    assert "setLaunchNotice(text, kind);" in app_js
    assert "launchPreflightMessage" in app_js
    assert "navigateToFirstMissingSetup" in app_js
    assert 'on("goAccountSetupBtn", "click", goAccountSetup)' in app_js
    assert 'on("goSourceSetupBtn", "click", goSourceSetup)' in app_js


def test_source_check_requires_account_before_api_work() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    source_check = app_js[app_js.index("async function handleSourceCheck()") : app_js.index("function resetAll()")]
    go_source = app_js[app_js.index("function goSourceSetup()") : app_js.index("function launchPreflightMessage")]
    route_helper = app_js[app_js.index("function routeToAccountForSourceCheck") : app_js.index("function setAuthMode")]

    assert (
        "Сначала подключите Telegram аккаунт. Он нужен, чтобы проверить комментарии канала. "
        "Канал останется в поле Источники."
    ) in app_js
    assert 'id="accountNotice" class="summary account-notice empty" role="alert"' in html
    assert "function sourceCheckAccountAvailable" in app_js
    assert "return Boolean(launchAccounts().length || selectedAccount() || state.accounts.length);" in app_js
    assert "routeToAccountForSourceCheck();" in source_check
    source_before_account_message = "Сначала подключите аккаунт. Он нужен, чтобы проверить источник."
    assert f'routeToAccountForSourceCheck("{source_before_account_message}");' in go_source
    assert go_source.index("if (!sourceCheckAccountAvailable())") < go_source.index('activateTab("sources");')
    assert source_check.index("if (!sourceCheckAccountAvailable())") < source_check.index(
        'setButtonBusy("sourceCheckBtn", true)'
    )
    assert source_check.index("if (!sourceCheckAccountAvailable())") < source_check.index("await ensureAuth()")
    assert source_check.index("if (!sourceCheckAccountAvailable())") < source_check.index("createAndCheckSource")
    assert 'setSummary(message, "bad");' in route_helper
    assert 'activateTab("accounts");' in route_helper
    assert 'setAccountNotice(message, "bad");' in route_helper
    assert 'focusAfterTab("sessionBlob");' in route_helper


def test_bulk_source_upload_controls_and_parser_are_wired() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")

    assert re.search(r"<textarea(?=[^>]*id=\"bulkSourceRefs\")", html)
    assert re.search(r'<button[^>]*id="bulkSourceBtn"[^>]*>\s*Добавить пачкой\s*</button>', html)
    assert "@channel, t.me/channel, https://t.me/channel" in html
    assert "bulk-source-box" in html

    parser = app_js[app_js.index("function parseBulkSourceRefs") : app_js.index("function sourcePayloadFromParsedRef")]
    assert "function parseTelegramSourceRef" in app_js
    assert "function parseBulkSourceRefs" in app_js
    assert 'String(text || "").split(/[\\s,;]+/)' in parser
    assert "const seen = new Set();" in parser
    assert "seen.has(parsed.key)" in parser
    assert "duplicateCount += 1" in parser
    assert "result.refs.push(parsed)" in parser


def test_bulk_source_upload_preflights_account_before_api_work() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    handler = app_js[app_js.index("async function handleBulkSourceUpload()") : app_js.index("function resetAll()")]
    bulk_helper_start = app_js.index("async function createOrReuseAndCheckBulkSource")
    bulk_helper_end = app_js.index("function formatBulkSourceSummary")
    helper = app_js[bulk_helper_start:bulk_helper_end]

    assert "BULK_SOURCE_ACCOUNT_REQUIRED_MESSAGE" in app_js
    assert "routeToAccountForSourceCheck(BULK_SOURCE_ACCOUNT_REQUIRED_MESSAGE);" in handler
    assert handler.index("if (!sourceCheckAccountAvailable())") < handler.index("parseBulkSourceRefs")
    assert handler.index("if (!sourceCheckAccountAvailable())") < handler.index("await ensureAuth()")
    assert handler.index("await ensureAuth()") < handler.index("createOrReuseAndCheckBulkSource(parsedRef)")
    assert 'api("/sources"' in helper
    assert 'api(`/sources/${source.id}/check-comments`, { method: "POST" })' in helper
    assert 'on("bulkSourceBtn", "click", handleBulkSourceUpload)' in app_js


def test_bulk_source_upload_dedupes_reuses_and_continues_after_failures() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    handler = app_js[app_js.index("async function handleBulkSourceUpload()") : app_js.index("function resetAll()")]
    bulk_helper_start = app_js.index("async function createOrReuseAndCheckBulkSource")
    bulk_helper_end = app_js.index("function formatBulkSourceSummary")
    helper = app_js[bulk_helper_start:bulk_helper_end]
    summary_start = app_js.index("function formatBulkSourceSummary")
    summary_end = app_js.index("function selectReadyLaunchSources")
    summary = app_js[summary_start:summary_end]

    assert "state.sources.find((source) => sourceMatchesParsedRef(source, parsedRef))" in helper
    assert 'source = await api("/sources"' in helper
    assert "created = true;" in helper
    assert "existing: Boolean(existing)" in helper
    assert "ready: Boolean(checked.comments_enabled)" in helper
    assert "duplicates: parsed.duplicateCount" in handler
    assert "failed: parsed.invalidCount" in handler
    assert "for (const parsedRef of parsed.refs)" in handler
    assert re.search(r"catch\s*{\s*stats\.failed \+= 1;\s*}", handler)
    assert "Готово:" in summary
    assert "Добавлено:" in summary
    assert "Уже были/дубликаты:" in summary
    assert "Ошибок:" in summary


def test_bulk_source_upload_selects_ready_sources_for_launch() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    handler = app_js[app_js.index("async function handleBulkSourceUpload()") : app_js.index("function resetAll()")]
    selector = app_js[app_js.index("function selectReadyLaunchSources") : app_js.index("async function parsePosts")]

    assert "readySourceIds.push(result.source.id)" in handler
    assert "await loadSavedEntities({ silent: true });" in handler
    assert "selectReadyLaunchSources(readySourceIds);" in handler
    assert "state.selectedSourceIds = Array.from(new Set([...state.selectedSourceIds, ...readyIds]));" in selector
    assert "persistLaunchSelections();" in selector
    assert "renderLaunchSourceChoices();" in selector
    assert "renderDashboardState({ updateLaunchNotice: true });" in selector
    assert 'activateTab("neuro");' in handler


def test_launch_supports_account_and_source_multi_selects() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    run_campaign = app_js[app_js.index("async function runCampaign") : app_js.index("async function runFlow()")]
    run_flow = app_js[app_js.index("async function runFlow()") : app_js.index("function normalizeError")]

    assert 'id="launchAccountChoices"' in html
    assert 'id="launchSourceChoices"' in html
    assert "Аккаунты для запуска" in html
    assert "Источники для запуска" in html
    assert 'name="runMode" value="comment_new_posts" checked' in html
    assert 'name="runMode" value="comment_recent_posts"' in html
    assert 'buildLaunchChoice("account_ids", account.id, accountTitle(account), "account")' in app_js
    assert 'buildLaunchChoice("source_ids", source.id, sourceTitle(source), "source")' in app_js
    assert "function launchAccounts" in app_js
    assert "function launchSources" in app_js
    assert "const accounts = launchAccounts();" in run_flow
    assert "const sources = launchSources();" in run_flow
    assert "async function runCampaign(accounts, sources, prompt)" in run_campaign
    assert "account_ids: accounts.map((account) => account.id)" in run_campaign
    assert "source_ids: sources.map((source) => source.id)" in run_campaign


def test_launch_missing_source_routes_to_sources_with_visible_alert() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = app_js.index("function navigateToFirstMissingSetup")
    end = app_js.index("function routeToAccountForSourceCheck")
    route_helper = app_js[start:end]

    assert 'id="sourceNotice" class="summary source-notice empty" role="alert"' in html
    assert 'activateTab("accounts");' in route_helper
    assert 'activateTab("sources");' in route_helper
    assert route_helper.index('activateTab("accounts");') < route_helper.index('activateTab("sources");')
    assert 'setAccountNotice(message, "bad");' in route_helper
    assert 'setSourceNotice(message, "bad");' in route_helper
    assert 'focusAfterTab("sourceRef");' in route_helper
    assert 'Не выбран источник. Проверьте источник во вкладке "Источники", затем запускайте кампанию.' in app_js


def test_run_flow_opens_reports_for_valid_run_and_stays_there_on_error() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    run_flow = app_js[app_js.index("async function runFlow()") : app_js.index("function normalizeError")]

    assert run_flow.index("if (!readiness.ready)") < run_flow.index('activateTab("reports");')
    missing_setup_pos = run_flow.index("navigateToFirstMissingSetup(readiness, message);")
    reports_pos = run_flow.index('activateTab("reports");')
    started_summary_pos = run_flow.index('setSummary("Запуск начался. Показываю ход проверки и результат.")')
    assert missing_setup_pos < reports_pos
    assert reports_pos < started_summary_pos
    assert 'const message = `Запуск остановлен: ${normalizeError(error.message || "")}`;' in run_flow
    assert run_flow.rindex('activateTab("reports");') < run_flow.rindex('setSummary(message, "bad");')


def test_reports_tab_has_client_work_report_and_csv_controls() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")

    for item_id in [
        "refreshWorkReportBtn",
        "downloadWorkReportCsvBtn",
        "workReportTotalCount",
        "workReportSentCount",
        "workReportDraftCount",
        "workReportFailedCount",
        "workReportChannelSummaries",
        "workReportTableBody",
    ]:
        assert f'id="{item_id}"' in html

    for label in ["Обновить отчет", "Скачать CSV", "Канал", "Дата и время", "Пост", "Комментарий", "Статус"]:
        assert label in html

    assert "workReport: null" in app_js
    assert 'lastCampaignId: localStorage.getItem("tf_last_campaign_id") || ""' in app_js
    assert "api(`/campaigns/${id}/comment-report`)" in app_js
    assert "fetchWithAuth(`/campaigns/${campaignId}/comment-report.csv`)" in app_js
    assert 'on("refreshWorkReportBtn", "click", () => loadWorkReport(state.lastCampaignId' in app_js
    assert '{ buttonId: "refreshWorkReportBtn" })' in app_js
    assert 'on("downloadWorkReportCsvBtn", "click", downloadWorkReportCsv)' in app_js


def test_account_import_routes_back_to_typed_source() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    start = app_js.index("async function handleAccountImport()")
    end = app_js.index("async function handleSourceCheck()")
    account_import = app_js[start:end]

    assert "Аккаунт готов. Теперь проверьте источник." in app_js
    assert 'const hasSourceRef = Boolean(value("sourceRef"));' in account_import
    assert "hasSourceRef ? SOURCE_READY_AFTER_ACCOUNT_MESSAGE" in account_import
    assert 'activateTab("sources");' in account_import
    assert 'focusAfterTab(hasSourceRef ? "sourceCheckBtn" : "sourceRef");' in account_import


def test_telegram_account_editor_controls_and_api_paths_are_wired() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    text = visible_text()

    for item_id in [
        "profileAccountId",
        "profileFirstName",
        "profileLastName",
        "profileUsername",
        "profileBio",
        "profilePhoto",
        "profileCurrentPassword",
        "profileNewPassword",
        "profilePasswordHint",
        "profile2faConfirmation",
        "loadProfileBtn",
        "saveProfileBtn",
        "uploadProfilePhotoBtn",
        "update2faBtn",
        "profileEditorNotice",
    ]:
        assert f'id="{item_id}"' in html

    for label in ["Редактор аккаунта", "Загрузить профиль", "Сохранить профиль", "Загрузить фото", "Обновить 2FA"]:
        assert label in text

    assert "profile-sensitive" in html
    assert "CHANGE 2FA" in html
    assert re.search(r'<input(?=[^>]*id="profileCurrentPassword")(?=[^>]*type="password")', html)
    assert re.search(r'<input(?=[^>]*id="profileNewPassword")(?=[^>]*type="password")', html)

    assert "function renderProfileAccountOptions" in app_js
    assert "async function loadAccountProfile" in app_js
    assert "async function saveAccountProfile" in app_js
    assert "async function uploadAccountProfilePhoto" in app_js
    assert "async function updateAccount2fa" in app_js
    assert "api(`/telegram-accounts/${accountId}/profile`)" in app_js
    assert 'api(`/telegram-accounts/${accountId}/profile`, { method: "PATCH"' in app_js
    assert (
        'fetchWithAuth(`/telegram-accounts/${accountId}/profile-photo`, { method: "POST", body: formData })' in app_js
    )
    assert 'api(`/telegram-accounts/${accountId}/2fa`, { method: "POST"' in app_js
    assert 'on("loadProfileBtn", "click", loadAccountProfile)' in app_js
    assert 'on("saveProfileBtn", "click", saveAccountProfile)' in app_js
    assert 'on("uploadProfilePhotoBtn", "click", uploadAccountProfilePhoto)' in app_js
    assert 'on("update2faBtn", "click", updateAccount2fa)' in app_js
    assert "tf_profile" not in app_js


def test_safe_v1_nav_has_proxies_then_campaigns_pages() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    nav = html[html.index('<nav class="side-nav"') : html.index("</nav>", html.index('<nav class="side-nav"'))]

    targets = re.findall(r'data-tab-target="([^"]+)"', nav)
    assert "accounts" in targets
    assert "proxies" in targets
    assert "campaigns" in targets
    assert targets[targets.index("accounts") + 1] == "proxies"
    assert targets[targets.index("proxies") + 1] == "campaigns"
    assert re.search(r'data-tab-target="proxies"[\s\S]*?<span>(?:Прокси|Proxies)</span>', nav)
    assert re.search(r'data-tab-target="campaigns"[\s\S]*?<span>(?:Кампании|Campaigns)</span>', nav)
    assert re.search(r'<section[^>]*id="tab-proxies"[^>]*data-tab-panel="proxies"', html)
    assert re.search(r'<section[^>]*id="tab-campaigns"[^>]*data-tab-panel="campaigns"', html)
    assert re.search(r'proxies: { eyebrow: "[^"]+", title: "(?:Прокси|Proxies)" }', app_js)
    assert re.search(r'campaigns: { eyebrow: "[^"]+", title: "(?:Кампании|Campaigns)" }', app_js)


def test_campaigns_page_has_list_edit_and_lifecycle_controls() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")

    for item_id in [
        "campaignsList",
        "campaignEditorForm",
        "campaignName",
        "campaignSourceIds",
        "campaignAccountIds",
        "campaignPromptTemplateId",
        "campaignLatestPostsPerSource",
        "campaignMode",
        "campaignPolicy",
        "refreshCampaignsBtn",
        "saveCampaignBtn",
        "stopCampaignBtn",
        "archiveCampaignBtn",
        "deleteCampaignBtn",
    ]:
        assert f'id="{item_id}"' in html

    assert "async function loadCampaigns" in app_js
    assert "async function saveCampaign" in app_js
    assert "async function stopCampaign" in app_js
    assert "async function archiveCampaign" in app_js
    assert "async function deleteCampaign" in app_js
    assert 'api("/campaigns")' in app_js
    assert re.search(r'api\(`/campaigns/\$\{[^}]+}`,\s*\{\s*method:\s*"PATCH"', app_js)
    assert re.search(r'api\(`/campaigns/\$\{[^}]+}/stop`,\s*\{\s*method:\s*"POST"', app_js)
    assert re.search(r'api\(`/campaigns/\$\{[^}]+}/archive`,\s*\{\s*method:\s*"POST"', app_js)
    assert re.search(r'api\(`/campaigns/\$\{[^}]+}`,\s*\{\s*method:\s*"DELETE"', app_js)


def test_proxies_page_has_create_list_check_and_assign_controls() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")

    for item_id in [
        "proxiesList",
        "proxyEditorForm",
        "proxyType",
        "proxyHost",
        "proxyPort",
        "proxyUsername",
        "proxyPassword",
        "proxyCountry",
        "proxyProvider",
        "proxyMaxAccounts",
        "proxyNote",
        "createProxyBtn",
        "updateProxyBtn",
        "refreshProxiesBtn",
        "checkProxyBtn",
        "deleteProxyBtn",
        "proxyAssignAccountId",
        "assignProxyBtn",
        "proxyAssignCampaignId",
        "assignProxyCampaignBtn",
        "proxyUsageBtn",
        "proxyUsageTarget",
    ]:
        assert f'id="{item_id}"' in html

    assert "async function loadProxies" in app_js
    assert "async function createProxy" in app_js
    assert "async function updateProxy" in app_js
    assert "async function deleteProxy" in app_js
    assert "async function checkProxy" in app_js
    assert "async function assignProxyToAccount" in app_js
    assert "async function assignProxyToCampaign" in app_js
    assert "async function loadProxyUsage" in app_js
    assert 'api("/proxies")' in app_js
    assert re.search(r'api\(`/proxies/\$\{[^}]+}`,\s*\{\s*method:\s*"PATCH"', app_js)
    assert re.search(r'api\(`/proxies/\$\{[^}]+}`,\s*\{\s*method:\s*"DELETE"', app_js)
    assert re.search(r'api\(`/proxies/\$\{[^}]+}/check`,\s*\{\s*method:\s*"POST"', app_js)
    assert re.search(r'api\(`/proxies/\$\{[^}]+}/assign-account`,\s*\{\s*method:\s*"POST"', app_js)
    assert re.search(r'api\(`/proxies/\$\{[^}]+}/assign-campaign`,\s*\{\s*method:\s*"POST"', app_js)
    assert re.search(r"api\(`/proxies/\$\{[^}]+}/usage`", app_js)


def test_profile_editor_has_ten_avatar_presets_and_upload_selected_control() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")

    assert len(re.findall(r"<button[^>]+data-avatar-preset=", html)) == 10
    assert 'id="uploadSelectedAvatarPresetBtn"' in html
    assert "selectedAvatarPreset" in app_js
    assert "function uploadSelectedAvatarPreset" in app_js
    assert 'on("uploadSelectedAvatarPresetBtn", "click", uploadSelectedAvatarPreset)' in app_js
    assert "data-avatar-preset" in app_js
    preset_upload_call = (
        'fetchWithAuth(`/telegram-accounts/${accountId}/profile-photo`, { method: "POST", body: formData })'
    )
    assert preset_upload_call in app_js


def test_warmup_page_has_plan_run_controls_and_safe_copy() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    text = visible_text().lower()

    for item_id in [
        "warmupPlanName",
        "warmupAccountChoices",
        "warmupScenario",
        "warmupPolicy",
        "createWarmupPlanBtn",
        "runWarmupPlanBtn",
        "warmupRunsList",
    ]:
        assert f'id="{item_id}"' in html

    assert "async function createWarmupPlan" in app_js
    assert "async function runWarmupPlan" in app_js
    assert 'api("/warmup/plans", { method: "POST"' in app_js
    assert re.search(r'api\(`/warmup/plans/\$\{[^}]+}/runs`,\s*\{\s*method:\s*"POST"', app_js)
    assert "manual" in text
    assert "черновой" in text
    assert "no hidden imitation" in text
