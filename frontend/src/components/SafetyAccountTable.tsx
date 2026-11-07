import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { TelegramSafetyPanel, type TelegramSafetyActions } from "@/components/TelegramSafetyPanel";

export type SafetyAccountRow = {
  id: string; phone: string | null; username: string | null; display_name: string | null;
  account_status: string; session_status: string; proxy_assigned: boolean; proxy_ready: boolean;
  proxy_status: string | null; permission: string; admission_basis: string | null;
  restriction_status: string; manual_hold: boolean; ready: boolean; problem: string | null;
  can_attest: boolean; attest_problem: string | null; can_pause: boolean;
};
export type SafetyAccountOverview = { accounts: SafetyAccountRow[]; can_manage: boolean; is_owner: boolean };
export type SafetyAccountTableActions = TelegramSafetyActions & {
  onLoadOverview: () => Promise<SafetyAccountOverview>;
  onOwnerAttest: (id: string, evidence: string) => Promise<unknown>;
  onPauseAccount: (id: string) => Promise<unknown>;
};
const permissionLabels: Record<string, string> = { allowed: "Разрешено", paused: "Приостановлено", required: "Нужен допуск" };
const restrictionLabels: Record<string, string> = { unknown: "Не проверено", clear: "Нет ограничений", limited: "Ограничен", blocked: "Заблокирован" };
const label = (a: SafetyAccountRow) => a.phone || a.display_name || a.username || a.id.slice(0, 8);

export function SafetyAccountTable(props: SafetyAccountTableActions) {
  const [data, setData] = React.useState<SafetyAccountOverview | null>(null);
  const [error, setError] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [busy, setBusy] = React.useState("");
  const [query, setQuery] = React.useState(() => new URLSearchParams(window.location.search).get("account") || "");
  const [filter, setFilter] = React.useState("all");
  const [page, setPage] = React.useState(0);
  const [attesting, setAttesting] = React.useState<SafetyAccountRow | null>(null);
  const [detail, setDetail] = React.useState<SafetyAccountRow | null>(null);
  const [note, setNote] = React.useState("");
  const load = props.onLoadOverview;
  const reload = React.useCallback(async () => {
    setLoading(true);
    try { setData(await load()); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось загрузить аккаунты"); }
    finally { setLoading(false); }
  }, [load]);
  React.useEffect(() => { void reload(); }, [reload]);
  React.useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("account");
    const account = data?.accounts.find(a => a.id === id);
    if (account) setQuery(current => current === id ? label(account) : current);
  }, [data]);
  const all = data?.accounts || [];
  const filtered = all.filter(a => [a.id, label(a), a.username, a.display_name, a.account_status, a.problem]
    .join(" ").toLowerCase().includes(query.trim().toLowerCase()) &&
    (filter === "all" || filter === "ready" && a.ready || filter === "admission" && a.can_attest && a.permission !== "allowed" ||
      filter === "proxy" && !a.proxy_ready || filter === "paused" && a.permission === "paused"))
    .sort((a, b) => Number(b.proxy_ready && b.can_attest && b.permission !== "allowed") - Number(a.proxy_ready && a.can_attest && a.permission !== "allowed"));
  const pages = Math.max(1, Math.ceil(filtered.length / 20));
  const currentPage = Math.min(page, pages - 1);
  async function mutate(a: SafetyAccountRow, action: "allow" | "pause") {
    setBusy(a.id); setError(""); setNotice("");
    try {
      if (action === "allow") await props.onOwnerAttest(a.id, note.trim() || "Владелец подтверждает: личный доверенный аккаунт, прогрет и допущен к работе. Подтверждено в разделе Безопасность.");
      else await props.onPauseAccount(a.id);
      setAttesting(null);
      setNotice(`${label(a)}: ${action === "allow" ? "допуск владельца сохранён" : "работа приостановлена"}.`);
      await reload();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось изменить допуск"); }
    finally { setBusy(""); }
  }
  return <section className="space-y-4 rounded-xl border bg-card p-4 sm:p-6" aria-label="Допуск аккаунтов" data-testid="safety-account-table">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-xl font-semibold">Допуск аккаунтов к работе</h2>
        <p className="mt-1 text-sm text-muted-foreground">Разрешайте работу доверенным аккаунтам или ставьте их на паузу. Состояние сессии и прокси показано отдельно.</p></div>
      <Button variant="outline" disabled={loading || Boolean(busy)} onClick={() => void reload()}>Обновить статусы</Button>
    </div>
    <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
      {[['Всего аккаунтов', all.length, 'all'], ['Готовы к работе', all.filter(a => a.ready).length, 'ready'],
        ['Можно дать допуск', all.filter(a => a.can_attest && a.permission !== 'allowed').length, 'admission'],
        ['Нужен прокси', all.filter(a => !a.proxy_ready).length, 'proxy']].map(([title, count, key]) =>
        <button type="button" key={key} onClick={() => { setFilter(String(key)); setPage(0); }} className="rounded-lg border p-3 text-left hover:bg-muted/50">
          <div className="text-xs text-muted-foreground">{title}</div><div className="mt-1 text-xl font-semibold">{data ? count : '—'}</div></button>)}
    </div>
    <div className="flex flex-wrap gap-2">
      <Input className="min-w-48 flex-1" aria-label="Поиск в таблице допуска" placeholder="Номер, имя, username или причина" value={query} onChange={e => { setQuery(e.target.value); setPage(0); }} />
      <select className="rounded-md border bg-background px-3 py-2 text-sm" aria-label="Фильтр допуска" value={filter} onChange={e => { setFilter(e.target.value); setPage(0); }}>
        <option value="all">Все аккаунты</option><option value="ready">Готовы к работе</option><option value="admission">Можно дать допуск</option><option value="proxy">Нужен прокси</option><option value="paused">Приостановлены</option>
      </select>
    </div>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {notice && <p role="status" className="rounded-md bg-muted p-3 text-sm">{notice}</p>}
    <div className="overflow-x-auto rounded-md border">
      <table className="block w-full text-left text-sm sm:table"><thead className="hidden bg-muted/40 sm:table-header-group"><tr>
        {['Аккаунт', 'Сессия / прокси', 'Допуск', 'Что требуется', 'Действия'].map(t => <th key={t} className="whitespace-nowrap p-3 font-medium">{t}</th>)}
      </tr></thead><tbody className="block sm:table-row-group">
        {filtered.slice(currentPage * 20, currentPage * 20 + 20).map(a => <tr key={a.id} className="grid border-t pb-3 align-top sm:table-row sm:pb-0" data-account-id={a.id}>
          <td className="block px-3 py-1 first:pt-3 sm:table-cell sm:p-3"><div className="whitespace-nowrap font-medium">{label(a)}</div><div className="text-xs text-muted-foreground">{a.username ? '@' + a.username : a.display_name || 'Без username'}</div></td>
          <td className="block px-3 py-1 first:pt-3 sm:table-cell sm:p-3"><div>{a.session_status === 'valid' ? 'Сессия проверена' : a.session_status === 'invalid' ? 'Сессия невалидна' : 'Сессия не проверена'}</div>
            <div className="text-xs text-muted-foreground">{a.proxy_ready ? 'Прокси назначен, проверка успешна' : a.proxy_assigned ? 'Прокси требует проверки' : 'Прокси не назначен'}</div></td>
          <td className="block px-3 py-1 first:pt-3 sm:table-cell sm:p-3"><span className={`inline-block whitespace-nowrap rounded-md px-2 py-1 text-xs ${a.permission === 'allowed' ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200' : 'bg-muted text-foreground'}`}>{permissionLabels[a.permission] || a.permission}</span>
            <div className="mt-1 text-xs text-muted-foreground">{a.admission_basis === 'owner_attestation' ? 'Подтверждение владельца' : `Telegram: ${restrictionLabels[a.restriction_status] || a.restriction_status}`}</div></td>
          <td className="block min-w-40 px-3 py-1 text-muted-foreground sm:table-cell sm:p-3">{a.problem || 'Готов к работе'}</td>
          <td className="block px-3 py-1 first:pt-3 sm:table-cell sm:p-3"><div className="flex flex-wrap gap-2">
            {a.permission !== 'allowed' && <Button size="sm" disabled={!a.can_attest || Boolean(busy)} title={a.attest_problem || undefined} onClick={() => { setAttesting(a); setNote(''); setError(''); }}>Разрешить работу</Button>}
            {a.can_pause && <Button size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => void mutate(a, 'pause')}>Приостановить</Button>}
            <Button size="sm" variant="ghost" onClick={() => setDetail(a)}>Подробнее</Button>
          </div>{!a.can_attest && a.permission !== 'allowed' && <p className="mt-1 max-w-60 text-xs text-muted-foreground">{a.attest_problem}</p>}</td>
        </tr>)}
        {!filtered.length && <tr><td colSpan={5} className="p-5 text-muted-foreground">{loading ? 'Загружаю аккаунты…' : 'Аккаунты не найдены'}</td></tr>}
      </tbody></table>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground"><span>Показано {filtered.length ? currentPage * 20 + 1 : 0}–{Math.min((currentPage + 1) * 20, filtered.length)} из {filtered.length}. Всего в workspace: {all.length}.</span>
      <div className="flex items-center gap-2"><Button size="sm" variant="outline" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Назад</Button><span>{currentPage + 1} / {pages}</span><Button size="sm" variant="outline" disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>Далее</Button></div></div>
    <Dialog open={Boolean(attesting)} onOpenChange={open => { if (!open && !busy) setAttesting(null); }}><DialogContent className="max-h-[85svh] overflow-y-auto"><DialogHeader>
      <DialogTitle>Разрешить работу {attesting ? label(attesting) : ''}</DialogTitle><DialogDescription>Вы подтверждаете, что это ваш доверенный, уже прогретый аккаунт. Допуск сохраняется от имени владельца workspace.</DialogDescription>
    </DialogHeader><p className="text-sm">После подтверждения аккаунт можно использовать в кампании. Назначение источников и пауза между вступлениями задаются при замене аккаунта.</p>
      <label className="grid gap-1 text-sm">Примечание (необязательно)<Input value={note} onChange={e => setNote(e.target.value)} maxLength={10000} placeholder="Например: личный аккаунт, прогрев завершён" /></label>
      {attesting && !attesting.proxy_ready && <p className="text-sm text-muted-foreground">Для запуска также потребуется рабочий прокси.</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <Button disabled={Boolean(busy)} onClick={() => attesting && void mutate(attesting, 'allow')}>{busy ? 'Сохраняю…' : 'Подтвердить допуск владельца'}</Button>
    </DialogContent></Dialog>
    <Dialog open={Boolean(detail)} onOpenChange={open => { if (!open) { setDetail(null); void reload(); } }}><DialogContent className="max-h-[85svh] overflow-y-auto"><DialogHeader><DialogTitle>{detail ? label(detail) : ''}: состояние аккаунта</DialogTitle><DialogDescription>Диагностика остановки и допуск по результату проверки Telegram.</DialogDescription></DialogHeader>
      {detail && <TelegramSafetyPanel accountId={detail.id} onLoadSafetyState={props.onLoadSafetyState} onResumeSafety={props.onResumeSafety} onAdmitSafety={props.onAdmitSafety} />}
    </DialogContent></Dialog>
  </section>;
}
