import * as React from "react";
import { apiRequest, storedToken, storedWorkspaceId } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Candidate = {
  id: string;
  phone?: string | null;
  username?: string | null;
  display_name?: string | null;
  status: string;
  can_select: boolean;
  ready: boolean;
  reason: string;
};
type Job = {
  id: string;
  source_id: string;
  kind: string;
  status: string;
  error_code?: string;
};
type Replacement = {
  status?: string;
  jobs?: Job[];
  wait_reason?: string | null;
  join_interval_seconds?: number;
  next_join_at?: string;
  resume_when_ready?: boolean;
};
const pendingStatuses = [
  "queued",
  "joining",
  "waiting_account",
  "needs_review",
];
const statusLabels: Record<string, string> = {
  queued: "Подготовка поставлена в очередь",
  joining: "Вступления выполняются",
  waiting_account: "Подготовка ожидает готовности аккаунта",
  needs_review: "Подготовка остановлена",
  ready: "Источники подготовлены",
  cancelled: "Подготовка отменена",
};

function readableReason(reason: string) {
  if (reason === "Ожидает допуска: admission_required")
    return "Нужен операторский допуск в разделе «Безопасность».";
  if (/^Ожидает допуска: [a-z_]+(?::.*)?$/.test(reason))
    return "Аккаунт приостановлен. Причина и допуск — в разделе «Безопасность».";
  const statuses: Record<string, string> = {
    invalid: "сессия невалидна",
    spam_blocked: "спам-блок",
    quarantined: "карантин",
    blocked: "заблокирован",
    frozen: "заморожен",
    banned: "заблокирован Telegram",
  };
  return reason.replace(
    /\b(invalid|spam_blocked|quarantined|blocked|frozen|banned)\b/g,
    (value) => statuses[value],
  );
}

export function CampaignAccountReplacement({
  campaignId,
  accountIds,
  accounts,
  sourceCount,
  initialReplacement,
  onReplace,
  onCancel,
  sourceCounts,
  sourceLabels,
}: {
  campaignId: string;
  accountIds: string[];
  accounts: {
    id: string;
    phone?: string | null;
    username?: string | null;
    display_name?: string | null;
  }[];
  sourceCount: number;
  initialReplacement: Replacement;
  onReplace: (payload: Record<string, unknown>) => Promise<void>;
  onCancel: () => Promise<void>;
  sourceCounts?: Record<string, unknown>;
  sourceLabels?: Record<string, string>;
}) {
  const [expanded, setExpanded] = React.useState(false);
  const [candidates, setCandidates] = React.useState<Candidate[]>([]);
  const [candidateRefresh, setCandidateRefresh] = React.useState(0);
  const [loading, setLoading] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [query, setQuery] = React.useState("");
  const [oldId, setOldId] = React.useState(accountIds[0] || "");
  const [newId, setNewId] = React.useState("");
  const [interval, setInterval] = React.useState("300");
  const [customMinutes, setCustomMinutes] = React.useState("5");
  const [resume, setResume] = React.useState(false);
  const [progress, setProgress] = React.useState(initialReplacement);
  const requestId = React.useRef<string | null>(null);
  React.useEffect(() => {
    setProgress(initialReplacement);
  }, [initialReplacement]);
  const accountIdsKey = JSON.stringify(accountIds);
  const stableAccountIds = React.useMemo(
    () => JSON.parse(accountIdsKey) as string[],
    [accountIdsKey],
  );
  React.useEffect(() => {
    if (!stableAccountIds.includes(oldId)) setOldId(stableAccountIds[0] || "");
  }, [stableAccountIds, oldId]);
  const pending = pendingStatuses.includes(progress.status || "");
  const uncertain = (progress.jobs || []).some(
    (j) => j.status === "attempting",
  );
  React.useEffect(() => {
    if (!pending) return;
    let disposed = false;
    const poll = async () => {
      try {
        const result = await apiRequest<Replacement>(
          `/campaigns/${campaignId}/account-replacement`,
          {
            token: storedToken() || undefined,
            workspaceId: storedWorkspaceId() || undefined,
          },
        );
        if (!disposed) setProgress(result);
      } catch {
        /* Keep confirmed progress; the explicit menu fetch reports API errors. */
      }
    };
    const timer = window.setInterval(() => {
      void poll();
    }, 15000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [campaignId, pending]);
  React.useEffect(() => {
    if (!expanded) return;
    let disposed = false;
    setLoading(true);
    setError(null);
    apiRequest<Candidate[]>(`/campaigns/${campaignId}/replacement-accounts`, {
      token: storedToken() || undefined,
      workspaceId: storedWorkspaceId() || undefined,
    })
      .then((result) => {
        if (!disposed) setCandidates(result);
      })
      .catch((err) => {
        if (!disposed)
          setError(
            err instanceof Error
              ? err.message
              : "Не удалось загрузить аккаунты",
          );
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [campaignId, expanded, stableAccountIds, candidateRefresh]);
  const filtered = candidates.filter((a) =>
    `${a.phone || ""} ${a.username || ""} ${a.display_name || ""} ${a.id} ${a.reason}`
      .toLocaleLowerCase()
      .includes(query.trim().toLocaleLowerCase()),
  );
  const selected = candidates.find((a) => a.id === newId);
  const selectedSources = sourceCounts?.[oldId];
  const preparationSourceCount = Array.isArray(selectedSources)
    ? selectedSources.length
    : sourceCount;
  const seconds =
    interval === "custom" ? Number(customMinutes) * 60 : Number(interval);
  const validInterval =
    Number.isInteger(seconds) && seconds >= 60 && seconds <= 86400;
  const joinedChannels = (progress.jobs || []).filter(
    (j) => j.kind === "channel" && j.status === "joined",
  ).length;
  const joinedDiscussions = (progress.jobs || []).filter(
    (j) => j.kind === "discussion" && j.status === "joined",
  ).length;
  const canReplace =
    !pending || (progress.status === "needs_review" && !uncertain);
  const label = (a: {
    phone?: string | null;
    display_name?: string | null;
    username?: string | null;
    id: string;
  }) =>
    a.phone ||
    a.display_name ||
    (a.username ? `@${a.username}` : a.id.slice(0, 8));
  async function submit() {
    setBusy(true);
    setError(null);
    requestId.current ||= crypto.randomUUID();
    try {
      await onReplace({
        old_account_id: oldId,
        new_account_id: newId,
        join_interval_seconds: seconds,
        resume_when_ready: resume,
        request_id: requestId.current,
      });
      requestId.current = null;
      setNewId("");
      setExpanded(false);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Не удалось заменить аккаунт",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className="grid gap-3 rounded-lg border bg-background p-3"
      data-testid="account-replacement"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-medium">
            Замена аккаунта с переносом источников
          </div>
          <p className="text-xs text-muted-foreground">
            Каналы, промпт, лимиты и история сохраняются. Вступления идут по
            очереди.
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={!canReplace || busy}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Скрыть выбор" : "Заменить аккаунт"}
        </Button>
      </div>
      {progress.status && (
        <div
          role="status"
          className="grid gap-1 rounded-md bg-muted/40 p-2 text-sm"
        >
          <span>{statusLabels[progress.status] || progress.status}</span>
          <span>
            Подтверждено: {joinedChannels} каналов, {joinedDiscussions}{" "}
            обсуждений. Осталось задач:{" "}
            {(progress.jobs || []).filter((j) => j.status !== "joined").length}.
          </span>
          {progress.join_interval_seconds && (
            <span>
              Интервал: {progress.join_interval_seconds / 60} мин.
              {progress.resume_when_ready
                ? " После подготовки — запуск."
                : " После подготовки — оставить на паузе."}
            </span>
          )}
          {progress.wait_reason && (
            <span className="text-amber-700 dark:text-amber-400">
              {readableReason(progress.wait_reason)}
            </span>
          )}
          {(progress.jobs || [])
            .filter((j) => j.error_code)
            .map((j) => (
              <span key={j.id} className="text-destructive">
                {j.kind === "discussion" ? "Обсуждение" : "Канал"}{" "}
                {sourceLabels?.[j.source_id] || j.source_id.slice(0, 8)}:{" "}
                {j.error_code}
              </span>
            ))}
        </div>
      )}
      {pending && !uncertain && (
        <Button
          type="button"
          variant="outline"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await onCancel();
            } catch (err) {
              setError(
                err instanceof Error
                  ? err.message
                  : "Не удалось отменить подготовку",
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          Отменить оставшуюся подготовку
        </Button>
      )}
      {expanded && (
        <div className="grid gap-3">
          {accountIds.length > 1 && (
            <label className="grid gap-1 text-sm">
              Какой аккаунт заменить
              <select
                value={oldId}
                onChange={(e) => {
                  setOldId(e.target.value);
                  requestId.current = null;
                }}
                className="rounded-md border bg-background p-2"
              >
                {accountIds.map((id) => (
                  <option key={id} value={id}>
                    {label(accounts.find((a) => a.id === id) || { id })}
                  </option>
                ))}
              </select>
            </label>
          )}
          <Input
            aria-label="Поиск аккаунта для замены"
            placeholder="Поиск по номеру, имени, username или статусу"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              В workspace: {candidates.length}. По поиску: {filtered.length}.
              Готовых: {candidates.filter((a) => a.ready).length}.
            </p>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={loading}
              onClick={() => setCandidateRefresh((v) => v + 1)}
            >
              Обновить список
            </Button>
          </div>
          <div
            className="h-64 min-h-0 overflow-y-auto overscroll-contain rounded-md border"
            role="radiogroup"
            aria-label="Новый аккаунт"
            data-testid="replacement-account-list"
          >
            {loading ? (
              <p className="p-3 text-sm">Загружаю аккаунты…</p>
            ) : (
              filtered.map((a) => (
                <button
                  type="button"
                  key={a.id}
                  role="radio"
                  aria-checked={newId === a.id}
                  disabled={!a.can_select || busy}
                  onClick={() => {
                    setNewId(a.id);
                    requestId.current = null;
                  }}
                  className={`flex w-full items-start gap-2 border-b p-3 text-left text-sm disabled:opacity-50 ${newId === a.id ? "bg-primary/10" : "hover:bg-muted"}`}
                >
                  <span aria-hidden="true">{newId === a.id ? "●" : "○"}</span>
                  <span className="min-w-0">
                    <span className="block truncate font-medium">
                      {label(a)}
                      {a.username ? ` · @${a.username}` : ""}
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {readableReason(a.reason)}
                    </span>
                  </span>
                </button>
              ))
            )}
            {!loading && !filtered.length && (
              <p className="p-3 text-sm">Аккаунты не найдены.</p>
            )}
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="grid gap-1 text-sm">
              Пауза между вступлениями
              <select
                value={interval}
                onChange={(e) => {
                  setInterval(e.target.value);
                  requestId.current = null;
                }}
                className="rounded-md border bg-background p-2"
              >
                <option value="60">1 минута</option>
                <option value="300">5 минут</option>
                <option value="600">10 минут</option>
                <option value="custom">Свой интервал</option>
              </select>
            </label>
            {interval === "custom" && (
              <label className="grid gap-1 text-sm">
                Интервал, минуты
                <Input
                  type="number"
                  min={1}
                  max={1440}
                  value={customMinutes}
                  onChange={(e) => {
                    setCustomMinutes(e.target.value);
                    requestId.current = null;
                  }}
                />
              </label>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            Ориентир для {preparationSourceCount} каналов:{" "}
            {validInterval
              ? Math.ceil(
                  (Math.max(0, preparationSourceCount - 1) * seconds) / 60,
                )
              : "—"}{" "}
            мин. Обязательные вступления в обсуждения также проходят с этой
            паузой. Ожидания Telegram могут увеличить срок.
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={resume}
              onChange={(e) => {
                setResume(e.target.checked);
                requestId.current = null;
              }}
            />
            Запустить кампанию после подготовки всех источников
          </label>
          {selected && !selected.ready && (
            <p className="rounded-md bg-amber-50 p-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
              {readableReason(selected.reason)} Можно закрепить источники
              сейчас; вступления начнутся, когда аккаунт будет готов.
              <a
                className="mt-1 block underline"
                href={`/dashboard?section=safety&account=${encodeURIComponent(selected.id)}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                Открыть допуск этого аккаунта
              </a>
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Этот шаг применяется отдельно от остальных несохранённых настроек
            формы. Кампания будет на паузе до завершения подготовки.
          </p>
          <Button
            type="button"
            disabled={
              busy ||
              !selected?.can_select ||
              !oldId ||
              !validInterval ||
              !canReplace
            }
            onClick={() => {
              void submit();
            }}
          >
            {busy ? "Сохраняю замену…" : "Заменить и подготовить источники"}
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
