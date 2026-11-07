import * as React from "react";
import { Loader2Icon, PlayIcon, RefreshCwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, type TelegramSafetyState } from "@/lib/api";

function safetyError(cause: unknown): string {
  if (
    cause instanceof ApiError &&
    cause.payload &&
    typeof cause.payload === "object" &&
    "detail" in cause.payload
  ) {
    const detail = cause.payload.detail;
    if (
      detail &&
      typeof detail === "object" &&
      "message" in detail &&
      typeof detail.message === "string"
    ) {
      return detail.message;
    }
  }
  return cause instanceof Error ? cause.message : "Состояние недоступно";
}

export type TelegramSafetyActions = {
  onLoadSafetyState: (accountId: string) => Promise<TelegramSafetyState>;
  onResumeSafety: (accountId: string) => Promise<TelegramSafetyState>;
  onAdmitSafety: (
    accountId: string,
    checkedAt: string,
    evidence: string,
  ) => Promise<TelegramSafetyState>;
};

export function TelegramSafetyPanel({
  accountId,
  onLoadSafetyState,
  onResumeSafety,
  onAdmitSafety,
}: TelegramSafetyActions & { accountId: string }) {
  const [state, setState] = React.useState<TelegramSafetyState | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [resuming, setResuming] = React.useState(false);
  const [confirmed, setConfirmed] = React.useState(false);
  const [clearConfirmed, setClearConfirmed] = React.useState(false);
  const [evidence, setEvidence] = React.useState("");
  const [evidenceCheckedAt, setEvidenceCheckedAt] = React.useState("");
  const [refresh, setRefresh] = React.useState(0);
  const [now, setNow] = React.useState<number | null>(null);

  React.useEffect(() => {
    const updateClock = () => setNow(Date.now());
    updateClock();
    const timer = window.setInterval(updateClock, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  React.useEffect(() => {
    let active = true;
    setState(null);
    setConfirmed(false);
    setClearConfirmed(false);
    setEvidence("");
    setEvidenceCheckedAt("");
    setLoading(true);
    setError(null);
    void onLoadSafetyState(accountId)
      .then((result) => {
        if (active) setState(result);
      })
      .catch((cause: unknown) => {
        if (active) setError(safetyError(cause));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [accountId, onLoadSafetyState, refresh]);

  async function mutate(action: () => Promise<TelegramSafetyState>) {
    if (resuming) return;
    setResuming(true);
    setError(null);
    try {
      const result = await action();
      setState(result);
      setConfirmed(false);
      setClearConfirmed(false);
      setEvidence("");
      setEvidenceCheckedAt("");
    } catch (cause: unknown) {
      setError(safetyError(cause));
      setConfirmed(false);
      setClearConfirmed(false);
      // Re-read the authoritative hold after rejection, without retrying the mutation.
      try {
        setState(await onLoadSafetyState(accountId));
      } catch {
        setState(null);
      }
    } finally {
      setResuming(false);
    }
  }

  const checkedAt = Date.parse(state?.restriction_checked_at || "");
  const baselineClear =
    state?.restriction_status === "clear" &&
    Boolean(state.operator_approved_at) &&
    now !== null &&
    checkedAt <= now &&
    checkedAt >= now - 24 * 60 * 60 * 1000;
  const waitPassed =
    !state?.wait_until || (now !== null && Date.parse(state.wait_until) <= now);
  const operationPending = ["pending", "unknown_delivery"].includes(
    state?.last_operation?.outcome || "",
  );
  const canResume = Boolean(
    state?.manual_hold && baselineClear && waitPassed && !operationPending,
  );
  const evidenceTime = Date.parse(evidenceCheckedAt);
  const evidenceFresh =
    Number.isFinite(evidenceTime) &&
    now !== null &&
    evidenceTime <= now &&
    evidenceTime >= now - 24 * 60 * 60 * 1000;
  const restricted = ["limited", "blocked", "frozen", "spam_blocked"].includes(
    state?.restriction_status || "unknown",
  );
  const canAdmit = Boolean(
    state &&
    !restricted &&
    waitPassed &&
    !operationPending &&
    evidenceFresh &&
    evidence.trim() &&
    clearConfirmed,
  );
  const date = (value: string | null) =>
    value ? new Date(value).toLocaleString("ru-RU") : "Не указано";

  return (
    <section
      className="min-w-0 border-y py-4"
      aria-label="Безопасность Telegram"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Безопасность Telegram</h3>
        <Button
          size="icon"
          variant="ghost"
          title="Обновить безопасность"
          aria-label="Обновить безопасность"
          disabled={loading || resuming}
          onClick={() => setRefresh((value) => value + 1)}
        >
          {loading ? (
            <Loader2Icon className="size-4 animate-spin" />
          ) : (
            <RefreshCwIcon className="size-4" />
          )}
        </Button>
      </div>
      {state ? (
        <>
          {state.diagnosis ? (
            <p role="status" className="mt-2 text-sm">
              {state.diagnosis.raw_code}: {state.diagnosis.meaning}
            </p>
          ) : null}
          <dl className="mt-2 grid gap-3 text-sm sm:grid-cols-2">
            {[
              [
                "Глобальное выполнение",
                state.execution_enabled ? "Включено" : "Отключено",
              ],
              [
                "Ручной hold",
                state.manual_hold
                  ? "Ручная блокировка"
                  : "Ручная блокировка снята",
              ],
              [
                "Причина",
                state.diagnosis?.label || state.reason || "Не указана",
              ],
              ["Ограничения", state.restriction_status],
              ["Проверено", date(state.restriction_checked_at)],
              ["Ожидание до", date(state.wait_until)],
              ["Одобрено оператором", date(state.operator_approved_at)],
              [
                "Ресурсный лимит пилота: RPC / 24 ч",
                String(state.rpc_attempt_limit ?? 20),
              ],
              [
                "Исходящие попытки / 24 ч",
                String(state.outbound_attempt_limit ?? 1),
              ],
            ].map(([label, value]) => (
              <div key={label} className="min-w-0">
                <dt className="text-xs text-muted-foreground">{label}</dt>
                <dd className="break-words">{value}</dd>
              </div>
            ))}
          </dl>
          {state.last_operation ? (
            <p className="mt-2 break-words text-sm">
              Последняя операция: {state.last_operation.method} ·{" "}
              {state.last_operation.outcome}
              {state.last_operation.error_code
                ? ` · ${state.last_operation.error_code}`
                : ""}
              {state.last_operation.message_id
                ? ` · message_id: ${state.last_operation.message_id}`
                : ""}
            </p>
          ) : null}
          {state.manual_hold ? (
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <label className="flex min-w-0 items-center gap-2 text-sm">
                <Checkbox
                  checked={confirmed}
                  disabled={!canResume || resuming}
                  onCheckedChange={(value) => setConfirmed(value === true)}
                />
                Подтверждаю ручное возобновление
              </label>
              <Button
                size="sm"
                disabled={!canResume || !confirmed || resuming}
                onClick={() => {
                  if (confirmed && canResume)
                    void mutate(() => onResumeSafety(accountId));
                }}
              >
                {resuming ? (
                  <Loader2Icon className="size-4 animate-spin" />
                ) : (
                  <PlayIcon className="size-4" />
                )}
                Возобновить
              </Button>
            </div>
          ) : null}
          {state.manual_hold ? (
            <form
              className="mt-4 grid min-w-0 gap-3 border-t pt-3"
              onSubmit={(event) => {
                event.preventDefault();
                if (canAdmit && !resuming)
                  void mutate(() =>
                    onAdmitSafety(
                      accountId,
                      new Date(evidenceTime).toISOString(),
                      evidence.trim(),
                    ),
                  );
              }}
            >
              <h4 className="text-sm font-medium">Допуск оператора</h4>
              <label className="grid min-w-0 gap-1 text-sm">
                Свидетельство проверки ограничений
                <Textarea
                  value={evidence}
                  maxLength={10000}
                  required
                  disabled={resuming}
                  onChange={(event) => {
                    setEvidence(event.target.value);
                    setClearConfirmed(false);
                  }}
                />
              </label>
              <label className="grid min-w-0 gap-1 text-sm">
                Дата проверки (местное время)
                <Input
                  type="datetime-local"
                  value={evidenceCheckedAt}
                  required
                  disabled={resuming}
                  className="min-w-0 max-w-full"
                  onChange={(event) => {
                    setEvidenceCheckedAt(event.target.value);
                    setClearConfirmed(false);
                  }}
                />
              </label>
              <label className="flex min-w-0 items-start gap-2 text-sm">
                <Checkbox
                  checked={clearConfirmed}
                  disabled={resuming}
                  onCheckedChange={(value) => setClearConfirmed(value === true)}
                />
                Подтверждаю результат clear: ограничений нет
              </label>
              <Button
                type="submit"
                size="sm"
                className="justify-self-start"
                disabled={!canAdmit || resuming}
              >
                {resuming ? (
                  <Loader2Icon className="size-4 animate-spin" />
                ) : (
                  <PlayIcon className="size-4" />
                )}
                Допустить
              </Button>
            </form>
          ) : null}
        </>
      ) : loading ? (
        <p className="text-sm text-muted-foreground">Загрузка состояния</p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-2 break-words text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </section>
  );
}
