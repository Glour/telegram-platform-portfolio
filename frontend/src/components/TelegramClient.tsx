import { useEffect, useRef, useState } from "react";
import { MessageCircle, RefreshCw, Send } from "lucide-react";
import { apiRequest, storedToken, storedWorkspaceId } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";

type Peer = { title: string; username?: string; peer: string; preview: string; unread: number; spambot: boolean };
type BotButton = { text: string; url?: string; action?: string; unsupported?: boolean };
type Message = { id: number; text: string; outgoing: boolean; date?: string; attachment?: string; links: { text: string; url: string }[]; buttons: BotButton[][] };

function errorMessage(error: unknown, reading = false) {
  let message = error instanceof Error ? error.message : "Не удалось выполнить действие";
  if (reading && /ConnectionError|ConnectionRefused|TimeoutError/.test(message))
    return "Подключение через прокси недоступно. Проверьте прокси или включите прямое подключение для этого окна.";
  try {
    const parsed = JSON.parse(message);
    const detail = parsed.detail || parsed;
    if (typeof detail.message === "string") message = detail.message;
  } catch { /* The API can also return a plain message. */ }
  return message;
}


export function TelegramClient({ accountId, accountName, restricted = false }: { accountId: string; accountName: string; restricted?: boolean }) {
  const [direct, setDirect] = useState(false);
  const directChoice = useRef(false);
  const [open, setOpen] = useState(false);
  const [dialogs, setDialogs] = useState<Peer[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<Peer | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [before, setBefore] = useState<number | null>(null);
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const historyElement = useRef<HTMLDivElement>(null);
  const previousHeight = useRef<number | null>(null);
  useEffect(() => {
    const element = historyElement.current;
    if (!element) return;
    element.scrollTop = previousHeight.current === null ? element.scrollHeight : element.scrollHeight - previousHeight.current;
    previousHeight.current = null;
  }, [messages]);
  const generation = useRef(0);
  const lock = useRef(false);
  const path = `/telegram-manual/${accountId}`;
  function request<T>(suffix: string, body: Record<string, unknown>) {
    return apiRequest<T>(`${path}/${suffix}`, { method: "POST", body: { ...body, direct: directChoice.current }, timeoutMs: 65000, token: storedToken() || "", workspaceId: storedWorkspaceId() || "" });
  }
  async function loadDialogs(more = false) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError("");
    const version = generation.current;
    try {
      const result = await request<{ dialogs: Peer[]; next_cursor: string | null }>("dialogs", { cursor: more ? cursor : null });
      if (version !== generation.current) return;
      setDialogs(old => more ? [...old, ...result.dialogs] : result.dialogs); setCursor(result.next_cursor);
    } catch (e) { if (version === generation.current) setError(errorMessage(e, true)); }
    finally { lock.current = false; setBusy(false); }
  }
  async function loadHistory(peer: Peer, more = false) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError("");
    const version = generation.current;
    try {
      const result = await request<{ messages: Message[]; next_before: number | null }>("history", { peer: peer.peer, before: more ? before : 0 });
      if (version !== generation.current) return;
      previousHeight.current = more ? historyElement.current?.scrollHeight || 0 : null;
      setSelected(peer); setMessages(old => more ? [...result.messages, ...old] : result.messages); setBefore(result.next_before);
    } catch (e) { if (version === generation.current) setError(errorMessage(e, true)); }
    finally { lock.current = false; setBusy(false); }
  }
  async function send(button?: string) {
    if (lock.current || !selected) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    const version = generation.current;
    const storageKey = `manual-telegram-send:${accountId}`;
    // Keep the same intent across network errors and reloads. Never automatically retry a write.
    let intent = sessionStorage.getItem(storageKey);
    if (!intent) { intent = crypto.randomUUID(); sessionStorage.setItem(storageKey, intent); }
    try {
      const result = await request<{ status: string; message?: string }>("send", { peer: selected.peer, text: button ? "" : text, button: button || null, request_id: intent });
      sessionStorage.removeItem(storageKey);
      if (version !== generation.current) return;
      setText(""); setNotice(result.message || "Telegram принял действие. Обновите сообщения, чтобы увидеть ответ.");
    } catch (e) { if (version === generation.current) setError(errorMessage(e)); }
    finally { lock.current = false; setBusy(false); }
  }
  function changeOpen(value: boolean) {
    generation.current += 1; setOpen(value);
    if (!value) { setMessages([]); setSelected(null); setDialogs([]); setText(""); setError(""); setNotice(""); setCursor(null); setBefore(null); }
    else { directChoice.current = false; setDirect(false); setSearch(""); void loadDialogs(); }
  }
  const displayed = dialogs.filter(p => `${p.title} ${p.username || ""}`.toLowerCase().includes(search.toLowerCase()));
  return <Dialog open={open} onOpenChange={changeOpen}>
    <DialogTrigger asChild><Button variant="outline" size="sm"><MessageCircle />Telegram</Button></DialogTrigger>
    <DialogContent className="flex h-[85vh] max-h-[900px] flex-col overflow-hidden sm:max-w-5xl">
      <DialogHeader><DialogTitle>Telegram · {accountName}</DialogTitle></DialogHeader>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={direct} disabled={busy}
        onChange={e => { directChoice.current = e.target.checked; setDirect(e.target.checked); setSelected(null); setMessages([]); setDialogs([]); setCursor(null); void loadDialogs(); }} />
        Подключаться напрямую, без прокси</label>
      {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
      {notice && <p role="status" className="text-sm">{notice}</p>}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 sm:grid-cols-[250px_1fr]">
        <aside className={`${selected ? "hidden sm:flex" : "flex"} min-h-0 flex-col gap-2`}>
          <div className="flex gap-1"><input aria-label="Поиск диалогов" placeholder="Поиск диалогов" value={search} onChange={e => setSearch(e.target.value)} className="min-w-0 flex-1 rounded border px-2 py-2" />
            <Button variant="ghost" size="icon" aria-label="Обновить диалоги" disabled={busy} onClick={() => void loadDialogs()}><RefreshCw /></Button></div>
          <div className="min-h-0 flex-1 overflow-y-auto rounded border">
            {displayed.map((peer, i) => <button key={`${peer.title}-${i}`} disabled={busy} onClick={() => { setText(""); setNotice(""); void loadHistory(peer); }} className={`block w-full border-b p-3 text-left ${selected === peer ? "bg-muted" : "hover:bg-muted/50"}`}>
              <div className="truncate font-medium">{peer.title || peer.username || "Диалог"}{peer.unread > 0 && <span className="ml-2 text-xs">{peer.unread}</span>}</div>
              <p className="truncate text-xs text-muted-foreground">{peer.preview}</p></button>)}
            {!busy && !error && !displayed.length && <p className="p-3 text-sm text-muted-foreground">Нет диалогов по запросу.</p>}
            {cursor && <Button variant="ghost" className="w-full" disabled={busy} onClick={() => void loadDialogs(true)}>Ещё диалоги</Button>}
          </div>
        </aside>
        <section className={`${selected ? "flex" : "hidden sm:flex"} min-h-0 min-w-0 flex-col gap-2`}>
          {selected ? <>
            <div className="flex items-center gap-2 border-b pb-2"><Button variant="ghost" className="sm:hidden" onClick={() => setSelected(null)}>Назад</Button><strong className="min-w-0 flex-1 truncate">{selected.title}</strong>
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void loadHistory(selected)}><RefreshCw />Обновить</Button></div>
            <div ref={historyElement} aria-label="История сообщений" className="min-h-0 flex-1 space-y-3 overflow-y-auto rounded bg-muted/30 p-3">
              {before && <Button variant="ghost" disabled={busy} onClick={() => void loadHistory(selected, true)}>Ранее</Button>}
              {messages.map(message => <article key={message.id} className={`max-w-[95%] rounded-xl border bg-background p-3 ${message.outgoing ? "ml-auto" : "mr-auto"}`}>
                <p className="whitespace-pre-wrap break-words text-sm">{message.text}</p>
                {message.attachment && <p className="text-xs text-muted-foreground">Вложение · {message.attachment}</p>}
                {message.links.map((link, i) => <a key={i} href={link.url} target="_blank" rel="noopener noreferrer" className="mt-2 block break-words text-sm text-primary underline">{link.text || "Открыть ссылку"} ↗</a>)}
                {message.buttons.map((row, i) => <div key={i} className="mt-2 flex flex-wrap gap-1">{row.map((button, j) => button.url ? <a key={j} href={button.url} target="_blank" rel="noopener noreferrer" className="rounded border px-3 py-1 text-sm underline">{button.text} ↗</a> : <Button key={j} variant="outline" size="sm" disabled={busy || (restricted && !selected.spambot) || button.unsupported || !button.action} onClick={() => void send(button.action)}>{button.text}</Button>)}</div>)}
                <p className="mt-1 text-right text-[11px] text-muted-foreground">{message.date ? new Date(message.date).toLocaleString() : ""}</p>
              </article>)}
              {!messages.length && !busy && <p className="text-sm text-muted-foreground">Сообщений нет.</p>}
            </div>
            {restricted && !selected.spambot ? <div className="flex flex-wrap items-center gap-2"><p className="text-sm text-muted-foreground">Аккаунт ограничен. Отправка доступна только в диалоге SpamBot.</p></div> :
              <form className="flex gap-2" onSubmit={e => { e.preventDefault(); void send(); }}><textarea aria-label="Сообщение" value={text} onChange={e => setText(e.target.value)} maxLength={4096} disabled={busy} className="max-h-32 min-h-16 min-w-0 flex-1 rounded border p-2" placeholder="Сообщение" /><Button type="submit" disabled={busy || !text.trim()}><Send />Отправить</Button></form>}
          </> : <p className="m-auto text-sm text-muted-foreground">Выберите диалог слева.</p>}
        </section>
      </div>
      {busy && <p role="status" className="text-xs text-muted-foreground">Загрузка…</p>}
    </DialogContent>
  </Dialog>;
}
