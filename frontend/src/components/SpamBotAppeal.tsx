import { useCallback, useEffect, useState } from "react";
import { Send, RefreshCw } from "lucide-react";
import { apiRequest, storedToken, storedWorkspaceId } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

type Step = {
  id: string;
  status: string;
  command?: string;
  reply?: string;
  buttons?: string[];
  error?: string;
  classification?: string;
  automatic?: boolean;
  stage?: number;
  can_restart?: boolean;
};

export function SpamBotAppeal({
  accountId,
  accountName,
}: {
  accountId: string;
  accountName: string;
}) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<Step | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [available, setAvailable] = useState(false);
  const [choice, setChoice] = useState("");
  const [mode, setMode] = useState("automatic");
  const [automaticText, setAutomaticText] = useState("");
  const [text, setText] = useState(
    "Please review the restriction on my account and clarify its reason and duration. Thank you.",
  );
  const path = `/telegram-safety/${accountId}/spambot-appeal`;
  const waiting = step?.status === "queued" || step?.status === "processing";
  const options = () => ({
    token: storedToken() || "",
    workspaceId: storedWorkspaceId() || "",
  });
  const refresh = useCallback(async () => {
    try {
      const response = await apiRequest<{
        step: Step | null;
        worker_available: boolean;
        automatic_explanation: string;
      }>(path, {
        token: storedToken() || "",
        workspaceId: storedWorkspaceId() || "",
      });
      setStep(response.step);
      setAvailable(response.worker_available);
      setAutomaticText(response.automatic_explanation || "");
      setError("");
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Не удалось загрузить обращение",
      );
    }
  }, [path]);
  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);
  useEffect(() => {
    if (!open || !waiting) return;
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [open, waiting, refresh]);
  useEffect(() => {
    setChoice("");
    setConfirmed(false);
  }, [step?.id, step?.status]);
  async function send(
    action: "start" | "automatic" | "button" | "explanation",
  ) {
    setBusy(true);
    setError("");
    try {
      const response = await apiRequest<{ step: Step }>(path, {
        ...options(),
        method: "POST",
        body: {
          action,
          text:
            action === "automatic"
              ? automaticText
              : action === "start"
                ? ""
                : action === "button"
                  ? choice
                  : text,
          confirmed,
          previous_id: step?.id || null,
        },
      });
      setStep(response.step);
      setConfirmed(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось отправить запрос");
    } finally {
      setBusy(false);
    }
  }
  const hasButtons = Boolean(step?.buttons?.length);
  const canReply =
    step?.status === "reply_received" &&
    !step.automatic &&
    step.classification !== "clear";
  const action = !step
    ? mode === "automatic"
      ? "automatic"
      : "start"
    : hasButtons
      ? "button"
      : "explanation";
  const statusLabels: Record<string, string> = {
    queued: "В очереди",
    processing: "Обрабатывается",
    reply_received: "Получен ответ SpamBot",
    sent_waiting_reply: "Отправлено, свежий ответ не получен",
    unknown_delivery: "Доставка неизвестна — повторная отправка отключена",
    failed: "Не выполнено",
    flow_completed: "Сценарий завершён",
    automation_stopped: "Сценарий остановлен",
  };
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Send />
          Запросить пересмотр
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Пересмотр ограничения · {accountName}</DialogTitle>
        </DialogHeader>
        <div className="grid min-w-0 gap-4">
          {!step && (
            <label className="grid gap-2 text-sm">
              Режим обращения
              <select
                value={mode}
                onChange={(e) => {
                  setMode(e.target.value);
                  setConfirmed(false);
                }}
                disabled={busy}
                className="h-10 w-full min-w-0 rounded-md border bg-background px-2"
              >
                <option value="automatic">Автоматический сценарий</option>
                <option value="manual">Пошагово</option>
              </select>
            </label>
          )}
          {!step && mode === "automatic" && (
            <label className="grid gap-2 text-sm">
              Текст объяснения
              <textarea
                readOnly
                value={automaticText}
                className="min-h-48 w-full rounded-md border bg-background p-3"
              />
            </label>
          )}
          {step && (
            <div className="flex items-center justify-between gap-2">
              <span>{statusLabels[step.status] || step.status}</span>
              <Button
                size="icon"
                variant="ghost"
                title="Обновить ответ"
                aria-label="Обновить ответ"
                onClick={() => void refresh()}
              >
                <RefreshCw />
              </Button>
            </div>
          )}
          {step?.reply && (
            <blockquote className="whitespace-pre-wrap break-words border-l-2 pl-3 text-sm">
              {step.reply}
            </blockquote>
          )}
          {step?.classification === "clear" && (
            <p className="text-sm">
              SpamBot не обнаружил ограничений. Кампания не возобновлена.
            </p>
          )}
          {step?.status === "flow_completed" &&
            step.classification !== "clear" && (
              <p className="text-sm">
                Ответ бота приведён выше. Завершение сценария не означает снятие
                ограничения.
              </p>
            )}
          {step?.error && (
            <p role="alert" className="break-words text-sm text-destructive">
              {step.error}
            </p>
          )}
          {step?.can_restart && (
            <Button
              variant="outline"
              onClick={() => {
                setStep(null);
                setConfirmed(false);
              }}
            >
              Новое обращение
            </Button>
          )}
          {error && (
            <p role="alert" className="break-words text-sm text-destructive">
              {error}
            </p>
          )}
          {!available && (
            <p role="status" className="text-sm text-muted-foreground">
              Обработчик обращений недоступен.
            </p>
          )}
          {canReply && hasButtons && (
            <label className="grid gap-2 text-sm">
              Ответ
              <select
                value={choice}
                onChange={(e) => setChoice(e.target.value)}
                className="h-10 w-full min-w-0 rounded-md border bg-background px-2"
              >
                <option value="">Выберите ответ</option>
                {step?.buttons?.map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </select>
            </label>
          )}
          {canReply && !hasButtons && (
            <label className="grid gap-2 text-sm">
              Текст обращения
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                maxLength={2000}
                className="min-h-32 w-full rounded-md border bg-background p-3"
              />
            </label>
          )}
          {(!step || canReply) && (
            <>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                  className="mt-1"
                />
                <span>
                  {!step
                    ? mode === "automatic"
                      ? "Подтверждаю, что давно не входил в аккаунт и давно не писал в публичных группах. Разрешаю автоматическое оспаривание ограничения, подтверждающие ответы и отправку текста выше официальному SpamBot."
                      : "Разрешаю запрос /start официальному SpamBot от этого аккаунта."
                    : "Подтверждаю достоверность выбранного ответа и разрешаю отправить его официальному SpamBot."}
                </span>
              </label>
              <Button
                disabled={
                  busy ||
                  waiting ||
                  !available ||
                  !confirmed ||
                  (!step && mode === "automatic" && !automaticText) ||
                  (hasButtons && !choice) ||
                  (canReply && !hasButtons && text.trim().length < 10)
                }
                onClick={() => void send(action)}
              >
                <Send />
                {!step
                  ? mode === "automatic"
                    ? "Запустить сценарий"
                    : "Начать обращение"
                  : "Отправить ответ"}
              </Button>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
