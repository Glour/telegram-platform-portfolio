const METRIKA_COUNTER_ID = Number(import.meta.env.VITE_METRIKA_COUNTER_ID || 0);

type MetrikaFunction = (
  counterId: number,
  method: string,
  target?: string,
  params?: Record<string, unknown>,
) => void;

declare global {
  interface Window {
    ym?: MetrikaFunction;
  }
}

export function trackGoal(goal: string, params?: Record<string, unknown>) {
  callMetrika("reachGoal", goal, params);
}

export function trackPageView(url: string) {
  callMetrika("hit", url);
}

export function trackProductVisit(workspaceId: string) {
  if (typeof window === "undefined" || !workspaceId) return;
  const prefix = `demo-platform:activation:${workspaceId}`;
  const now = Date.now();
  const today = new Date(now).toISOString().slice(0, 10);
  const firstSeenRaw = window.localStorage.getItem(`${prefix}:first-seen`);
  const firstSeen = firstSeenRaw ? Number(firstSeenRaw) : now;
  if (!firstSeenRaw) window.localStorage.setItem(`${prefix}:first-seen`, String(now));
  if (window.localStorage.getItem(`${prefix}:last-visit`) === today) return;

  const daysSinceFirst = Math.max(0, Math.floor((now - firstSeen) / 86_400_000));
  window.localStorage.setItem(`${prefix}:last-visit`, today);
  trackGoal("product_visit", {
    workspace_id: workspaceId,
    days_since_first_visit: daysSinceFirst,
  });

  for (const day of [7, 30]) {
    const retentionKey = `${prefix}:retention-${day}`;
    if (daysSinceFirst >= day && !window.localStorage.getItem(retentionKey)) {
      window.localStorage.setItem(retentionKey, today);
      trackGoal(`retention_day_${day}`, { workspace_id: workspaceId });
    }
  }
}

export function trackActivationMilestone(
  workspaceId: string,
  goal: string,
  params?: Record<string, unknown>,
) {
  if (typeof window === "undefined" || !workspaceId || !goal) return;
  const key = `demo-platform:activation:${workspaceId}:milestone:${goal}`;
  if (window.localStorage.getItem(key)) return;
  window.localStorage.setItem(key, new Date().toISOString());
  trackGoal(goal, { workspace_id: workspaceId, ...params });
}

function callMetrika(
  method: string,
  target?: string,
  params?: Record<string, unknown>,
  attempt = 0,
) {
  if (typeof window === "undefined" || !METRIKA_COUNTER_ID) return;

  if (typeof window.ym === "function") {
    window.ym(METRIKA_COUNTER_ID, method, target, params);
    return;
  }

  if (attempt >= 10) return;

  window.setTimeout(() => {
    callMetrika(method, target, params, attempt + 1);
  }, 300);
}
