export function statusLabel(status: string) {
  const labels: Record<string, string> = {
    active: "Активен",
    archived: "Архив",
    available: "Доступен",
    cancelled: "Отменен",
    completed: "Завершен",
    deleted: "Удален",
    draft: "Черновик",
    failed: "Ошибка",
    frozen: "Заморожен",
    quarantined: "Карантин",
    blocked: "Заблокирован",
    blacklisted: "Черный список",
    spam_blocked: "Спам-блок",
    generated: "Сгенерирован",
    invalid: "Невалидный",
    invalid_account_banned: "Аккаунт забанен",
    invalid_comments_closed: "Комментарии закрыты",
    invalid_join_request: "Нужна заявка",
    invalid_not_channel: "Не канал",
    invalid_not_found: "Не найден",
    invalid_private: "Приватный",
    invalid_write_forbidden: "Запрет записи",
    new: "Новый",
    not_started: "Не начат",
    pending: "Ожидает",
    pending_check: "Ожидает проверки",
    ready: "Готов",
    paused: "Пауза",
    running: "Запущен",
    scheduled: "Запланирован",
    sent: "Отправлен",
    stopped: "Остановлен",
    succeeded: "Выполнено",
    validated: "Проверен",
    warming: "Прогревается",
  };
  return labels[status] || status;
}

export function isCampaignStoppable(status: string) {
  return ["pending", "running"].includes(status);
}

export function campaignEventLabel(value: string) {
  const labels: Record<string, string> = {
    run_created: "Запуск создан",
    run_pending: "Запуск ожидает",
    tick_started: "Проверка источников началась",
    runner_waiting: "Ожидаем новые посты",
    tick_completed: "Проверка завершена",
    run_completed: "Запуск завершен",
    run_cancelled: "Запуск остановлен",
    run_failed: "Ошибка запуска",
    manual_stop_requested: "Остановка запрошена",
    live_listener_active: "Live-режим активен",
    live_neuro_message_skipped: "Сообщение пропущено",
    live_neuro_reply_created: "Ответ создан",
    live_neuro_reply_failed: "Ошибка ответа",
  };
  return labels[value] || value;
}

export function campaignEventMessage(message: string) {
  const labels: Record<string, string> = {
    "Campaign run failed during one bounded tick.":
      "Запуск упал во время обработки источников.",
    "Campaign run finished one bounded tick.":
      "Фоновая проверка источников завершена.",
    "Campaign run completed.": "Запуск завершен.",
    "Campaign run reached a terminal stop condition.":
      "Запуск дошел до условия остановки.",
  };
  return labels[message] || message;
}

export function campaignErrorText(
  message?: string | null,
  code?: string | null,
) {
  const text = String(message || "").trim();
  const errorCode = String(code || "").trim();
  const normalized = text.toLowerCase();
  if (
    normalized.includes("source comments are not enabled") ||
    errorCode === "COMMENTS_CLOSED"
  ) {
    return (
      "У источника нет доступного обсуждения для комментариев. Мы пробуем вступить в обсуждение автоматически; " +
      "если вступить некуда или Telegram не дает доступ, источник уходит в архив и удаляется из активных кампаний."
    );
  }
  if (normalized.includes("_deactivate_source_in_campaign")) {
    return (
      "Внутренняя ошибка при обработке недоступного источника. Исправлено: " +
      "теперь такой источник будет архивироваться/пропускаться без падения всей кампании."
    );
  }
  return [errorCode, text].filter(Boolean).join(": ");
}

export function campaignSkipReasonLabel(value: string) {
  const labels: Record<string, string> = {
    outgoing_message: "это исходящее сообщение аккаунта",
    duplicate: "ответ уже был создан",
    empty_or_outgoing: "пустое или исходящее сообщение",
    too_short: "сообщение слишком короткое",
    too_long: "сообщение слишком длинное",
    command: "похоже на команду",
    promotional: "похоже на рекламу",
    keyword_mismatch: "нет ключевого слова кампании",
  };
  return labels[value] || value;
}
