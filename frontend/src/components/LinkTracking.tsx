import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { apiRequest, apiBlobRequest } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Link = {
  id: string;
  title: string;
  destination_url: string;
  placement: string;
  campaign_id: string | null;
  account_id: string | null;
  enabled: boolean;
  tracking_url: string;
  requests: number;
  transitions: number;
  unique_daily: number;
};
type Choice = {
  id: string;
  name?: string;
  phone?: string | null;
  username?: string | null;
};
const blank = {
  title: "",
  destination_url: "",
  placement: "",
  campaign_id: "",
  account_id: "",
  enabled: true,
};

export function LinkTracking({
  token,
  workspaceId,
  campaigns,
  accounts,
}: {
  token: string;
  workspaceId: string;
  campaigns: Choice[];
  accounts: Choice[];
}) {
  const [links, setLinks] = useState<Link[]>([]);
  const [days, setDays] = useState(30);
  const [search, setSearch] = useState("");
  const [form, setForm] = useState(blank);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(async () => {
    try {
      setLinks(
        await apiRequest<Link[]>(`/tracking-links?days=${days}`, {
          token,
          workspaceId,
        }),
      );
      setError("");
      setLoaded(true);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Не удалось загрузить статистику",
      );
    }
  }, [days, token, workspaceId]);
  useEffect(() => {
    void load();
  }, [load]);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await apiRequest(`/tracking-links${editing ? `/${editing}` : ""}`, {
        token,
        workspaceId,
        method: editing ? "PUT" : "POST",
        body: {
          ...form,
          campaign_id: form.campaign_id || null,
          account_id: form.account_id || null,
        },
      });
      setForm(blank);
      setEditing(null);
      await load();
      toast.success("Ссылка сохранена");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Ошибка сохранения");
    } finally {
      setBusy(false);
    }
  };
  const edit = (link: Link) => {
    setEditing(link.id);
    setForm({
      title: link.title,
      destination_url: link.destination_url,
      placement: link.placement,
      campaign_id: link.campaign_id || "",
      account_id: link.account_id || "",
      enabled: link.enabled,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const exportCsv = async () => {
    try {
      const blob = await apiBlobRequest(
        `/tracking-links/export.csv?days=${days}`,
        { token, workspaceId },
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "link-transitions.csv";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Ошибка экспорта");
    }
  };
  const filtered = links.filter((l) =>
    `${l.title} ${l.placement} ${l.destination_url}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const totals = links.reduce(
    (a, l) => ({
      requests: a.requests + l.requests,
      transitions: a.transitions + l.transitions,
    }),
    { requests: 0, transitions: 0 },
  );
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-semibold">Ссылки и переходы</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Ставьте отдельную ссылку на каждое размещение. Название кнопки в
          Telegram менять не нужно.
        </p>
      </div>
      <form onSubmit={save} className="space-y-4 rounded-xl border p-4">
        <h3 className="font-medium">
          {editing ? "Редактирование ссылки" : "Новая отслеживаемая ссылка"}
        </h3>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="lt-title">Название направления</Label>
            <Input
              id="lt-title"
              required
              maxLength={160}
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="Example campaign"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="lt-url">Куда перенаправлять</Label>
            <Input
              id="lt-url"
              type="url"
              required
              maxLength={2048}
              value={form.destination_url}
              onChange={(e) =>
                setForm({ ...form, destination_url: e.target.value })
              }
              placeholder="https://t.me/channel или https://t.me/+…"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="lt-placement">Размещение / переходник</Label>
            <Input
              id="lt-placement"
              maxLength={160}
              value={form.placement}
              onChange={(e) => setForm({ ...form, placement: e.target.value })}
              placeholder="Переходник №12 · нейрокомментинг"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="lt-campaign">Кампания (необязательно)</Label>
            <select
              id="lt-campaign"
              className="h-9 w-full rounded-md border bg-background px-2 text-sm"
              value={form.campaign_id}
              onChange={(e) =>
                setForm({ ...form, campaign_id: e.target.value })
              }
            >
              <option value="">Без привязки</option>
              {campaigns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name || c.id}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="lt-account">Аккаунт (необязательно)</Label>
            <select
              id="lt-account"
              className="h-9 w-full rounded-md border bg-background px-2 text-sm"
              value={form.account_id}
              onChange={(e) => setForm({ ...form, account_id: e.target.value })}
            >
              <option value="">Без привязки</option>
              {accounts.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.phone || c.username || c.id}
                </option>
              ))}
            </select>
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          />
          Ссылка включена
        </label>
        <p className="text-xs text-muted-foreground">
          Изменение назначения сохраняет URL и историю. Выключенная ссылка не
          перенаправляет посетителей.
        </p>
        <div className="flex gap-2">
          <Button disabled={busy} type="submit">
            {busy ? "Сохраняю…" : editing ? "Сохранить" : "Создать ссылку"}
          </Button>
          {editing && (
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setEditing(null);
                setForm(blank);
              }}
            >
              Отмена
            </Button>
          )}
        </div>
      </form>
      <div className="flex flex-wrap items-center gap-3">
        <Label htmlFor="lt-period">Период</Label>
        <select
          id="lt-period"
          className="rounded-md border bg-background p-2 text-sm"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          <option value={1}>Сегодня</option>
          <option value={7}>7 дней</option>
          <option value={30}>30 дней</option>
          <option value={90}>90 дней</option>
        </select>
        <Button variant="outline" onClick={() => void load()}>
          Обновить
        </Button>
        <Button variant="outline" onClick={() => void exportCsv()}>
          Экспорт по дням CSV
        </Button>
        <span className="text-sm">
          Переходы: <b>{totals.transitions}</b> · технические запросы:{" "}
          {totals.requests - totals.transitions}
        </span>
      </div>
      <p className="text-sm text-muted-foreground">
        Переход — запрос после фильтрации известных ботов и предпросмотров, а не
        подтверждение подписки. Уникальные ≈ — сумма уникальных посетителей за
        каждый день (МСК), а не точное число людей за период. Один человек в
        разные дни считается повторно. Общая ссылка в общем переходнике не
        определяет исходный аккаунт.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <Input
        aria-label="Поиск ссылок"
        placeholder="Поиск по названию, размещению или назначению"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="overflow-x-auto rounded-xl border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-left">
            <tr>
              {[
                "Ссылка / размещение",
                "Переходы",
                "Уникальные ≈",
                "Технические",
                "Действия",
              ].map((h) => (
                <th key={h} className="whitespace-nowrap p-3 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map((l) => (
              <tr key={l.id} className="border-t">
                <td className="max-w-md p-3">
                  <div className="font-medium">
                    {l.title}{" "}
                    {!l.enabled && (
                      <span className="text-muted-foreground">· выключена</span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {l.placement}
                  </div>
                  <code className="block break-all text-xs">
                    {l.tracking_url}
                  </code>
                  <div
                    className="truncate text-xs text-muted-foreground"
                    title={l.destination_url}
                  >
                    → {l.destination_url}
                  </div>
                </td>
                <td className="p-3">{l.transitions}</td>
                <td className="p-3">{l.unique_daily}</td>
                <td className="p-3">{l.requests - l.transitions}</td>
                <td className="p-3">
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        void navigator.clipboard.writeText(l.tracking_url).then(
                          () => toast.success("Ссылка скопирована"),
                          () => toast.error("Не удалось скопировать ссылку"),
                        );
                      }}
                    >
                      Копировать
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => edit(l)}>
                      Изменить
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
            {!filtered.length && (
              <tr>
                <td
                  colSpan={5}
                  className="p-6 text-center text-muted-foreground"
                >
                  {loaded
                    ? "Ссылок пока нет или ничего не найдено"
                    : "Загружаю ссылки…"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
