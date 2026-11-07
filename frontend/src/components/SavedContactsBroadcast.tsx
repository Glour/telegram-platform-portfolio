import * as React from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { ApiOptions } from "@/lib/api";

type Account = {
  id: string;
  username: string | null;
  display_name: string | null;
  status: string;
};
type Contact = {
  user_id: string;
  username: string | null;
  first_name: string;
  last_name: string;
  suggested_name: string;
  eligible: boolean;
  reason: string | null;
};
type Selection = { user_id: string; name: string; use_fallback: boolean };
type ContactsResponse = {
  contacts_snapshot_id: string;
  sender_username: string;
  caption_limit: number;
  pilot_contact_found: boolean;
  pilot_only: boolean;
  contacts: Contact[];
};
type Preview = {
  preview_id: string;
  can_start: boolean;
  pilot_only: boolean;
  recipients: {
    user_id: string;
    username: string | null;
    name: string;
    caption: string;
  }[];
};
type Timing = {
  delay_min: number;
  delay_max: number;
  pause_every_min: number;
  pause_every_max: number;
  pause_min: number;
  pause_max: number;
  daily_limit: number;
};
const DEFAULT_TIMING: Timing = {
  delay_min: 4,
  delay_max: 7,
  pause_every_min: 90,
  pause_every_max: 120,
  pause_min: 1,
  pause_max: 8,
  daily_limit: 1000,
};
function TimingFields({
  value,
  onChange,
  disabled = false,
}: {
  value: Timing;
  onChange: (value: Timing) => void;
  disabled?: boolean;
}) {
  const fields: [keyof Timing, string][] = [
    ["delay_min", "Между сообщениями: от, сек."],
    ["delay_max", "Между сообщениями: до, сек."],
    ["pause_every_min", "Доп. пауза каждые: от, сек."],
    ["pause_every_max", "Доп. пауза каждые: до, сек."],
    ["pause_min", "Доп. пауза длится: от, сек."],
    ["pause_max", "Доп. пауза длится: до, сек."],
    ["daily_limit", "Суточный лимит аккаунта"],
  ];
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {fields.map(([key, label]) => (
        <label key={key} className="grid gap-2">
          {label}
          <Input
            type="number"
            min={key.startsWith("delay") ? 4 : 1}
            max={key === "daily_limit" ? 10000 : 3600}
            disabled={disabled}
            value={value[key]}
            onChange={(e) =>
              onChange({ ...value, [key]: Number(e.target.value) })
            }
          />
        </label>
      ))}
    </div>
  );
}
type Status = {
  name: string;
  exclusions?: string;
  timing: Partial<Timing>;
  recipient_count: number;
  progress: { next_send_at?: string };
  status: string;
  error: string | null;
  report: { sent?: number; outgoing_verified?: number; unknown?: number };
  actions: {
    recipient_id: string;
    recipient: string;
    caption: string;
    status: string;
    username: string | null;
    message_id: string | null;
    outgoing_verified: boolean;
    error: string | null;
  }[];
};
type Campaign = {
  id: string;
  name: string;
  status: string;
  config: Record<string, unknown>;
};
const BODY =
  "С октября планируется повышение цен на массаж, если Вы планируете приобрести абонемент, то сегодня последний день для фиксации стоимости массажей по ценам сегодняшнего дня💜";
function firstName(value: string) {
  return value.match(/\p{L}+(?:[-‐‑]\p{L}+)*/u)?.[0] ?? "";
}
function normalizeCommaSpacing(value: string) {
  return value.replace(/,[^\S\r\n]*(?=\S)/g, ", ");
}
const statusNames: Record<string, string> = {
  draft: "Черновик",
  prepared: "Подготовлено",
  queued: "В очереди",
  pending: "Ожидает отправки",
  running: "Выполняется",
  sending: "Отправляется",
  succeeded: "Отправлено и проверено",
  sent_unverified: "Отправлено; проверка диалога не завершена",
  failed: "Ошибка",
  paused: "Приостановлено",
  unknown: "Результат неизвестен — нужна проверка диалога",
  completed: "Завершено",
  cancelled: "Остановлено",
  skipped: "Пропущено",
};

export function SavedContactsBroadcast({
  accounts,
  campaigns,
  request,
  onChanged,
}: {
  accounts: Account[];
  campaigns: Campaign[];
  request: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onChanged: () => Promise<void>;
}) {
  const [accountId, setAccountId] = React.useState("");
  const [contacts, setContacts] = React.useState<ContactsResponse | null>(null);
  const [selected, setSelected] = React.useState<Record<string, Selection>>({});
  const [names, setNames] = React.useState<Record<string, string>>({});
  const [query, setQuery] = React.useState("");
  const [recipientLimit, setRecipientLimit] = React.useState(300);
  const [exclusions, setExclusions] = React.useState("");
  const [excludePrevious, setExcludePrevious] = React.useState(true);
  const [timing, setTiming] = React.useState<Timing>(DEFAULT_TIMING);
  const [runExclusions, setRunExclusions] = React.useState<string | null>(null);
  const [runTiming, setRunTiming] = React.useState<Timing | null>(null);
  const [exportText, setExportText] = React.useState<string | null>(null);
  const [text, setText] = React.useState(`{name}, доброе утро 🪷\n${BODY}`);
  const [file, setFile] = React.useState<File | null>(null);
  const [imageUrl, setImageUrl] = React.useState("");
  const [preview, setPreview] = React.useState<Preview | null>(null);
  const [draftId, setDraftId] = React.useState<string | null>(null);
  const [activeId, setActiveId] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<Status | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!file) {
      setImageUrl("");
      return;
    }
    const url = URL.createObjectURL(file);
    setImageUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  React.useEffect(() => {
    if (!activeId) return;
    setRunTiming(null);
    setRunExclusions(null);
    setExportText(null);
    let cancelled = false;
    async function poll() {
      try {
        const value = await request<Status>(
          `/contact-broadcasts/${activeId}/status`,
        );
        if (!cancelled) {
          setStatus(value);
          setRunTiming(
            (current) => current ?? { ...DEFAULT_TIMING, ...value.timing },
          );
          setRunExclusions((current) => current ?? value.exclusions ?? "");
        }
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      }
    }
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeId, request]);
  function invalidate() {
    setPreview(null);
    setDraftId(null);
    setNotice(null);
  }
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  async function loadContacts() {
    invalidate();
    setContacts(null);
    setSelected({});
    const result = await request<ContactsResponse>(
      `/contact-broadcasts/accounts/${accountId}/contacts`,
      { method: "POST", timeoutMs: 70000 },
    );
    setContacts(result);
    setNames(
      Object.fromEntries(
        result.contacts.map((c) => [c.user_id, c.suggested_name]),
      ),
    );
  }
  async function loadText(upload?: File) {
    if (!upload) return;
    if (upload.size > 64 * 1024)
      throw new Error("Текстовый файл должен быть не больше 64 КБ.");
    const contents = new TextDecoder("utf-8", { fatal: true }).decode(
      await upload.arrayBuffer(),
    );
    setText(normalizeCommaSpacing(contents.replace(/^\uFEFF/, "")));
    invalidate();
  }
  async function buildPreview() {
    if (!file || !contacts)
      throw new Error("Получите контакты и загрузите картинку.");
    const normalizedText = normalizeCommaSpacing(text);
    setText(normalizedText);
    const form = new FormData();
    form.append("file", file);
    const media = await request<{ media_id: string }>(
      "/contact-broadcasts/media",
      { method: "POST", body: form },
    );
    const value = await request<Preview>("/contact-broadcasts/preview", {
      method: "POST",
      body: {
        account_id: accountId,
        contacts_snapshot_id: contacts.contacts_snapshot_id,
        media_id: media.media_id,
        recipient_limit: recipientLimit,
        exclusions,
        exclude_previous: excludePrevious,
        timing,
        message_text: normalizedText,
        recipients: Object.values(selected).map((row) => ({
          ...row,
          name: firstName(row.name),
        })),
      },
    });
    setPreview(value);
    setDraftId(null);
    setSelected(
      Object.fromEntries(
        value.recipients.map((row) => [
          row.user_id,
          { user_id: row.user_id, name: row.name, use_fallback: false },
        ]),
      ),
    );
    setNames((current) => ({
      ...current,
      ...Object.fromEntries(
        value.recipients.map((row) => [row.user_id, row.name]),
      ),
    }));
  }
  async function saveDraft() {
    if (!preview) return;
    const campaign = await request<{ id: string }>(
      "/contact-broadcasts/drafts",
      {
        method: "POST",
        body: {
          preview_id: preview.preview_id,
          name: "Промо-рассылка по контактам",
        },
      },
    );
    setDraftId(campaign.id);
    setNotice("Черновик сохранён. Сообщения ещё не отправлялись.");
    await onChanged();
  }
  async function confirmAndStart() {
    if (!preview?.can_start) return;
    const id =
      draftId ??
      (
        await request<{ id: string }>("/contact-broadcasts/drafts", {
          method: "POST",
          body: {
            preview_id: preview.preview_id,
            name: "Промо-рассылка по контактам",
          },
        })
      ).id;
    setDraftId(id);
    await start(id);
    setPreview(null);
  }
  async function start(id: string) {
    await request(`/contact-broadcasts/${id}/start`, { method: "POST" });
    setActiveId(id);
    setNotice("Запуск поставлен в очередь. Результат появится ниже.");
    await onChanged();
  }
  const filtered =
    contacts?.contacts.filter((c) =>
      `${c.username ?? ""} ${c.first_name} ${c.last_name}`
        .toLowerCase()
        .includes(query.toLowerCase()),
    ) ?? [];
  const savedCampaigns = campaigns.filter(
    (c) => c.config.recipient_mode === "saved_contacts",
  );
  const exclusionSet = new Set(
    exclusions.split(/[\s,;]+/).map((token) =>
      token
        .replace(/^(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me)\//i, "")
        .split("?")[0]
        .replace(/^id:/i, "")
        .replace(/^@|\/$/g, "")
        .toLowerCase(),
    ),
  );
  function excluded(c: Contact) {
    return (
      exclusionSet.has(c.user_id) ||
      (!!c.username && exclusionSet.has(c.username.toLowerCase()))
    );
  }
  const eligibleContacts =
    contacts?.contacts.filter((c) => c.eligible && !excluded(c)) ?? [];
  const allSelected =
    eligibleContacts.length > 0 &&
    eligibleContacts
      .slice(0, recipientLimit)
      .every((c) => !!selected[c.user_id]);
  function selection(c: Contact): Selection {
    return {
      user_id: c.user_id,
      name: names[c.user_id] ?? c.suggested_name,
      use_fallback: false,
    };
  }
  async function showExport(scope: "selected" | "sent", id: string) {
    const result = await request<{ text: string; count: number }>(
      `/contact-broadcasts/${id}/recipients?scope=${scope}`,
    );
    setExportText(result.text);
    try {
      await navigator.clipboard.writeText(result.text);
      setNotice(
        `Скопировано получателей: ${result.count}. Контакты без username указаны как id:123.`,
      );
    } catch {
      setNotice("Список готов ниже: его можно выделить и скопировать.");
    }
  }
  async function campaignAction(actionName: "pause" | "resume") {
    if (!activeId) return;
    await request(`/contact-broadcasts/${activeId}/${actionName}`, {
      method: "POST",
    });
    setStatus(await request<Status>(`/contact-broadcasts/${activeId}/status`));
    await onChanged();
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Промо-рассылка по сохранённым контактам</CardTitle>
        <CardDescription>
          Выберите аккаунт и получателей. В обращение подставляется только
          первое имя из контактов этого Telegram-аккаунта. Фамилии, номера и
          эмодзи отсекаются.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <p
            role="alert"
            className="rounded-lg border border-destructive p-3 text-destructive"
          >
            {error}
          </p>
        )}
        {notice && (
          <p role="status" className="rounded-lg bg-muted p-3">
            {notice}
          </p>
        )}
        <fieldset disabled={busy} className="space-y-4 disabled:opacity-70">
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid min-w-60 gap-2">
              Аккаунт
              <select
                aria-label="Аккаунт отправителя"
                className="h-9 rounded-md border bg-background px-3"
                value={accountId}
                onChange={(e) => {
                  setAccountId(e.target.value);
                  setContacts(null);
                  setSelected({});
                  invalidate();
                }}
              >
                <option value="">Выберите аккаунт</option>
                {accounts
                  .filter((a) => !["archived", "deleted"].includes(a.status))
                  .map((a) => (
                    <option key={a.id} value={a.id}>
                      @{a.username ?? a.display_name ?? a.id}
                    </option>
                  ))}
              </select>
            </label>
            <Button
              type="button"
              disabled={!accountId || busy}
              onClick={() => void action(loadContacts)}
            >
              Получить контакты
            </Button>
          </div>
          <div className="grid gap-4 lg:grid-cols-[12rem_1fr]">
            <label className="grid content-start gap-2">
              Лимит контактов за запуск
              <Input
                aria-label="Лимит контактов за запуск"
                type="number"
                min={1}
                max={10000}
                value={recipientLimit}
                onChange={(e) => {
                  const limit = Number(e.target.value);
                  setRecipientLimit(limit);
                  if (limit > 0)
                    setSelected((current) =>
                      Object.fromEntries(
                        Object.entries(current).slice(0, limit),
                      ),
                    );
                  invalidate();
                }}
              />
            </label>
            <label className="grid gap-2">
              Исключить получателей
              <Textarea
                aria-label="Исключить получателей"
                rows={3}
                placeholder="@username, ссылка t.me/username или id:123 — каждый с новой строки"
                value={exclusions}
                onChange={(e) => {
                  setExclusions(e.target.value);
                  invalidate();
                }}
              />
            </label>
          </div>
          <label className="flex items-center gap-2">
            <Checkbox
              checked={excludePrevious}
              onCheckedChange={(checked) => {
                setExcludePrevious(checked === true);
                invalidate();
              }}
            />
            Исключать ранее отправленных в промо-кампаниях этого воркспейса
          </label>
          <div className="space-y-3 rounded-lg border p-3">
            <h3 className="font-medium">Задержки и лимиты</h3>
            <TimingFields
              value={timing}
              onChange={(value) => {
                setTiming(value);
                invalidate();
              }}
            />
            <p className="text-sm text-muted-foreground">
              При ограничении Telegram кампания остановится; FloodWait переносит
              следующую отправку. Задержки не гарантируют отсутствие блокировки.
            </p>
          </div>
          {contacts && (
            <div className="space-y-3 rounded-lg border p-3">
              <p>
                Получено контактов: {contacts.contacts.length}. Выбрано:{" "}
                {Object.keys(selected).length}. Подпись: до{" "}
                {contacts.caption_limit}.
              </p>
              {contacts.pilot_only && !contacts.pilot_contact_found && (
                <p className="text-destructive">
                  Настроенный тестовый получатель отсутствует среди доступных
                  контактов.
                </p>
              )}
              <label className="flex items-center gap-2">
                <Checkbox
                  aria-label="Выбрать все контакты"
                  disabled={!eligibleContacts.length}
                  checked={
                    allSelected
                      ? true
                      : Object.keys(selected).length
                        ? "indeterminate"
                        : false
                  }
                  onCheckedChange={(checked) => {
                    setSelected(
                      checked === true
                        ? Object.fromEntries(
                            eligibleContacts
                              .slice(0, recipientLimit)
                              .map((c) => [c.user_id, selection(c)]),
                          )
                        : {},
                    );
                    invalidate();
                  }}
                />
                Выбрать все контакты
              </label>
              <Input
                aria-label="Поиск контактов"
                placeholder="Поиск по имени или username"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <div className="max-h-80 space-y-2 overflow-auto">
                {filtered.map((c) => (
                  <div
                    key={c.user_id}
                    className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_18rem]"
                  >
                    <label className="flex items-center gap-2">
                      <Checkbox
                        disabled={
                          !c.eligible ||
                          excluded(c) ||
                          (!selected[c.user_id] &&
                            Object.keys(selected).length >= recipientLimit)
                        }
                        checked={!!selected[c.user_id]}
                        onCheckedChange={(checked) => {
                          setSelected((current) => {
                            const next = { ...current };
                            if (checked)
                              next[c.user_id] = {
                                user_id: c.user_id,
                                name: names[c.user_id] ?? c.suggested_name,
                                use_fallback: false,
                              };
                            else delete next[c.user_id];
                            return next;
                          });
                          invalidate();
                        }}
                      />
                      <span>
                        {[c.first_name, c.last_name]
                          .filter(Boolean)
                          .join(" ") || c.user_id}
                        {c.username && ` (@${c.username})`}
                        {c.reason && ` — ${c.reason}`}
                        {excluded(c) && " — исключён"}
                      </span>
                    </label>
                    <Input
                      aria-label={`Имя для ${c.username ?? c.user_id}`}
                      placeholder="Первое имя"
                      disabled={!c.eligible}
                      value={names[c.user_id] ?? ""}
                      onChange={(e) => {
                        setNames((current) => ({
                          ...current,
                          [c.user_id]: e.target.value,
                        }));
                        setSelected((current) =>
                          current[c.user_id]
                            ? {
                                ...current,
                                [c.user_id]: {
                                  ...current[c.user_id],
                                  name: e.target.value,
                                },
                              }
                            : current,
                        );
                        invalidate();
                      }}
                      onBlur={(e) => {
                        const name = firstName(e.target.value);
                        if (name !== e.target.value) {
                          setNames((current) => ({
                            ...current,
                            [c.user_id]: name,
                          }));
                          setSelected((current) =>
                            current[c.user_id]
                              ? {
                                  ...current,
                                  [c.user_id]: { ...current[c.user_id], name },
                                }
                              : current,
                          );
                          invalidate();
                        }
                      }}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="grid gap-4">
            <label className="grid gap-2">
              Текст с именем
              <Textarea
                rows={6}
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  invalidate();
                }}
              />
              <span className="text-xs text-muted-foreground">
                Подстановка: {"{name}"}. Для буквальных фигурных скобок
                используйте {"{{ и }}"}.
              </span>
            </label>
          </div>
          <div className="flex flex-wrap gap-4">
            <label className="grid gap-2">
              Загрузить текст (.txt, UTF-8)
              <Input
                type="file"
                accept=".txt,text/plain"
                onChange={(e) => {
                  const upload = e.target.files?.[0];
                  void action(() => loadText(upload));
                  e.target.value = "";
                }}
              />
            </label>
            <label className="grid gap-2">
              Картинка (JPEG/PNG, до 10 МБ)
              <Input
                type="file"
                accept="image/jpeg,image/png"
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  invalidate();
                }}
              />
            </label>
          </div>
          <Button
            type="button"
            disabled={
              !contacts ||
              !file ||
              !Object.keys(selected).length ||
              recipientLimit < 1 ||
              busy
            }
            onClick={() => void action(buildPreview)}
          >
            Предпросмотр
          </Button>
        </fieldset>
        {preview && (
          <div className="space-y-3 rounded-lg border p-4">
            <h3 className="font-medium">
              Подтверждение промо-рассылки: {preview.recipients.length}{" "}
              получателей
            </h3>
            <p>
              Проверьте получателей, имена, текст и картинку. После
              подтверждения начнётся отправка выбранным контактам.
            </p>
            {imageUrl && (
              <img
                src={imageUrl}
                alt="Картинка рассылки"
                className="max-h-80 max-w-full rounded-lg object-contain"
              />
            )}
            {preview.recipients.map((row) => (
              <div key={row.user_id} className="rounded-md bg-muted p-3">
                <p className="mb-2 font-medium">
                  {row.username ? `@${row.username}` : row.user_id}
                  {row.name && ` · ${row.name}`}
                </p>
                <p className="whitespace-pre-wrap">{row.caption}</p>
              </div>
            ))}
            {!preview.can_start && (
              <p className="text-muted-foreground">
                Можно сохранить черновик. Запуск доступен только для настроенных
                тестового аккаунта и получателя.
              </p>
            )}
          </div>
        )}
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {preview && (
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    const text = preview.recipients
                      .map((row) =>
                        row.username ? `@${row.username}` : `id:${row.user_id}`,
                      )
                      .join("\n");
                    setExportText(text);
                    try {
                      await navigator.clipboard.writeText(text);
                      setNotice("Список получателей скопирован.");
                    } catch {
                      setNotice("Список для копирования показан ниже.");
                    }
                  })
                }
              >
                Скопировать получателей запуска
              </Button>
            )}
            {preview && (
              <Button
                type="button"
                variant="secondary"
                disabled={busy || !!draftId}
                onClick={() => void action(saveDraft)}
              >
                Сохранить черновик
              </Button>
            )}
            <Button
              type="button"
              aria-describedby={!preview ? "promo-start-hint" : undefined}
              disabled={busy || !preview?.can_start}
              onClick={() => void action(confirmAndStart)}
            >
              Подтвердить и запустить промо-рассылку
            </Button>
          </div>
          {!preview && (
            <p id="promo-start-hint" className="text-sm text-muted-foreground">
              Для запуска сначала нажмите «Предпросмотр» и проверьте сообщение.
            </p>
          )}
        </div>
        {savedCampaigns.length > 0 && (
          <div className="space-y-2">
            <h3 className="font-medium">Сохранённые рассылки</h3>
            {savedCampaigns.map((c) => (
              <div
                key={c.id}
                className="flex flex-wrap items-center gap-2 rounded-lg border p-3"
              >
                <span className="mr-auto">
                  {c.name} · {statusNames[c.status] ?? c.status} ·{" "}
                  {String(c.config.recipient_count ?? "")} контактов
                </span>
                <Button
                  type="button"
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    setActiveId(c.id);
                    setStatus(null);
                  }}
                >
                  Посмотреть результат
                </Button>
                {c.status === "draft" && (
                  <Button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        setActiveId(c.id);
                        setStatus(
                          await request<Status>(
                            `/contact-broadcasts/${c.id}/status`,
                          ),
                        );
                      })
                    }
                  >
                    Проверить перед запуском
                  </Button>
                )}
              </div>
            ))}
          </div>
        )}
        {activeId && status && (
          <div className="space-y-2 rounded-lg border p-4">
            <p className="font-medium">
              {status.name ?? "Кампания"} · Результат:{" "}
              {statusNames[status.status] ?? status.status}
            </p>
            <p>
              Всего: {status.recipient_count ?? status.actions.length}.
              Осталось:{" "}
              {
                status.actions.filter((row) =>
                  ["prepared", "queued", "sending"].includes(row.status),
                ).length
              }
              .
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  void action(() => showExport("selected", activeId))
                }
              >
                Скопировать всех получателей
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => void action(() => showExport("sent", activeId))}
              >
                Скопировать отправленных
              </Button>
            </div>
            {status.progress?.next_send_at &&
              ["pending", "running"].includes(status.status) && (
                <p>
                  Следующая отправка не раньше:{" "}
                  {new Date(status.progress.next_send_at).toLocaleString(
                    "ru-RU",
                  )}
                  .
                </p>
              )}
            {status.status === "paused" && runTiming && (
              <div className="space-y-3">
                <h3>Настройки продолжения</h3>
                <label className="grid gap-2">
                  Исключить оставшихся получателей
                  <Textarea
                    aria-label="Исключить оставшихся получателей"
                    rows={3}
                    value={runExclusions ?? ""}
                    onChange={(e) => setRunExclusions(e.target.value)}
                  />
                </label>
                <TimingFields
                  value={runTiming}
                  onChange={setRunTiming}
                  disabled={busy}
                />
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy}
                  onClick={() =>
                    void action(async () => {
                      await request(
                        `/contact-broadcasts/${activeId}/settings`,
                        {
                          method: "PATCH",
                          body: {
                            timing: runTiming,
                            exclusions: runExclusions ?? "",
                          },
                        },
                      );
                      setStatus(
                        await request<Status>(
                          `/contact-broadcasts/${activeId}/status`,
                        ),
                      );
                      setNotice(
                        "Настройки сохранены. Можно продолжить кампанию.",
                      );
                    })
                  }
                >
                  Сохранить настройки кампании
                </Button>
                <Button
                  type="button"
                  disabled={busy}
                  onClick={() => void action(() => campaignAction("resume"))}
                >
                  Продолжить с места паузы
                </Button>
              </div>
            )}
            <p>
              Отправлено: {status.report.sent ?? 0}. Проверено в диалоге:{" "}
              {status.report.outgoing_verified ?? 0}. Неопределённый результат:{" "}
              {status.report.unknown ?? 0}.
            </p>
            {status.error && <p className="text-destructive">{status.error}</p>}
            <div className="max-h-96 space-y-2 overflow-auto">
              {status.actions.map((row) => (
                <div key={row.recipient_id} className="rounded-md bg-muted p-3">
                  <p>
                    {row.recipient} · {statusNames[row.status] ?? row.status}
                    {row.message_id && ` · ID ${row.message_id}`}
                  </p>
                  {row.error && <p className="text-destructive">{row.error}</p>}
                  <p className="mt-2 whitespace-pre-wrap">{row.caption}</p>
                </div>
              ))}
            </div>
            {status.status === "draft" && (
              <Button
                type="button"
                disabled={busy}
                onClick={() => void action(() => start(activeId))}
              >
                Подтвердить и запустить черновик
              </Button>
            )}
            {["pending", "running"].includes(status.status) && (
              <Button
                type="button"
                disabled={busy}
                onClick={() => void action(() => campaignAction("pause"))}
              >
                Приостановить кампанию
              </Button>
            )}
            {["pending", "running", "paused"].includes(status.status) && (
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    await request(`/contact-broadcasts/${activeId}/stop`, {
                      method: "POST",
                    });
                    setStatus(
                      await request<Status>(
                        `/contact-broadcasts/${activeId}/status`,
                      ),
                    );
                    await onChanged();
                  })
                }
              >
                Завершить без продолжения
              </Button>
            )}
          </div>
        )}
        {exportText !== null && (
          <label className="grid gap-2">
            Список получателей для копирования
            <Textarea
              aria-label="Список получателей для копирования"
              rows={6}
              readOnly
              value={exportText}
              onFocus={(e) => e.target.select()}
            />
          </label>
        )}
      </CardContent>
    </Card>
  );
}
