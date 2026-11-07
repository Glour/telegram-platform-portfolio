export function clampNumber(
  value: FormDataEntryValue | null,
  min: number,
  max: number,
  fallback: number,
) {
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(numberValue)));
}

export function splitRefs(value: string) {
  return Array.from(
    new Set(
      value
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

export function dialogKindRu(value: string) {
  const labels: Record<string, string> = {
    channel: "канал",
    group: "группа",
    user: "пользователь",
    dialog: "диалог",
  };
  return labels[value] || value;
}

export function formatDateTimeRu(
  value: string | null | undefined,
  fallback = "нет",
) {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatTimeOnlyRu(
  value: string | null | undefined,
  fallback = "--:--:--",
) {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function formatRestrictionUntilMoscow(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const part = (type: string) =>
    parts.find((item) => item.type === type)?.value || "";
  return {
    date: `${part("day")}.${part("month")}.${part("year")}`,
    time: `${part("hour")}:${part("minute")} МСК`,
  };
}
