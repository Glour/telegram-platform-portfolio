"""Shared RPC taxonomy. A stop decision is separate from proof of a global ban."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ErrorRule:
    code: str
    category: str
    label: str
    meaning: str
    reason: str
    spam_signal: str = "none"
    stop_scope: str = "account"
    restriction_status: str | None = None
    automatic_retry: bool = False


ERROR_CATALOG = (
    ErrorRule(
        "UserBannedInChannelError",
        "write_restriction",
        "Подозрение на спамблок / запрет записи",
        "Запись в этом обсуждении запрещена. Исключить источник; "
        "глобальное ограничение аккаунта этим ответом не подтверждено.",
        "source_denied",
        "suspected",
        stop_scope="source",
    ),
    ErrorRule(
        "PeerFloodError",
        "spam_restriction",
        "Антиспам-ограничение Telegram",
        "Telegram отклонил действие антиспам-ошибкой PEER_FLOOD. Срок и причина не установлены.",
        "spam_restricted",
        "rpc_confirmed",
        restriction_status="limited",
    ),
    ErrorRule(
        "FloodWaitError",
        "rate_limit",
        "Лимит частоты запросов",
        "Сохранить точный срок ожидания Telegram. FLOOD_WAIT не доказывает спамблок.",
        "flood_wait",
    ),
    ErrorRule(
        "SlowModeWaitError",
        "rate_limit",
        "Медленный режим обсуждения",
        "Telegram требует выдержать интервал обсуждения. Это не спамблок.",
        "flood_wait",
    ),
    ErrorRule(
        "ChatWriteForbiddenError",
        "permissions",
        "Запись в чат запрещена",
        "Недостаточно права писать в этот чат; причина спамблока не доказана.",
        "source_denied",
        stop_scope="source",
    ),
    ErrorRule(
        "ChatAdminRequiredError",
        "permissions",
        "Нужны права администратора",
        "Операция требует дополнительных прав в чате.",
        "source_denied",
        stop_scope="source",
    ),
    ErrorRule(
        "ChatGuestSendForbiddenError",
        "membership",
        "Необходимо членство в обсуждении",
        "Гостевая запись запрещена. Не путать со спамблоком и не вступать автоматически после отказа.",
        "source_denied",
        stop_scope="source",
    ),
    ErrorRule(
        "ChannelPrivateError",
        "source_access",
        "Источник недоступен",
        "Нет доступа к каналу или обсуждению. Не доказывает ограничения всего аккаунта.",
        "source_denied",
        stop_scope="source",
    ),
    ErrorRule(
        "MsgIdInvalidError",
        "invalid_post",
        "Недействительный идентификатор сообщения",
        "Пост или идентификатор сообщения недоступен; смысл зависит от RPC метода.",
        "unknown_rpc",
    ),
    ErrorRule(
        "InviteRequestSentError",
        "approval_pending",
        "Заявка на вступление отправлена",
        "Это заявка, а не подтверждённое членство. Не повторять вступление.",
        "unknown_rpc",
        stop_scope="source",
    ),
    ErrorRule(
        "AuthKeyUnregisteredError",
        "session",
        "Ключ сессии недействителен",
        "Сессия не авторизована.",
        "session_invalid",
    ),
    ErrorRule(
        "AuthKeyDuplicatedError",
        "session",
        "Ключ сессии отозван",
        "Telegram отклонил дублированный ключ; это не диагноз спамблока.",
        "session_invalid",
    ),
    ErrorRule("SessionRevokedError", "session", "Сессия отозвана", "Авторизация сессии отозвана.", "session_invalid"),
    ErrorRule(
        "UserDeactivatedError",
        "account_disabled",
        "Аккаунт деактивирован",
        "Аккаунт недоступен; причина не установлена.",
        "session_invalid",
    ),
    ErrorRule(
        "UserDeactivatedBanError",
        "account_disabled",
        "Аккаунт заблокирован Telegram",
        "Telegram сообщил о блокировке аккаунта; причина и срок не установлены.",
        "session_invalid",
        restriction_status="frozen",
    ),
    ErrorRule(
        "ConnectionError",
        "transport",
        "Ошибка соединения",
        "Ошибка транспорта; не доказывает спамблок. При записи доставка может быть неизвестна.",
        "transport_error",
    ),
    ErrorRule(
        "TimeoutError",
        "transport",
        "Таймаут соединения",
        "Истёк срок ожидания; не повторять запись с неизвестной доставкой.",
        "transport_error",
    ),
    ErrorRule(
        "OSError", "transport", "Ошибка сети", "Сетевая ошибка; не доказывает ограничение Telegram.", "transport_error"
    ),
    ErrorRule(
        "UsernameNotOccupiedError",
        "source_lookup",
        "Источник не найден",
        "Указанный username не существует. Исключить этот источник, сохранив работу аккаунта.",
        "source_denied",
        stop_scope="source",
    ),
    ErrorRule(
        "UsernameInvalidError",
        "source_lookup",
        "Некорректный username источника",
        "Адрес источника некорректен. Ошибка не означает ограничение аккаунта.",
        "source_denied",
        stop_scope="source",
    ),
)


def _key(code: str) -> str:
    return code.removesuffix("Error").replace("_", "").upper()


def classify_error(code: str, method: str | None = None, *, distinct_targets: int = 1) -> dict:
    rule = next((row for row in ERROR_CATALOG if _key(row.code) == _key(code)), None)
    if rule is None:
        if "Flood" in code or "Wait" in code or code.startswith(("FLOOD_WAIT", "SLOWMODE_WAIT")):
            rule = ERROR_CATALOG[2]
        else:
            rule = ErrorRule(
                code,
                "unknown",
                "Неизвестная ошибка Telegram",
                "Остановить аккаунт для разбора; не выдумывать причину или срок.",
                "unknown_rpc",
            )
    result = {**asdict(rule), "raw_code": code, "method": method, "distinct_targets": distinct_targets}
    if rule.code == "UserBannedInChannelError" and distinct_targets > 1:
        result.update(spam_signal="strong_suspected", label="Повторный запрет записи — сильное подозрение на спамблок")
    if rule.code == "MsgIdInvalidError" and method == "GetDiscussionMessageRequest":
        result.update(reason="post_unavailable", stop_scope="post")
    if rule.code == "ChannelPrivateError" and method in {"GetHistoryRequest", "GetFullChannelRequest"}:
        result.update(stop_scope="source")
    return result
