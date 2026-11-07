import * as React from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import {
  ArrowRightIcon,
  CheckIcon,
  CircleHelpIcon,
  ClipboardListIcon,
  CreditCardIcon,
  FileTextIcon,
  GaugeIcon,
  Layers3Icon,
  LockKeyholeIcon,
  MailIcon,
  MenuIcon,
  MessageSquareTextIcon,
  NetworkIcon,
  RadioTowerIcon,
  ReceiptTextIcon,
  ShieldCheckIcon,
  ShoppingCartIcon,
  SparklesIcon,
  Trash2Icon,
  UsersRoundIcon,
  WalletCardsIcon,
  XIcon,
} from "lucide-react";

import { FloatingAssistantWidget } from "@/components/assistant/FloatingAssistantWidget";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  apiRequest,
  checkoutCartRequest,
  clearSessionStorage,
  getCartRequest,
  storedToken,
  storedWorkspaceId,
  updateCartRequest,
  USER_STORAGE_KEY,
  WORKSPACE_DATA_STORAGE_KEY,
  type AccessSummary,
  type User,
  type Workspace,
} from "@/lib/api";
import { openAssistantSupport } from "@/lib/assistant";
import { trackGoal } from "@/lib/metrika";
import { cn } from "@/lib/utils";

const navItems = [
  { to: "/", label: "Главная" },
  { to: "/platform", label: "Платформа" },
  { to: "/about", label: "О сервисе" },
  { to: "/help", label: "Полезное" },
  { to: "/blog", label: "Статьи" },
  { to: "/use-cases", label: "Сценарии" },
  { to: "/pricing", label: "Тарифы" },
  { to: "/faq", label: "FAQ" },
];

const pricingUrl = "/pricing#pricing-plans";
const cartPageUrl = "/checkout";
const cartStorageKey = "demo-platform_cart_v1";
const cartChangedEvent = "demo-platform-cart-changed";
const siteUrl = "https://demo.invalid";
const defaultOgImage = `${siteUrl}/og-demo-platform.svg`;

const footerSections = [
  {
    title: "Продукт",
    links: [
      { to: "/platform", label: "Платформа" },
      { to: "/about", label: "О сервисе" },
      { to: "/help", label: "Как пользоваться" },
      { to: "/blog", label: "Статьи по Telegram-маркетингу" },
      { to: "/pricing", label: "Тарифы" },
      { to: "/pricing#modules", label: "Модули по отдельности" },
      { to: "/faq", label: "FAQ" },
    ],
  },
  {
    title: "Сценарии",
    links: [
      { to: "/neurocommenting", label: "Нейрокомментинг" },
      { to: "/content-factory", label: "Контент-завод" },
      { to: "/telegram-lead-generation", label: "Лиды из Telegram" },
      { to: "/telegram-account-warmup", label: "Прогрев аккаунтов" },
      { to: "/telegram-marketing-platform", label: "Telegram-маркетинг" },
    ],
  },
  {
    title: "Документы",
    links: [
      { to: "/legal/privacy", label: "Конфиденциальность" },
      { to: "/legal/offer", label: "Оферта" },
      { to: "/legal/terms", label: "Соглашение" },
      { to: "/legal/payment", label: "Оплата и возвраты" },
      { to: "/sitemap", label: "Карта сайта" },
      { to: "/contact", label: "Поддержка", support: true },
    ],
  },
];

function BrandWord({
  aiClassName = "text-[#d7ff46]",
  className,
}: {
  aiClassName?: string;
  className?: string;
}) {
  return (
    <span className={cn("whitespace-nowrap", className)}>
      demo<span className={aiClassName}>platform</span>
    </span>
  );
}

type MarketingSession = {
  token: string;
  workspaceId: string;
  user: User;
  workspace: Workspace | null;
};

function readJsonStorage<T>(key: string): T | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function useMarketingSession() {
  const [session, setSession] = React.useState<MarketingSession | null>(() => {
    const token = typeof window === "undefined" ? null : storedToken();
    const workspaceId =
      typeof window === "undefined" ? null : storedWorkspaceId();
    const user = readJsonStorage<User>(USER_STORAGE_KEY);
    const workspace = readJsonStorage<Workspace>(WORKSPACE_DATA_STORAGE_KEY);
    return token && workspaceId && user
      ? { token, workspaceId, user, workspace }
      : null;
  });
  const [loading, setLoading] = React.useState(() => Boolean(session));

  React.useEffect(() => {
    const token = storedToken();
    const workspaceId = storedWorkspaceId();
    if (!token || !workspaceId) {
      setSession(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    Promise.all([
      apiRequest<User>("/auth/me", { token }),
      apiRequest<Workspace[]>("/workspaces", { token }),
    ])
      .then(([user, workspaces]) => {
        if (cancelled) return;
        const workspace =
          workspaces.find((item) => item.id === workspaceId) ||
          workspaces[0] ||
          null;
        if (!workspace) {
          setSession(null);
          return;
        }
        window.localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user));
        window.localStorage.setItem(
          WORKSPACE_DATA_STORAGE_KEY,
          JSON.stringify(workspace),
        );
        setSession({ token, workspaceId: workspace.id, user, workspace });
      })
      .catch(() => {
        if (!cancelled) {
          clearSessionStorage();
          setSession(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return { session, loading };
}

function uniqueItems(items: string[]) {
  return Array.from(new Set(items.map((item) => item.trim()).filter(Boolean)));
}

type BillingCycle = "monthly" | "yearly";

function cycleCode(code: string, cycle: BillingCycle) {
  if (code === "free") return code;
  return cycle === "yearly" ? `${code}.yearly` : code;
}

function baseProductCode(code: string) {
  return code.endsWith(".yearly") ? code.slice(0, -7) : code;
}

const planRank: Record<string, number> = { start: 1, core: 2, agency: 3 };
const planIncludedCodes: Record<string, string[]> = {
  start: [
    "module.neurocommenting",
    "module.message_broadcast",
    "module.warmup",
    "module.channel_base",
    "module.ai",
    "module.datasets",
  ],
  core: [
    "module.neurocommenting",
    "module.message_broadcast",
    "module.warmup",
    "module.channel_base",
    "module.ai",
    "module.datasets",
    "module.content_factory",
  ],
  agency: [
    "module.neurocommenting",
    "module.message_broadcast",
    "module.warmup",
    "module.channel_base",
    "module.ai",
    "module.datasets",
    "module.content_factory",
  ],
};

function normalizeCartItems(items: string[]) {
  const standaloneByBase = new Map<string, string>();
  let bestPlan: string | null = null;

  for (const raw of uniqueItems(items)) {
    const code = raw.trim();
    if (!productByCode(code)) continue;
    const base = baseProductCode(code);
    if (planRank[base]) {
      const currentBase = bestPlan ? baseProductCode(bestPlan) : null;
      if (!currentBase || planRank[base] >= planRank[currentBase]) {
        bestPlan = code;
      }
      continue;
    }
    standaloneByBase.set(base, code);
  }

  if (!bestPlan) return Array.from(standaloneByBase.values());
  const bestPlanBase = baseProductCode(bestPlan);
  const included = new Set(planIncludedCodes[bestPlanBase] || []);
  return [
    bestPlan,
    ...Array.from(standaloneByBase.entries())
      .filter(([base]) => !included.has(base))
      .map(([, code]) => code),
  ];
}

function featureEnabled(
  access: AccessSummary | null | undefined,
  featureCode: string,
) {
  if (!access) return false;
  if (access.enabled_features.includes(featureCode)) return true;
  return access.entitlements.some(
    (entitlement) =>
      entitlement.feature_code === featureCode && entitlement.limit_value > 0,
  );
}

function productFeatureCodes(code: string) {
  const base = baseProductCode(code);
  if (planIncludedCodes[base]) return planIncludedCodes[base];
  if (base.startsWith("module.")) return [base];
  return [];
}

function productAlreadyOwned(
  code: string,
  access: AccessSummary | null | undefined,
) {
  if (!access) return false;
  const base = baseProductCode(code);
  if (base === "free") return true;
  if (access.plan?.code && baseProductCode(access.plan.code) === base)
    return true;
  const features = productFeatureCodes(code);
  return (
    features.length > 0 &&
    features.every((feature) => featureEnabled(access, feature))
  );
}

function filterOwnedCartItems(items: string[], access?: AccessSummary | null) {
  return normalizeCartItems(items).filter(
    (code) => !productAlreadyOwned(code, access),
  );
}

type CheckoutProduct = (typeof checkoutProducts)[number];

function readStoredCart() {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(cartStorageKey) || "[]",
    );
    return Array.isArray(parsed) ? normalizeCartItems(parsed.map(String)) : [];
  } catch {
    return [];
  }
}

function writeStoredCart(items: string[]) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(
    cartStorageKey,
    JSON.stringify(normalizeCartItems(items)),
  );
  window.dispatchEvent(new Event(cartChangedEvent));
}

function useMarketingAccess(session?: MarketingSession | null) {
  const [access, setAccess] = React.useState<AccessSummary | null>(null);
  const [loading, setLoading] = React.useState(false);

  React.useEffect(() => {
    if (!session) {
      setAccess(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    void apiRequest<AccessSummary>("/plans/access", {
      token: session.token,
      workspaceId: session.workspaceId,
    })
      .then((summary) => {
        if (!cancelled) setAccess(summary);
      })
      .catch(() => {
        if (!cancelled) setAccess(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [session]);

  return { access, loading };
}

function useMarketingCart(
  session?: MarketingSession | null,
  access?: AccessSummary | null,
) {
  const [items, setItems] = React.useState<string[]>(() =>
    filterOwnedCartItems(readStoredCart(), access),
  );
  const [syncing, setSyncing] = React.useState(false);

  React.useEffect(() => {
    function syncCart() {
      setItems(filterOwnedCartItems(readStoredCart(), access));
    }
    window.addEventListener("storage", syncCart);
    window.addEventListener(cartChangedEvent, syncCart);
    return () => {
      window.removeEventListener("storage", syncCart);
      window.removeEventListener(cartChangedEvent, syncCart);
    };
  }, [access]);

  React.useEffect(() => {
    if (!session) return;
    let cancelled = false;
    setSyncing(true);
    void getCartRequest(session.token, session.workspaceId)
      .then((cart) => {
        if (cancelled) return;
        const merged = filterOwnedCartItems(
          [...cart.items, ...readStoredCart()],
          access,
        );
        writeStoredCart(merged);
        setItems(merged);
        if (merged.join("|") !== cart.items.join("|")) {
          void updateCartRequest(
            session.token,
            session.workspaceId,
            merged,
            cart.provider,
          );
        }
      })
      .catch(() => {
        if (!cancelled) setItems(readStoredCart());
      })
      .finally(() => {
        if (!cancelled) setSyncing(false);
      });
    return () => {
      cancelled = true;
    };
  }, [session, access]);

  React.useEffect(() => {
    const cleaned = filterOwnedCartItems(items, access);
    if (cleaned.join("|") !== items.join("|")) {
      writeStoredCart(cleaned);
      setItems(cleaned);
      if (session) {
        void updateCartRequest(
          session.token,
          session.workspaceId,
          cleaned,
        ).catch(() => undefined);
      }
    }
  }, [access, items, session]);

  const setCartItems = React.useCallback(
    (nextItems: string[]) => {
      const normalized = filterOwnedCartItems(nextItems, access);
      writeStoredCart(normalized);
      setItems(normalized);
      if (session) {
        void updateCartRequest(
          session.token,
          session.workspaceId,
          normalized,
        ).catch(() => {
          setItems(readStoredCart());
        });
      }
    },
    [session, access],
  );

  const addItem = React.useCallback(
    (code: string) => {
      trackGoal("cart_add", { product: code });
      setCartItems([...items, code]);
    },
    [items, setCartItems],
  );

  const removeItem = React.useCallback(
    (code: string) => {
      setCartItems(items.filter((item) => item !== code));
    },
    [items, setCartItems],
  );

  const clearCart = React.useCallback(() => setCartItems([]), [setCartItems]);

  return { items, addItem, removeItem, setCartItems, clearCart, syncing };
}

function productByCode(code: string) {
  return checkoutProducts.find((product) => product.code === code);
}

function cartProducts(items: string[]) {
  return items
    .map((code) => productByCode(code))
    .filter((product): product is CheckoutProduct => Boolean(product));
}

const modules = [
  {
    icon: MessageSquareTextIcon,
    title: "AI-комментарии",
    text: "AI-комментарии к новым и историческим постам с настройкой тона, промптов и безопасного режима.",
  },
  {
    icon: MailIcon,
    title: "Рассылки сообщений",
    text: "Аккуратная отправка заданного текста по диалогам через живые Telegram-аккаунты с задержками и отчетами.",
  },
  {
    icon: SparklesIcon,
    title: "AI реакции",
    text: "Постановка реакций на посты в каналах и группах с вероятностью, лимитами и ротацией аккаунтов.",
  },
  {
    icon: NetworkIcon,
    title: "База каналов и источники",
    text: "Каталог каналов по тематикам, ручные источники, проверка комментариев и доступности через Telegram API.",
  },
  {
    icon: RadioTowerIcon,
    title: "Аккаунты и прокси",
    text: "Импорт Telegram-аккаунтов, привязка прокси, проверка доступности, спам-блоков и состояния аккаунта.",
  },
  {
    icon: GaugeIcon,
    title: "Прогрев",
    text: "Планы прогрева, чеклисты и статусы помогают не запускать активность на неподготовленных аккаунтах.",
  },
  {
    icon: Layers3Icon,
    title: "Контент-завод",
    text: "Источники, знания о продукте, рубрики, генерация постов, картинки, согласование и публикация в одном контуре.",
  },
];

const standaloneProducts = [
  {
    code: "module.neurocommenting",
    name: "AI-комментарии",
    price: 3490,
    yearlyPrice: 34900,
    status: "available",
    description:
      "AI-комментарии к новым и историческим постам по промптам, тонам и правилам безопасности.",
  },
  {
    code: "module.message_broadcast",
    name: "Рассылки сообщений",
    price: 2190,
    yearlyPrice: 21900,
    status: "available",
    description:
      "Отправка заданного текста по выбранным диалогам через живые Telegram-аккаунты.",
  },
  {
    code: "module.warmup",
    name: "Прогрев аккаунтов",
    price: 2490,
    yearlyPrice: 24900,
    status: "available",
    description:
      "Планы прогрева, проверки и чеклисты здоровья аккаунтов перед кампаниями.",
  },
  {
    code: "module.content_factory",
    name: "Контент-завод",
    price: 6990,
    yearlyPrice: 69900,
    status: "available",
    description:
      "Полный цикл Telegram-контента: источники, знания о продукте, рубрики, генерация постов, картинки, согласование и публикация.",
  },
  {
    code: "module.channel_base",
    name: "База каналов",
    price: 0,
    yearlyPrice: 0,
    status: "available",
    description:
      "Каталог Telegram-каналов по тематикам для быстрого подбора источников.",
  },
  {
    code: "module.mass_reactions",
    name: "AI реакции",
    price: 2190,
    yearlyPrice: 21900,
    status: "available",
    description:
      "AI реакции на посты в каналах и группах с лимитами, задержками и ротацией аккаунтов.",
  },
  {
    code: "module.neurochatting",
    name: "AI-чат",
    price: 3490,
    yearlyPrice: 34900,
    status: "available",
    description:
      "AI-ответы в группах и обсуждениях для мягкого старта диалогов.",
  },
  {
    code: "module.neurodialogs",
    name: "AI диалоги",
    price: 2990,
    yearlyPrice: 29900,
    status: "available",
    description:
      "AI-общение в личных диалогах по заданным сценариям и ограничениям.",
  },
  {
    code: "module.parsing.dialogs",
    name: "Парсинг диалогов",
    price: 490,
    yearlyPrice: 4900,
    status: "available",
    description: "Сбор списка личных диалогов аккаунта с базовыми метаданными.",
  },
  {
    code: "module.parsing.channels",
    name: "Парсинг каналов",
    price: 490,
    yearlyPrice: 4900,
    status: "available",
    description:
      "Сбор Telegram-каналов с базовыми метаданными и рабочей структурой.",
  },
  {
    code: "module.parsing.groups",
    name: "Парсинг групп",
    price: 490,
    yearlyPrice: 4900,
    status: "available",
    description: "Сбор групп и чатов аккаунта с сохранением в рабочую базу.",
  },
  {
    code: "module.parsing.posts",
    name: "Парсинг постов",
    price: 490,
    yearlyPrice: 4900,
    status: "available",
    description:
      "Сбор постов из Telegram-источников с сохранением в рабочую базу.",
  },
  {
    code: "module.parsing.users",
    name: "Парсинг пользователей",
    price: 490,
    yearlyPrice: 4900,
    status: "available",
    description: "Сбор участников и пользователей из доступных источников.",
  },
  {
    code: "module.parsing.comments",
    name: "Парсинг комментариев",
    price: 490,
    yearlyPrice: 4900,
    status: "available",
    description: "Сбор комментариев под постами каналов и групп.",
  },
];

const checkoutProducts = [
  ...standaloneProducts.filter((product) => product.status === "available"),
  ...standaloneProducts
    .filter((product) => product.status === "available")
    .map((product) => ({
      ...product,
      code: `${product.code}.yearly`,
      name: `${product.name} на год`,
      price: product.yearlyPrice,
      billingCycle: "yearly",
    })),
  {
    code: "start",
    name: "Start",
    price: 4990,
    yearlyPrice: 49900,
    status: "available",
    description:
      "AI-комментарии, рассылки, прогрев и база каналов для первого рабочего контура.",
  },
  {
    code: "start.yearly",
    name: "Start на год",
    price: 49900,
    status: "available",
    billingCycle: "yearly",
    description:
      "Годовой доступ Start: AI-комментарии, рассылки, прогрев и база каналов.",
  },
  {
    code: "core",
    name: "Core",
    price: 9900,
    yearlyPrice: 99000,
    status: "available",
    description:
      "Все доступные модули платформы, включая контент-завод, и повышенные лимиты.",
  },
  {
    code: "core.yearly",
    name: "Core на год",
    price: 99000,
    status: "available",
    billingCycle: "yearly",
    description:
      "Годовой доступ Core со всеми доступными модулями, контент-заводом и повышенными лимитами.",
  },
  {
    code: "agency",
    name: "Agency",
    price: 19900,
    yearlyPrice: 199000,
    status: "available",
    description: "Командный тариф с расширенными лимитами.",
  },
  {
    code: "agency.yearly",
    name: "Agency на год",
    price: 199000,
    status: "available",
    billingCycle: "yearly",
    description: "Годовой командный тариф с расширенными лимитами.",
  },
];

function formatRub(value: number) {
  return `${new Intl.NumberFormat("ru-RU").format(value)} ₽`;
}

const pricingPlans = [
  {
    name: "Free",
    code: "free",
    price: 0,
    yearlyPrice: 0,
    note: "7 дней",
    description:
      "Кабинет для подготовки. Новым пользователям автоматически открывается 7-дневный тест всех рабочих модулей с безопасными лимитами.",
    features: [
      "7 дней бесплатно для новых пользователей",
      "Telegram-аккаунты",
      "Прокси",
      "Источники",
      "Отчеты и история",
    ],
    cta: "Попробовать бесплатно",
    href: "/signup",
  },
  {
    name: "Core",
    code: "core",
    price: 9900,
    yearlyPrice: 99000,
    note: "Основной пакет",
    description:
      "Все доступные рабочие модули платформы, включая контент-завод, повышенные лимиты и будущие релизы после готовности.",
    features: [
      "Все доступные модули",
      "База каналов",
      "Повышенные лимиты",
      "Приоритетная поддержка",
    ],
    featured: true,
    cta: "Оплатить 30 дней",
  },
  {
    name: "Пилот с внедрением",
    code: "pilot",
    price: 59900,
    yearlyPrice: 59900,
    note: "С сопровождением",
    description:
      "За 30 дней вместе подключаем аккаунт, подбираем источники, запускаем первую безопасную кампанию и разбираем отчет.",
    features: [
      "Все возможности Core на 30 дней",
      "Настройка первого рабочего сценария",
      "Сопровождение до первого результата",
      "Итоговый отчет и план продолжения",
    ],
    cta: "Обсудить пилот",
    href: "/contact",
  },
];

const comparisonRows = [
  {
    feature: "Telegram-аккаунты",
    free: "1 тестовый",
    start: "5 аккаунтов",
    core: "20 аккаунтов",
    agency: "60 аккаунтов",
  },
  {
    feature: "Прокси",
    free: "Включены",
    start: "Включены",
    core: "Включены",
    agency: "Включены",
  },
  {
    feature: "Источники",
    free: "До 100",
    start: "До 5 000",
    core: "До 25 000",
    agency: "До 100 000",
  },
  {
    feature: "База каналов",
    free: "Нет",
    start: "Включена",
    core: "Включена",
    agency: "Включена",
  },
  {
    feature: "AI-комментарии",
    free: "Нет",
    start: "Включен",
    core: "Включен",
    agency: "Включен",
  },
  {
    feature: "Рассылки сообщений",
    free: "Нет",
    start: "Включены",
    core: "Включены",
    agency: "Включены",
  },
  {
    feature: "Прогрев аккаунтов",
    free: "Нет",
    start: "Включен",
    core: "Включен",
    agency: "Включен",
  },
  {
    feature: "Контент-завод",
    free: "Нет",
    start: "Отдельный модуль",
    core: "Включен",
    agency: "Включен",
  },
  {
    feature: "AI реакции",
    free: "Отдельный модуль",
    start: "Отдельный модуль",
    core: "Включены",
    agency: "Включены",
  },
  {
    feature: "AI-чат",
    free: "Отдельный модуль",
    start: "Отдельный модуль",
    core: "Включен",
    agency: "Включен",
  },
  {
    feature: "AI диалоги",
    free: "Отдельный модуль",
    start: "Отдельный модуль",
    core: "Включены",
    agency: "Включены",
  },
  {
    feature: "Парсеры",
    free: "Отдельные модули",
    start: "Отдельные модули",
    core: "Включены",
    agency: "Включены",
  },
  {
    feature: "Внедрение",
    free: "Нет",
    start: "Самостоятельно",
    core: "Самостоятельно",
    agency: "С командой demo-platform",
  },
];

const platformMetrics = [
  {
    value: "1 кабинет",
    label: "для аккаунтов, прокси, источников, кампаний и отчетов",
  },
  {
    value: "5-12 минут",
    label: "до первого тестового запуска после подготовки аккаунтов",
  },
  {
    value: "до 70%",
    label: "меньше ручных операций в рутинном мониторинге источников",
  },
  {
    value: "24/7",
    label: "фоновые проверки новых постов, ошибок и статусов аккаунтов",
  },
];

const platformScreenshots = [
  {
    src: "/help/screenshots/01-dashboard-overview.png",
    title: "Дашборд",
    text: "Сводка по аккаунтам, кампаниям, ошибкам и активным процессам.",
  },
  {
    src: "/help/screenshots/05-ai-comments.png",
    title: "AI-комментарии",
    text: "Промпты, длина, тон, лимиты, черновики и статусы публикации рядом.",
  },
  {
    src: "/help/screenshots/07-parsing.png",
    title: "Парсинг",
    text: "Источники, фильтры, лимиты и окно найденных данных без бесконечного полотна.",
  },
  {
    src: "/help/screenshots/08-content-factory.png",
    title: "Контент-завод",
    text: "Знания о продукте, рубрики, генерация постов и публикация из одного места.",
  },
];

const platformFlow = [
  {
    title: "Подключаете инфраструктуру",
    text: "Импортируете Telegram-аккаунты, назначаете прокси, проверяете ограничения и доступы к источникам.",
  },
  {
    title: "Собираете рабочие источники",
    text: "Добавляете каналы, группы, конкретные аккаунты и базу каналов, чтобы кампания работала по понятной карте.",
  },
  {
    title: "Настраиваете AI и лимиты",
    text: "Задаете тон, промпты, длину комментариев, задержки, дневные лимиты, фильтры и правила согласования.",
  },
  {
    title: "Запускаете и смотрите логи",
    text: "Каждый комментарий, ошибка Telegram, проблема с сессией или прокси попадает в историю и отчеты.",
  },
];

const scenarioPlaybooks = [
  {
    title: "Недвижимость: быстрый тест оффера",
    audience: "Маркетолог или отдел продаж",
    result: "Понять, какие каналы и темы дают ответы без ручного обхода десятков обсуждений.",
    modules: ["Источники", "AI-комментарии", "UTM", "Отчеты"],
    steps: [
      "Собрать 30-80 релевантных каналов и обсуждений по новостройкам, ипотеке и районам.",
      "Задать разные углы комментариев: вопрос, экспертное уточнение, мягкий оффер, ссылка с UTM.",
      "Запустить малый лимит на подготовленных аккаунтах и сравнить ответы по источникам.",
    ],
  },
  {
    title: "Агентство: несколько клиентов в одном процессе",
    audience: "Команда performance/Telegram-маркетинга",
    result: "Развести кампании, аккаунты, источники и отчеты, чтобы менеджер видел статус без ручных таблиц.",
    modules: ["Workspace", "Кампании", "Аккаунты", "Логи"],
    steps: [
      "Создать отдельные кампании под клиентов, офферы и сегменты аудитории.",
      "Назначить аккаунты и лимиты так, чтобы активность не смешивалась между проектами.",
      "Смотреть ошибки, публикации и результаты в общей истории, а не в чатах исполнителей.",
    ],
  },
  {
    title: "Инфобизнес: прогрев перед запуском",
    audience: "Продюсер, launch-команда",
    result: "Подготовить аккаунты и источники до активной волны комментариев и сообщений.",
    modules: ["Прогрев", "Прокси", "AI-комментарии", "Безопасность"],
    steps: [
      "Проверить здоровье аккаунтов, сессии, прокси и доступность целевых источников.",
      "Запустить осторожный прогрев с паузами, чеклистами и ручной проверкой проблемных аккаунтов.",
      "После подготовки включить AI-комментарии с короткими лимитами и постепенным ростом нагрузки.",
    ],
  },
  {
    title: "Контент-команда: посты из базы знаний",
    audience: "Редактор, SMM, личный бренд",
    result: "Получать черновики постов из источников и знаний о продукте, не собирая контекст руками.",
    modules: ["Контент-завод", "Источники", "Рубрики", "Публикация"],
    steps: [
      "Загрузить знания о продукте, офферах, аудитории, ограничениях и стиле коммуникации.",
      "Подключить источники идей и рубрики, чтобы AI писал не абстрактно, а по рабочему контексту.",
      "Согласовывать черновики или публиковать автоматически, если процесс уже проверен.",
    ],
  },
];

const faqs = [
  {
    question: "Можно попробовать платформу бесплатно?",
    answer:
      "Да. Новые пользователи получают 7 дней тестового доступа к рабочим модулям с ограниченными лимитами: можно открыть кабинет, добавить аккаунты и проверить сценарий до оплаты.",
  },
  {
    question: "Это просто бот для комментариев?",
    answer:
      "Нет. Идея платформы шире: аккаунты, прокси, источники, кампании, AI-генерация, парсинг, прогрев, отчеты и доступы собираются в один рабочий кабинет.",
  },
  {
    question: "После регистрации пользователь сразу попадает в кабинет?",
    answer:
      "Да. Целевой сценарий такой: человек приходит на лендинг, регистрируется или входит, затем попадает на /dashboard в свой workspace.",
  },
  {
    question: "Можно будет ограничивать модули по оплате?",
    answer:
      "Да. В backend уже есть entitlements: базовые разделы открыты бесплатно, а AI-модули, прогрев и виды парсинга можно выдавать после оплаты.",
  },
  {
    question: "Какие платежи планируются?",
    answer:
      "Оплата будет привязана к тарифам и модулям: после успешного платежа кабинет получает нужные лимиты и доступы.",
  },
  {
    question: "Сервис помогает обходить ограничения Telegram?",
    answer:
      "Нет. demo-platform не предназначен для обхода банов, лимитов, антиспам-систем или правил Telegram. Платформа автоматизирует действия пользователя и помогает работать аккуратнее: задавать паузы, проверять аккаунты, вести логи и не отправлять однотипные сообщения без контроля.",
  },
  {
    question: "Можно ли гарантировать, что аккаунт не заблокируют?",
    answer:
      "Нет. Решения о лимитах, FloodWait, временных ограничениях и блокировках принимает Telegram. На риск влияют возраст аккаунта, история номера, жалобы пользователей, качество прокси, частота действий, содержание сообщений и поведение в чатах. Мы даем инструменты осторожной настройки, но не можем гарантировать отсутствие ограничений.",
  },
  {
    question: "Как снизить риск блокировок?",
    answer:
      "Используйте только свои аккаунты и легальные основания для коммуникации. Прогревайте новые аккаунты постепенно, не пишите незнакомым людям без ожидания контакта, избегайте массовых одинаковых ссылок, коммерческого спама, агрессивных призывов, резких скачков активности и работы без пауз. Перед запуском проверяйте статус аккаунта через runtime-события Telegram и следите за логами кампании.",
  },
];

const pricingFaqs = [
  {
    question: "Можно подключать модули отдельно?",
    answer:
      "Да. Базовый доступ можно расширять отдельными возможностями или готовыми пакетами под конкретный сценарий.",
  },
  {
    question: "Что произойдет после оплаты?",
    answer:
      "Workspace получает нужные права доступа, лимиты и оплаченный срок. Автосписаний нет: следующий период пользователь оплачивает отдельно.",
  },
  {
    question: "Можно ли начать без оплаты?",
    answer:
      "Да. Новым пользователям доступен 7-дневный тест рабочих модулей с ограниченными лимитами. После теста можно остаться на бесплатной базе или подключить нужный тариф/модуль.",
  },
];

type SeoLandingPageData = {
  slug: string;
  badge: string;
  title: string;
  description: string;
  intro: string;
  primaryKeyword: string;
  keywords: string[];
  pains: string[];
  workflow: { title: string; text: string }[];
  faq: { question: string; answer: string }[];
};

export const seoLandingPages: Record<string, SeoLandingPageData> = {
  neurocommenting: {
    slug: "neurocommenting",
    badge: "Нейрокомментинг",
    title: "Нейрокомментинг для Telegram-чатов и обсуждений",
    description:
      "Платформа demo-platform помогает запускать нейрокомментинг в Telegram: источники, AI-комментарии, лимиты, UTM-ссылки, логи и отчеты в одном кабинете.",
    intro:
      "Нейрокомментинг нужен командам, которые хотят системно участвовать в обсуждениях, тестировать офферы и получать переходы из Telegram-чатов без ручного мониторинга каждого поста.",
    primaryKeyword: "нейрокомментинг",
    keywords: [
      "нейрокомментинг",
      "нейро комментарии",
      "ai комментарии telegram",
      "нейрокомментинг в телеграм",
      "автоматизация комментариев telegram",
      "комментарии в телеграм чатах",
    ],
    pains: [
      "Маркетолог не успевает вручную отслеживать десятки источников.",
      "Комментарии пишутся без контекста и быстро выглядят как спам.",
      "Непонятно, какие аккаунты, ссылки и чаты дают переходы.",
    ],
    workflow: [
      {
        title: "Источники и посты",
        text: "Соберите каналы, группы и обсуждения, где появляется релевантный спрос.",
      },
      {
        title: "AI-профиль и тон",
        text: "Опишите роль, оффер, ограничения и стиль комментариев для каждого сценария.",
      },
      {
        title: "Лимиты и отчеты",
        text: "Запускайте кампанию через паузы, статусы, UTM-метки и понятные логи.",
      },
    ],
    faq: [
      {
        question: "Нейрокомментинг - это спам?",
        answer:
          "Нет, если использовать его аккуратно: писать по контексту, соблюдать правила чатов, не дублировать однотипные тексты и не обещать обход ограничений Telegram.",
      },
      {
        question: "Можно ли отследить переходы?",
        answer:
          "Да. Для Telegram-профилей и кампаний используются короткие ссылки и UTM-метки, а переходы и цели смотрятся в Яндекс.Метрике.",
      },
    ],
  },
  "telegram-automation": {
    slug: "telegram-automation",
    badge: "Автоматизация Telegram",
    title: "Автоматизация Telegram-маркетинга без ручной рутины",
    description:
      "demo-platform объединяет Telegram-аккаунты, прокси, источники, AI-комментарии, рассылки, логи и отчеты для управляемых Telegram-кампаний.",
    intro:
      "Автоматизация Telegram нужна, когда один менеджер уже не может вручную вести аккаунты, проверять источники, писать комментарии, контролировать лимиты и собирать аналитику.",
    primaryKeyword: "автоматизация Telegram",
    keywords: [
      "автоматизация telegram",
      "автоматизация телеграм",
      "telegram маркетинг",
      "telegram automation",
      "платформа telegram маркетинга",
      "автоматизация телеграм аккаунтов",
    ],
    pains: [
      "Аккаунты, прокси, источники и кампании живут в разных таблицах.",
      "Нет единого журнала действий и ошибок.",
      "Сложно масштабировать Telegram-операции на команду или агентство.",
    ],
    workflow: [
      {
        title: "Единый кабинет",
        text: "Workspace собирает аккаунты, источники, кампании, отчеты и доступы команды.",
      },
      {
        title: "Фоновые процессы",
        text: "Celery-воркеры обрабатывают парсинг, генерацию, прогрев и тики кампаний.",
      },
      {
        title: "Контроль результата",
        text: "UTM, цели Метрики, статусы аккаунтов и логи помогают видеть, что работает.",
      },
    ],
    faq: [
      {
        question: "Что можно автоматизировать?",
        answer:
          "Источники, AI-комментарии, рассылки, прогрев, отчеты, статусы аккаунтов и часть фоновых проверок.",
      },
      {
        question: "Это подходит агентствам?",
        answer:
          "Да. Workspace-модель помогает разделять клиентов, кампании, аккаунты и доступы.",
      },
    ],
  },
  "ai-comments-telegram": {
    slug: "ai-comments-telegram",
    badge: "AI-комментарии",
    title: "AI-комментарии для Telegram: от промпта до отчета",
    description:
      "Сервис AI-комментариев для Telegram помогает генерировать контекстные ответы, контролировать тон, лимиты, ссылки, источники и результаты кампаний.",
    intro:
      "AI-комментарии работают лучше, когда они привязаны к контексту поста, роли аккаунта, офферу и правилам безопасности. demo-platform собирает эти настройки рядом с логами и аналитикой.",
    primaryKeyword: "AI-комментарии Telegram",
    keywords: [
      "ai комментарии telegram",
      "ии комментарии телеграм",
      "нейросеть для комментариев",
      "ai commenting telegram",
      "генерация комментариев telegram",
      "комментарии с искусственным интеллектом",
    ],
    pains: [
      "AI пишет слишком рекламно или одинаково.",
      "Нет правил тона для разных аккаунтов и источников.",
      "Команда не видит, какие ответы ушли и где были ошибки.",
    ],
    workflow: [
      {
        title: "Промпт и профиль",
        text: "AI-профиль задает роль, стиль, ограничения, оффер и запрещенные формулировки.",
      },
      {
        title: "Humanizer-правила",
        text: "Тексты становятся короче, естественнее и ближе к формату живого обсуждения.",
      },
      {
        title: "Логи публикаций",
        text: "Каждый комментарий, ошибка и источник сохраняются для контроля кампании.",
      },
    ],
    faq: [
      {
        question: "Можно ли делать комментарии не рекламными?",
        answer:
          "Да. В промпте задаются тон, полезность, ограничения и запрет на агрессивные CTA.",
      },
      {
        question: "AI сам отправляет комментарии?",
        answer:
          "Это зависит от настроек кампании. Для рискованных сценариев лучше использовать ручную проверку и лимиты.",
      },
    ],
  },
  "content-factory": {
    slug: "content-factory",
    badge: "Контент-завод",
    title: "Контент-завод для Telegram-команд и агентств",
    description:
      "Контент-завод demo-platform помогает готовить идеи, комментарии, сообщения, промпты, связки и отчеты для Telegram-маркетинга.",
    intro:
      "Контент-завод нужен, когда команде важно регулярно создавать варианты текстов, тестировать гипотезы и поддерживать единый стиль коммуникации в Telegram.",
    primaryKeyword: "контент-завод",
    keywords: [
      "контент завод",
      "контент-завод",
      "ai контент для telegram",
      "генерация контента telegram",
      "контент для телеграм маркетинга",
      "производство контента с ai",
    ],
    pains: [
      "Контент пишется рывками и без системы.",
      "Промпты теряются, а удачные связки не переиспользуются.",
      "Сложно понять, какие тексты приводят переходы и заявки.",
    ],
    workflow: [
      {
        title: "Библиотека связок",
        text: "Храните офферы, тезисы, промпты и варианты сообщений для разных сегментов.",
      },
      {
        title: "Генерация вариантов",
        text: "Готовьте несколько подач под разные чаты, роли аккаунтов и этапы прогрева.",
      },
      {
        title: "Аналитика",
        text: "Ссылки, UTM и цели помогают сравнивать кампании, источники и тексты.",
      },
    ],
    faq: [
      {
        question: "Контент-завод уже доступен?",
        answer:
          "Модуль описан как часть продуктовой линейки. В тарифах он может подключаться отдельно или входить в старшие пакеты по мере готовности.",
      },
      {
        question: "Можно ли использовать для Telegram-чатов?",
        answer:
          "Да, но тексты должны быть контекстными, аккуратными и соответствовать правилам площадки.",
      },
    ],
  },
  "telegram-lead-generation": {
    slug: "telegram-lead-generation",
    badge: "Лиды из Telegram",
    title: "Лиды из Telegram-чатов через аккуратные AI-комментарии",
    description:
      "demo-platform помогает тестировать лидогенерацию в Telegram-чатах: источники, комментарии, короткие ссылки, UTM, цели Метрики и отчеты.",
    intro:
      "Лиды из Telegram появляются не от массового спама, а от точного попадания в обсуждения, понятного оффера, полезного комментария и прозрачной аналитики.",
    primaryKeyword: "лиды из Telegram",
    keywords: [
      "лиды из telegram",
      "лиды из телеграм",
      "telegram лидогенерация",
      "продвижение через комментарии",
      "заявки из telegram чатов",
      "маркетинг в телеграм чатах",
    ],
    pains: [
      "Неясно, какие чаты и аккаунты дают переходы.",
      "Ссылки публикуются без UTM и потом не анализируются.",
      "Комментарии выглядят как реклама и вызывают жалобы.",
    ],
    workflow: [
      {
        title: "Карта источников",
        text: "Разделите чаты по тематике, качеству обсуждений и вероятности спроса.",
      },
      {
        title: "Уникальные ссылки",
        text: "Используйте короткие ссылки и UTM, чтобы видеть эффективность аккаунтов и кампаний.",
      },
      {
        title: "Метрика и цели",
        text: "Смотрите не только визиты, но и signup, contact, checkout и оплату.",
      },
    ],
    faq: [
      {
        question: "Что важнее: много комментариев или качество?",
        answer:
          "Качество. Для нового теста лучше меньше комментариев, но с контекстом, паузами и измеримыми ссылками.",
      },
      {
        question: "Как отслеживать лиды?",
        answer:
          "Через UTM-метки, Яндекс.Метрику, цели signup/contact/payment и отчет по utm_content.",
      },
    ],
  },
  "telegram-account-warmup": {
    slug: "telegram-account-warmup",
    badge: "Прогрев аккаунтов",
    title: "Прогрев Telegram-аккаунтов с лимитами и логами",
    description:
      "Инструменты прогрева Telegram-аккаунтов в demo-platform помогают работать осторожнее: паузы, статусы, проверки, логи и контроль активности.",
    intro:
      "Прогрев аккаунтов не гарантирует отсутствие ограничений, но помогает не начинать активные кампании резким всплеском действий и видеть проблемы заранее.",
    primaryKeyword: "прогрев Telegram-аккаунтов",
    keywords: [
      "прогрев telegram аккаунтов",
      "прогрев телеграм аккаунтов",
      "лимиты telegram аккаунтов",
      "статусы telegram аккаунтов",
      "telegram floodwait",
      "контроль telegram аккаунтов",
    ],
    pains: [
      "Новые аккаунты получают ограничения из-за резкого старта.",
      "Команда не видит FloodWait, ошибки и статусы в одном месте.",
      "Паузы и лимиты задаются вручную и легко сбиваются.",
    ],
    workflow: [
      {
        title: "Проверка аккаунтов",
        text: "Смотрите статус, историю ошибок и готовность аккаунта к кампании.",
      },
      {
        title: "Постепенная активность",
        text: "Используйте паузы, лимиты и осторожные сценарии вместо резких всплесков.",
      },
      {
        title: "Карантин и логи",
        text: "Проблемные аккаунты должны останавливаться, а ошибки сохраняться для анализа.",
      },
    ],
    faq: [
      {
        question: "Прогрев гарантирует отсутствие бана?",
        answer:
          "Нет. Решения принимает Telegram. Прогрев и лимиты снижают хаос, но не являются гарантией.",
      },
      {
        question: "Можно ли обходить ограничения Telegram?",
        answer:
          "Нет. Платформа не предназначена для обхода ограничений и должна использоваться в рамках правил Telegram.",
      },
    ],
  },
  "telegram-marketing-platform": {
    slug: "telegram-marketing-platform",
    badge: "Telegram-платформа",
    title: "Платформа Telegram-маркетинга для команд и агентств",
    description:
      "demo-platform - платформа Telegram-маркетинга: аккаунты, источники, AI-комментарии, прогрев, кампании, UTM-ссылки, отчеты и доступы.",
    intro:
      "Когда Telegram-маркетинг становится регулярным процессом, нужны не отдельные скрипты, а платформа с ролями, workspace, логами, лимитами и аналитикой.",
    primaryKeyword: "платформа Telegram-маркетинга",
    keywords: [
      "платформа telegram маркетинга",
      "telegram marketing platform",
      "сервис telegram маркетинга",
      "кабинет telegram маркетинга",
      "telegram для агентства",
      "управление telegram кампаниями",
    ],
    pains: [
      "Агентство ведет клиентов в разных таблицах и чатах.",
      "Невозможно быстро понять статус кампаний и аккаунтов.",
      "Нет единой связки между источниками, комментариями и заявками.",
    ],
    workflow: [
      {
        title: "Workspace",
        text: "Разделяйте клиентов, аккаунты, кампании, источники и доступы.",
      },
      {
        title: "Модули",
        text: "Подключайте AI, прогрев, рассылки, парсинг и отчеты по мере готовности.",
      },
      {
        title: "Отчеты",
        text: "Смотрите статусы, клики, цели, ошибки и динамику запусков.",
      },
    ],
    faq: [
      {
        question: "Чем платформа лучше набора скриптов?",
        answer:
          "Платформа дает единый кабинет, роли, логи, лимиты, историю и понятный контроль кампаний.",
      },
      {
        question: "Можно ли вести несколько клиентов?",
        answer:
          "Да, workspace-подход рассчитан на разделение проектов и командный доступ.",
      },
    ],
  },
  "telegram-comment-bot": {
    slug: "telegram-comment-bot",
    badge: "Бот для комментариев",
    title: "Бот для комментариев в Telegram с AI и лимитами",
    description:
      "demo-platform помогает запускать Telegram-комментарии аккуратно: AI-профиль, источники, лимиты, UTM-ссылки, логи и отчеты без массового однотипного спама.",
    intro:
      "Когда ищут бота для комментариев в Telegram, чаще всего нужен не бездумный автопостинг, а управляемый процесс: где писать, от какого аккаунта, каким тоном, с какими паузами и как измерять результат.",
    primaryKeyword: "бот для комментариев в Telegram",
    keywords: [
      "бот для комментариев telegram",
      "бот комментариев телеграм",
      "telegram comment bot",
      "ai бот для комментариев",
      "автокомментарии telegram",
      "комментарии в telegram с ai",
    ],
    pains: [
      "Обычные боты пишут одинаково и быстро выглядят как спам.",
      "Не видно, какие источники, аккаунты и тексты реально дают переходы.",
      "Нет понятных лимитов, пауз, логов ошибок и безопасной остановки кампании.",
    ],
    workflow: [
      {
        title: "Контекст вместо шаблона",
        text: "AI-профиль учитывает тему поста, роль аккаунта, оффер и запреты на агрессивные формулировки.",
      },
      {
        title: "Лимиты и контроль",
        text: "Кампания должна работать через паузы, статусы, FloodWait-события и остановку при первых признаках риска.",
      },
      {
        title: "Аналитика",
        text: "UTM-метки, короткие ссылки и отчеты помогают сравнивать источники, аккаунты и варианты комментариев.",
      },
    ],
    faq: [
      {
        question: "Это обычный Telegram-бот для спама?",
        answer:
          "Нет. demo-platform позиционируется как управляемая платформа для контекстных комментариев, источников, лимитов и отчетов, а не как инструмент массового одинакового спама.",
      },
      {
        question: "Можно ли проверять комментарии перед публикацией?",
        answer:
          "Да. Для рискованных сценариев лучше использовать ручную проверку, лимиты и журнал действий, чтобы не публиковать слабые или повторяющиеся ответы.",
      },
    ],
  },
  "telegram-auto-comments": {
    slug: "telegram-auto-comments",
    badge: "Автокомментарии",
    title: "Автокомментарии в Telegram: AI, источники и безопасные паузы",
    description:
      "Сервис автокомментариев в Telegram для команд: подбор источников, AI-ответы по контексту, паузы, UTM-метки, события Telegram и аналитика кампаний.",
    intro:
      "Автокомментарии полезны только тогда, когда они не превращаются в поток одинаковых рекламных фраз. В demo-platform сценарий строится вокруг источников, контекста, лимитов и измеримой воронки.",
    primaryKeyword: "автокомментарии в Telegram",
    keywords: [
      "автокомментарии telegram",
      "автоматические комментарии telegram",
      "автокомменты телеграм",
      "ai автокомментарии",
      "нейрокомментинг telegram",
      "сервис комментариев telegram",
    ],
    pains: [
      "Команда вручную мониторит посты и не успевает реагировать вовремя.",
      "Автоматические ответы без контекста раздражают аудиторию и модераторов.",
      "Без UTM и целей непонятно, какие комментарии приводят регистрации.",
    ],
    workflow: [
      {
        title: "Выбор источников",
        text: "Соберите каналы, обсуждения и чаты, где реально есть релевантный спрос, а не случайный трафик.",
      },
      {
        title: "AI-ответы и правила",
        text: "Задайте тон, стоп-слова, формат CTA, запрет повторов и условия, когда комментарий лучше пропустить.",
      },
      {
        title: "Оценка результата",
        text: "Смотрите переходы, цели, ошибки Telegram, жалобы, удаления и signup-события по каждой кампании.",
      },
    ],
    faq: [
      {
        question: "Автокомментарии можно запускать сразу на большой объем?",
        answer:
          "Лучше нет. Стартуйте с малых лимитов, проверяйте качество источников и реакцию аудитории, затем постепенно расширяйте кампанию.",
      },
      {
        question: "Как понять, что комментарии работают?",
        answer:
          "Нужны UTM-метки, цели Метрики, отчет по источникам и учет не только кликов, но и регистраций, действий в кабинете и ошибок Telegram.",
      },
    ],
  },
  "telegram-marketing-agency": {
    slug: "telegram-marketing-agency",
    badge: "Для агентств",
    title: "Telegram-маркетинг для агентств: кампании, доступы и отчетность",
    description:
      "demo-platform помогает агентствам вести Telegram-маркетинг в отдельных кабинетах клиентов: источники, аккаунты, AI-комментарии, UTM-ссылки, логи и отчеты.",
    intro:
      "Агентству нужен не очередной скрипт, а повторяемый процесс: отдельный workspace для клиента, роли команды, понятные источники, безопасные лимиты и отчетность, в которой виден путь до регистрации.",
    primaryKeyword: "Telegram-маркетинг для агентств",
    keywords: [
      "telegram маркетинг для агентств",
      "автоматизация telegram для агентства",
      "ведение telegram кампаний",
      "отчетность telegram маркетинга",
      "telegram маркетинг для клиентов",
    ],
    pains: [
      "Данные клиентов, доступы команды и кампании смешиваются в одной таблице.",
      "Нельзя уверенно объяснить клиенту, какие источники привели регистрации.",
      "Запуск зависит от ручной памяти менеджера, а не от повторяемого процесса.",
    ],
    workflow: [
      {
        title: "Отдельный кабинет клиента",
        text: "Разделяйте доступы, аккаунты, источники и кампании между клиентскими проектами.",
      },
      {
        title: "Управляемые гипотезы",
        text: "Фиксируйте оффер, аудиторию, UTM-разметку и ограничения до запуска, а не после него.",
      },
      {
        title: "Отчет по воронке",
        text: "Сопоставляйте действия в Telegram с переходами, регистрациями и дальнейшей активацией в продукте.",
      },
    ],
    faq: [
      {
        question: "Можно ли вести несколько клиентов?",
        answer:
          "Да. Workspace-модель нужна именно для разделения клиентских проектов, участников и данных кампаний.",
      },
      {
        question: "Как показывать результат клиенту?",
        answer:
          "Через уникальные UTM-ссылки, цели аналитики, логи кампании и отчет по источникам. Не стоит выдавать сырые HTTP-запросы или показы ботов за лиды.",
      },
    ],
  },
  "telegram-lead-generation-agency": {
    slug: "telegram-lead-generation-agency",
    badge: "Лидогенерация",
    title: "Лидогенерация в Telegram для агентств с UTM и целями",
    description:
      "Сценарий лидогенерации в Telegram для агентств: подбор источников, контекстные AI-комментарии, UTM-разметка, цели Метрики и отчет по воронке.",
    intro:
      "Лидогенерация в Telegram начинается с релевантных обсуждений и полезного ответа, а не с массовой рассылки. demo-platform помогает проверить гипотезу небольшим измеримым запуском.",
    primaryKeyword: "лидогенерация в Telegram для агентств",
    keywords: [
      "лидогенерация telegram агентство",
      "лиды из telegram для бизнеса",
      "заявки из telegram чатов",
      "utm telegram",
      "аналитика лидов telegram",
    ],
    pains: [
      "Переходы есть, но источник и конкретный сценарий не определены.",
      "Одинаковые рекламные сообщения снижают доверие и вызывают жалобы.",
      "Клиент видит клики, но не понимает, дошли ли люди до регистрации.",
    ],
    workflow: [
      {
        title: "Карта спроса",
        text: "Сначала выберите каналы и чаты, где пользователи уже обсуждают задачу клиента.",
      },
      {
        title: "Контекстный ответ",
        text: "AI-профиль задает полезность, тон, стоп-слова и условия, когда лучше не публиковаться.",
      },
      {
        title: "Измеримая воронка",
        text: "Каждая ссылка получает UTM, а результат оценивается по signup и активации, а не только по переходу.",
      },
    ],
    faq: [
      {
        question: "Можно ли сразу масштабировать кампанию?",
        answer:
          "Нет. Сначала подтвердите качество источников и сообщения на малом объеме, затем постепенно расширяйте только рабочий сценарий.",
      },
      {
        question: "Какая цель важнее кликов?",
        answer:
          "Регистрация, первая полезная операция и оплата. Клик без следующего шага не доказывает ценность канала.",
      },
    ],
  },
  "telegram-campaign-analytics": {
    slug: "telegram-campaign-analytics",
    badge: "Аналитика",
    title: "Аналитика Telegram-кампаний: UTM, цели и отчеты",
    description:
      "demo-platform связывает Telegram-кампании с UTM-разметкой, целями аналитики, логами публикаций и отчетами по источникам, аккаунтам и сценариям.",
    intro:
      "Telegram-кампания становится управляемой, когда у источника, аккаунта и сценария есть свой идентификатор, а переходы можно связать с регистрациями и дальнейшими действиями пользователя.",
    primaryKeyword: "аналитика Telegram-кампаний",
    keywords: [
      "аналитика telegram кампаний",
      "utm метки telegram",
      "отчетность telegram маркетинга",
      "цели метрики telegram",
      "эффективность telegram источников",
    ],
    pains: [
      "UTM-метки используются бессистемно и не позволяют сравнить сценарии.",
      "Метрика показывает визит, но не путь пользователя до активации или оплаты.",
      "Логи Telegram и веб-аналитика существуют отдельно и не складываются в решение.",
    ],
    workflow: [
      {
        title: "Единая схема UTM",
        text: "Закрепите за каждым тестом source, medium, campaign и content, чтобы отчеты не смешивали трафик.",
      },
      {
        title: "События воронки",
        text: "Отслеживайте регистрацию, подключение аккаунта, запуск первой кампании, checkout и успешную оплату.",
      },
      {
        title: "Еженедельный разбор",
        text: "Сравнивайте поисковые показы, органические визиты, регистрации и активации по одному набору метрик.",
      },
    ],
    faq: [
      {
        question: "Достаточно ли смотреть просмотры и клики?",
        answer:
          "Нет. Эти показатели помогают заметить интерес, но решение о масштабе нужно принимать по качественным регистрациям, активациям и оплатам.",
      },
      {
        question: "Как избежать потери UTM?",
        answer:
          "Используйте единый шаблон, не меняйте значения в середине теста и сохраняйте исходную ссылку рядом с настройками кампании.",
      },
    ],
  },
};

type BlogArticleData = {
  slug: string;
  badge: string;
  title: string;
  description: string;
  intro: string;
  category: string;
  readTime: string;
  publishedAt: string;
  keywords: string[];
  sections: { title: string; paragraphs: string[]; bullets?: string[] }[];
  faq: { question: string; answer: string }[];
  related: { to: string; label: string }[];
};

export const blogArticles: Record<string, BlogArticleData> = {
  "telegram-limits-floodwait": {
    slug: "telegram-limits-floodwait",
    badge: "Лимиты Telegram",
    title: "FloodWait и лимиты Telegram: как запускать кампании без резких всплесков",
    description:
      "Практический разбор FloodWait, лимитов Telegram, пауз между действиями, логов аккаунтов и безопасного старта Telegram-кампаний.",
    intro:
      "FloodWait обычно появляется не из-за одной кнопки, а из-за суммарной картины: возраст аккаунта, история номера, скорость действий, одинаковые тексты, качество прокси и жалобы. Поэтому полезнее управлять риском заранее, а не чинить кампанию после блокировки.",
    category: "Безопасность",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "FloodWait Telegram",
      "лимиты Telegram",
      "безопасная автоматизация Telegram",
      "ошибки Telegram аккаунтов",
      "прогрев Telegram",
    ],
    sections: [
      {
        title: "Что означает FloodWait",
        paragraphs: [
          "FloodWait - это сигнал Telegram, что аккаунт должен подождать перед следующим действием. В маркетинговых сценариях он часто появляется, когда активность растет быстрее, чем аккаунт выглядит естественно для площадки.",
          "Нельзя считать FloodWait простой технической ошибкой. Это ограничение платформы, и правильная реакция - остановить или замедлить сценарий, сохранить событие в логах и не пытаться повторять действие в цикле.",
        ],
        bullets: [
          "фиксируйте код ошибки и длительность ожидания",
          "снижайте частоту действий по аккаунту",
          "разделяйте новые и старые аккаунты",
          "не отправляйте одинаковые сообщения и ссылки массово",
        ],
      },
      {
        title: "Какие факторы повышают риск",
        paragraphs: [
          "Риск растет, когда новый аккаунт сразу получает коммерческую нагрузку, подключается к большому числу источников, пишет однотипные комментарии или работает с нестабильного IP. Даже хорошая AI-генерация не спасает, если операционный контур выглядит агрессивно.",
          "В demo-platform мы смотрим на лимиты, статусы, ошибки и журнал действий как на единую систему. Это помогает не путать рабочую кампанию с хаотичным скриптом.",
        ],
        bullets: [
          "резкий старт после регистрации аккаунта",
          "однотипные CTA и короткие ссылки без контекста",
          "частая смена прокси или подозрительная география",
          "работа без stop-loss после первых ограничений",
        ],
      },
      {
        title: "Как запускать аккуратнее",
        paragraphs: [
          "Начинайте с малых объемов, проверяйте реакцию источников, не смешивайте прогрев и агрессивную кампанию в одном окне. Для новых аккаунтов важнее стабильность поведения, чем количество действий в первый день.",
          "В отчете должны быть видны не только успешные публикации, но и пропуски, паузы, причины остановок и ошибки Telegram. Именно эти данные позволяют масштабировать кампанию без слепого увеличения лимитов.",
        ],
      },
    ],
    faq: [
      {
        question: "Можно ли полностью убрать FloodWait?",
        answer:
          "Нет. Решение принимает Telegram. Можно только снизить вероятность и корректно реагировать на ограничение.",
      },
      {
        question: "Что делать после первого FloodWait?",
        answer:
          "Остановить повтор действия, сохранить событие, снизить частоту по аккаунту и проверить связку аккаунт-прокси-источник-текст.",
      },
    ],
    related: [
      { to: "/telegram-account-warmup", label: "Прогрев аккаунтов" },
      { to: "/help/safety", label: "Инструкция по безопасности" },
    ],
  },
  "ai-comments-telegram-guide": {
    slug: "ai-comments-telegram-guide",
    badge: "AI-комментарии",
    title: "AI-комментарии в Telegram: как писать по контексту, а не спамить",
    description:
      "Гайд по AI-комментариям в Telegram: источники, контекст поста, tone of voice, ограничения, UTM-ссылки и проверка результата.",
    intro:
      "AI-комментарии работают только тогда, когда они похожи на осмысленное участие в обсуждении. Если модель пишет одинаковые рекламные фразы, площадка и люди быстро воспринимают это как спам.",
    category: "Нейрокомментинг",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "AI комментарии Telegram",
      "нейрокомментинг",
      "комментарии в Telegram чатах",
      "автоматизация комментариев",
      "Telegram маркетинг",
    ],
    sections: [
      {
        title: "Начинайте с источников, а не с промпта",
        paragraphs: [
          "Качество комментариев зависит от того, где они публикуются. Один и тот же оффер может быть уместен в профессиональном чате и раздражать в новостном обсуждении. Поэтому перед генерацией нужно понять тему, правила, активность и формат площадки.",
          "Хороший источник - это не просто канал с аудиторией. Это место, где ваш комментарий может добавить пользу, уточнение, пример или аккуратный переход к решению.",
        ],
      },
      {
        title: "Что должен знать AI-профиль",
        paragraphs: [
          "AI-профиль должен описывать роль, запреты, тон, целевое действие и границы обещаний. Важно прямо запрещать агрессивные коммерческие формулировки, повтор одинаковых ссылок и комментарии без связи с постом.",
          "В demo-platform такой профиль связывается с кампанией, источниками, лимитами и отчетами, чтобы команда видела не только текст, но и контекст его публикации.",
        ],
        bullets: [
          "кто говорит и почему ему можно доверять",
          "какие темы допустимы",
          "какие обещания запрещены",
          "когда лучше ничего не писать",
        ],
      },
      {
        title: "Как измерять результат",
        paragraphs: [
          "Для Telegram-кампаний мало смотреть только на количество комментариев. Нужны UTM-метки, короткие ссылки, цели в Метрике, переходы на посадочные страницы и события в кабинете.",
          "Если комментарии дают клики, но не дают регистрации, проблема может быть в оффере, посадочной странице или активации после signup. SEO и аналитика здесь должны работать вместе.",
        ],
      },
    ],
    faq: [
      {
        question: "AI-комментарии безопасны?",
        answer:
          "Они безопаснее ручного хаоса только при лимитах, контексте, модерации промпта и уважении правил Telegram и чатов.",
      },
      {
        question: "Нужно ли ставить ссылку в каждый комментарий?",
        answer:
          "Нет. Частые одинаковые ссылки выглядят как реклама. Лучше чередовать полезные ответы, мягкие переходы и комментарии без ссылки.",
      },
    ],
    related: [
      { to: "/neurocommenting", label: "Страница про нейрокомментинг" },
      { to: "/help/ai-comments", label: "Инструкция AI-комментариев" },
    ],
  },
  "utm-telegram-campaigns": {
    slug: "utm-telegram-campaigns",
    badge: "Аналитика",
    title: "UTM-метки для Telegram-кампаний: как понять, откуда пришла регистрация",
    description:
      "Как размечать Telegram-кампании UTM-метками, разделять аккаунты и источники, смотреть переходы в Яндекс.Метрике и связывать их с регистрациями.",
    intro:
      "Telegram часто дает переходы без понятного реферера. Если не разметить ссылки заранее, потом сложно отличить хороший источник от случайного клика или внутренней сессии.",
    category: "Аналитика",
    readTime: "6 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "UTM Telegram",
      "метки для Telegram кампаний",
      "Яндекс Метрика Telegram",
      "аналитика Telegram маркетинга",
      "отследить переходы из Telegram",
    ],
    sections: [
      {
        title: "Минимальная структура UTM",
        paragraphs: [
          "Для Telegram-маркетинга обычно хватает четырех параметров: source, medium, campaign и content. Source показывает площадку, medium - тип активности, campaign - запуск, content - аккаунт, источник или вариант текста.",
          "Важно договориться о словаре заранее. Если один аккаунт называется account_01, а второй acc1, отчет будет дробиться и станет менее полезным.",
        ],
        bullets: [
          "utm_source=telegram",
          "utm_medium=neurocommenting или broadcast",
          "utm_campaign=название запуска",
          "utm_content=аккаунт, источник или вариант",
        ],
      },
      {
        title: "Что смотреть в Метрике",
        paragraphs: [
          "Переходы сами по себе не равны результату. Смотрите глубину, время, посадочные страницы, достижение цели signup_submit, checkout и дальнейшую активацию в продукте.",
          "Если в Метрике много dashboard-сессий, отделяйте внутреннюю операционную активность от холодного трафика. Иначе рекламный канал будет казаться сильнее, чем он есть.",
        ],
      },
      {
        title: "Как связать UTM с продуктовой активацией",
        paragraphs: [
          "Лучший отчет соединяет UTM, регистрацию, workspace и первое полезное действие: добавление аккаунта, источника, кампании или оплату. Тогда видно не только кто кликнул, но и кто начал пользоваться.",
          "Для demo-platform это особенно важно: регистрация без добавления Telegram-аккаунта не означает, что пользователь дошел до ценности продукта.",
        ],
      },
    ],
    faq: [
      {
        question: "Почему Telegram-переходы иногда попадают в direct?",
        answer:
          "Мессенджеры и браузеры не всегда передают реферер, поэтому без UTM часть переходов выглядит как прямой заход.",
      },
      {
        question: "Нужно ли делать отдельную ссылку на каждый аккаунт?",
        answer:
          "Да, если вы хотите понять, какой аккаунт или вариант текста дает клики и регистрации.",
      },
    ],
    related: [
      { to: "/telegram-lead-generation", label: "Лиды из Telegram" },
      { to: "/use-cases", label: "Сценарии кампаний" },
    ],
  },
  "telegram-account-warmup-guide": {
    slug: "telegram-account-warmup-guide",
    badge: "Прогрев",
    title: "Прогрев Telegram-аккаунтов: что реально стоит контролировать",
    description:
      "Практическое руководство по прогреву Telegram-аккаунтов: возраст, прокси, лимиты, stop-loss, журнал действий и безопасное повышение активности.",
    intro:
      "Прогрев не магическая защита от ограничений, а дисциплина постепенного поведения. Он помогает команде не запускать новые аккаунты в боевую нагрузку без истории и контроля.",
    category: "Аккаунты",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "прогрев Telegram аккаунтов",
      "Telegram аккаунты для маркетинга",
      "лимиты аккаунтов Telegram",
      "безопасный старт Telegram",
      "аккаунты Telegram прокси",
    ],
    sections: [
      {
        title: "Что прогревать на самом деле",
        paragraphs: [
          "Прогревается не только количество действий. Важны стабильность сессии, прокси, возраст аккаунта, география, темп вступлений, реакции на ошибки и отсутствие однотипной коммерческой нагрузки.",
          "Если команда видит только итоговое число действий, она пропускает главный риск: аккаунт может выполнять план, но накапливать подозрительные события.",
        ],
      },
      {
        title: "Stop-loss важнее максимального лимита",
        paragraphs: [
          "У каждого прогрева должны быть условия остановки: FloodWait, ошибки авторизации, подозрительный статус, жалобы, нестабильный прокси или резкое падение успешных действий.",
          "Без stop-loss прогрев превращается в генератор риска. Система должна уметь не только делать действия, но и вовремя молчать.",
        ],
        bullets: [
          "останавливайте аккаунт после критических ошибок",
          "не повышайте лимиты каждый день автоматически",
          "проверяйте историю перед переводом в кампанию",
          "разделяйте прогрев и активную рекламную нагрузку",
        ],
      },
      {
        title: "Когда аккаунт готов к кампании",
        paragraphs: [
          "Готовность - это не возраст в днях, а сочетание истории, стабильного прокси, отсутствия свежих ограничений, понятного расписания и малых тестов на релевантных источниках.",
          "В demo-platform прогрев связан с аккаунтами, источниками и логами, поэтому решение о запуске можно принимать по фактам, а не по ощущениям.",
        ],
      },
    ],
    faq: [
      {
        question: "Сколько дней нужно прогревать аккаунт?",
        answer:
          "Единого числа нет. Новый аккаунт лучше вести медленно и смотреть на статусы, ошибки, возраст, прокси и реакцию источников.",
      },
      {
        question: "Прогрев нужен старым аккаунтам?",
        answer:
          "Да, если аккаунт долго не использовался или меняет сценарий активности. Резкий старт опасен и для старых аккаунтов.",
      },
    ],
    related: [
      { to: "/help/warmup", label: "Инструкция по прогреву" },
      { to: "/telegram-limits-floodwait", label: "FloodWait и лимиты" },
    ],
  },
  "telegram-marketing-safety": {
    slug: "telegram-marketing-safety",
    badge: "Безопасный маркетинг",
    title: "Безопасный Telegram-маркетинг: как не превратить автоматизацию в спам",
    description:
      "Принципы безопасного Telegram-маркетинга: релевантные источники, контекстные сообщения, лимиты, запрет спама, журнал действий и уважение правил площадки.",
    intro:
      "Автоматизация усиливает и хорошие, и плохие процессы. Если стратегия строится на массовых одинаковых сообщениях, инструмент только быстрее приведет к жалобам. Поэтому SEO, продукт и Telegram-операции должны опираться на пользу и контроль.",
    category: "Стратегия",
    readTime: "6 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "Telegram маркетинг безопасно",
      "антиспам Telegram",
      "автоматизация Telegram без спама",
      "Telegram продвижение",
      "нейрокомментинг безопасно",
    ],
    sections: [
      {
        title: "Релевантность важнее объема",
        paragraphs: [
          "Лучше написать десять уместных комментариев в правильных обсуждениях, чем сотню одинаковых сообщений в случайных чатах. Релевантность снижает жалобы и повышает шанс перехода.",
          "Для SEO это тоже работает: качественные страницы и профильные размещения полезнее массовых каталогов без аудитории.",
        ],
      },
      {
        title: "Что нельзя автоматизировать без контроля",
        paragraphs: [
          "Нельзя запускать без контроля коммерческие сообщения незнакомым людям, однотипные ссылки, агрессивные обещания, обход модерации и повтор действий после ограничений Telegram.",
          "Даже если технически действие возможно, продукт должен помогать команде выбрать безопасный режим и видеть последствия.",
        ],
        bullets: [
          "массовые одинаковые ссылки",
          "обещания обхода ограничений",
          "публикации без учета правил чата",
          "повтор после FloodWait или жалоб",
        ],
      },
      {
        title: "Как выглядит здоровый процесс",
        paragraphs: [
          "Здоровый Telegram-маркетинг начинается с гипотезы, списка релевантных источников, ограниченного теста, UTM-меток, логов и разбора результата. Только после этого можно расширять объем.",
          "demo-platform полезен именно как операционный слой: не просто генерировать текст, а связывать аккаунты, источники, лимиты, отчеты и аналитику.",
        ],
      },
    ],
    faq: [
      {
        question: "Можно ли продвигаться через каталоги?",
        answer:
          "Можно точечно, если каталог живой и тематический. Массовые автоматические прогоны лучше не делать основной SEO-стратегией.",
      },
      {
        question: "Что считать хорошим первым тестом?",
        answer:
          "Небольшой запуск на релевантных источниках с разными UTM, понятным оффером, лимитами и разбором регистраций.",
      },
    ],
    related: [
      { to: "/blog/utm-telegram-campaigns", label: "UTM для Telegram" },
      { to: "/platform", label: "Платформа demo-platform" },
    ],
  },
  "yandex-metrika-telegram-traffic": {
    slug: "yandex-metrika-telegram-traffic",
    badge: "Метрика",
    title: "Как читать Telegram-трафик в Яндекс.Метрике и не перепутать его с internal",
    description:
      "Как анализировать Telegram-переходы в Яндекс.Метрике: UTM, direct, referrer, internal traffic, цели регистрации и активация пользователя после signup.",
    intro:
      "В Telegram-маркетинге легко увидеть много визитов и решить, что канал работает. Но часть трафика может быть direct, internal, dashboard-сессиями или повторными заходами команды. Поэтому Метрику нужно читать через источники, цели и активацию.",
    category: "Аналитика",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "Яндекс Метрика Telegram",
      "Telegram трафик Метрика",
      "direct трафик Telegram",
      "UTM Метрика Telegram",
      "аналитика Telegram кампаний",
    ],
    sections: [
      {
        title: "Почему Telegram часто выглядит как direct",
        paragraphs: [
          "Мессенджеры и мобильные браузеры не всегда передают реферер. Если ссылка не размечена UTM-метками, переход может попасть в прямые заходы, хотя фактически пользователь пришел из Telegram.",
          "Поэтому для каждой кампании лучше заранее создавать короткие ссылки с utm_source, utm_medium, utm_campaign и utm_content. Это позволяет отделить аккаунты, источники и варианты текстов.",
        ],
        bullets: [
          "direct не всегда означает ввод адреса руками",
          "referrer может теряться между Telegram и браузером",
          "utm_content помогает сравнить аккаунты и тексты",
          "короткие ссылки проще использовать в комментариях",
        ],
      },
      {
        title: "Какие отчеты смотреть первыми",
        paragraphs: [
          "Начинайте с источников, посадочных страниц, поисковых фраз, устройств, географии и целей. Если видите длинные dashboard-сессии, отделяйте их от холодного маркетингового трафика.",
          "Для demo-platform важны не только визиты, но и цели signup_submit, переход в кабинет, добавление аккаунта, источника или кампании. Без этого можно оптимизировать клики, которые не приводят к продуктовой ценности.",
        ],
      },
      {
        title: "Как понять, что SEO или Telegram реально сработали",
        paragraphs: [
          "Хороший сигнал - это цепочка: источник или поисковый запрос, релевантная посадочная, регистрация, создание workspace и первое полезное действие. Если регистрация есть, но активации нет, надо улучшать onboarding, а не просто закупать больше трафика.",
          "Такой разбор помогает не спорить ощущениями. Команда видит, какой канал дал не только визит, но и пользователя, который начал работать в сервисе.",
        ],
      },
    ],
    faq: [
      {
        question: "Почему в Метрике много визитов, но мало регистраций?",
        answer:
          "Переходы могут быть внутренними, повторными или нецелевыми. Смотрите цели signup_submit и продуктовые действия после регистрации.",
      },
      {
        question: "Можно ли анализировать Telegram без UTM?",
        answer:
          "Можно частично, но отчет будет шумным. UTM нужны, чтобы отделять кампании, аккаунты, источники и варианты текста.",
      },
    ],
    related: [
      { to: "/blog/utm-telegram-campaigns", label: "UTM для Telegram" },
      { to: "/telegram-lead-generation", label: "Лиды из Telegram" },
    ],
  },
  "neurocommenting-vs-spam": {
    slug: "neurocommenting-vs-spam",
    badge: "Позиционирование",
    title: "Нейрокомментинг и спам: где проходит граница",
    description:
      "Чем аккуратный нейрокомментинг отличается от спама: контекст, полезность, лимиты, правила чатов, UTM, логи и отказ от массовых одинаковых ссылок.",
    intro:
      "Нейрокомментинг не должен означать поток одинаковых рекламных сообщений. Его сильная сторона - быстро находить релевантные обсуждения и помогать команде отвечать по теме, с ограничениями и измерением результата.",
    category: "Нейрокомментинг",
    readTime: "6 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "нейрокомментинг спам",
      "нейрокомментинг Telegram",
      "AI комментарии без спама",
      "безопасный нейрокомментинг",
      "комментарии Telegram продвижение",
    ],
    sections: [
      {
        title: "Спам начинается там, где исчезает контекст",
        paragraphs: [
          "Если комментарий можно вставить под любой пост без изменений, он почти наверняка выглядит как спам. Хороший AI-комментарий должен учитывать тему поста, аудиторию, тон обсуждения и правила площадки.",
          "Вместо одинакового CTA лучше использовать разные роли: уточнение, полезный пример, ответ на вопрос, мягкая ссылка на материал или вообще комментарий без ссылки.",
        ],
      },
      {
        title: "Что делает процесс безопаснее",
        paragraphs: [
          "Безопасность складывается из ограничений: выбор источников, паузы, дневные лимиты, запреты в промпте, stop-loss после ошибок и журнал действий. Это не гарантия отсутствия жалоб, но это превращает кампанию в управляемый процесс.",
          "demo-platform не обещает обход антиспама Telegram. Задача платформы - помочь работать аккуратнее и видеть, где риск становится выше результата.",
        ],
        bullets: [
          "пишите только в релевантных обсуждениях",
          "не ставьте ссылку в каждый комментарий",
          "не повторяйте один текст массово",
          "останавливайте аккаунт после ограничений",
        ],
      },
      {
        title: "Как измерять качество нейрокомментинга",
        paragraphs: [
          "Качество - это не только количество опубликованных комментариев. Смотрите переходы по UTM, глубину просмотра, signup_submit, ответы людей, жалобы и ошибки Telegram.",
          "Если комментарии дают клики без регистрации, надо проверять оффер и посадочную. Если дают регистрации без активации, надо чинить onboarding и первый шаг внутри продукта.",
        ],
      },
    ],
    faq: [
      {
        question: "Нейрокомментинг легален?",
        answer:
          "Сам по себе инструмент не нарушает правила. Нарушения начинаются, когда его используют для спама, обмана, массовых нежелательных сообщений или обхода ограничений площадки.",
      },
      {
        question: "Нужна ли ручная модерация AI-комментариев?",
        answer:
          "На старте да. Когда промпты, источники и лимиты проверены, часть сценариев можно автоматизировать осторожнее.",
      },
    ],
    related: [
      { to: "/neurocommenting", label: "Нейрокомментинг" },
      { to: "/blog/telegram-marketing-safety", label: "Безопасный Telegram-маркетинг" },
    ],
  },
  "telegram-campaign-reporting": {
    slug: "telegram-campaign-reporting",
    badge: "Отчеты",
    title: "Отчеты Telegram-кампаний: какие KPI смотреть после запуска",
    description:
      "Какие KPI нужны для Telegram-кампаний: источники, комментарии, клики, UTM, регистрации, активация workspace, ошибки Telegram и стоимость результата.",
    intro:
      "Отчет по Telegram-кампании должен показывать не только сколько комментариев отправлено. Важно понимать, какие источники дали переходы, где пользователи зарегистрировались, что сломалось по аккаунтам и дошел ли новый пользователь до первого действия в продукте.",
    category: "Аналитика",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "отчет Telegram кампании",
      "KPI Telegram маркетинга",
      "аналитика Telegram кампаний",
      "эффективность Telegram продвижения",
      "отчеты нейрокомментинг",
    ],
    sections: [
      {
        title: "Разделяйте операционные и бизнес-метрики",
        paragraphs: [
          "Операционные метрики отвечают на вопрос, была ли кампания технически выполнена: аккаунты, источники, комментарии, ошибки, FloodWait, пропуски и статусы публикаций.",
          "Бизнес-метрики отвечают на другой вопрос: появились ли переходы, регистрации, оплаты или хотя бы активированные workspace. Если смешать эти уровни, можно радоваться успешной отправке комментариев, которые не дали результата.",
        ],
        bullets: [
          "операционные KPI: отправки, ошибки, паузы, статусы аккаунтов",
          "маркетинговые KPI: клики, глубина, цели, регистрация",
          "продуктовые KPI: workspace, аккаунт, источник, кампания",
          "финальные KPI: оплата, LTV, повторный запуск",
        ],
      },
      {
        title: "Минимальная таблица отчета",
        paragraphs: [
          "В базовом отчете должны быть источник, аккаунт, UTM, опубликованный текст или ссылка на действие, статус, ошибка, переходы, достижение цели и следующий шаг пользователя. Так команда видит причинно-следственную цепочку.",
          "Если отчет заканчивается на количестве комментариев, он полезен только оператору. Для маркетолога и собственника важнее связка источник - клик - регистрация - активация.",
        ],
      },
      {
        title: "Как использовать отчет для SEO и контента",
        paragraphs: [
          "Запросы, которые дают переходы и регистрации, стоит превращать в статьи, FAQ и посадочные страницы. Если люди кликают на тему FloodWait, UTM или нейрокомментинга без спама, эти темы должны получать отдельные страницы и внутренние ссылки.",
          "Так Telegram-кампании становятся не только каналом лидов, но и источником данных для бесплатного SEO-контента.",
        ],
      },
    ],
    faq: [
      {
        question: "Какой KPI главный для Telegram-кампании?",
        answer:
          "Главный KPI зависит от этапа. На старте важны клики и регистрации, но для продукта сильнее всего активация: пользователь создал workspace и сделал первое полезное действие.",
      },
      {
        question: "Нужно ли учитывать ошибки Telegram в маркетинговом отчете?",
        answer:
          "Да. Ошибки, FloodWait и ограничения объясняют, почему кампания не масштабируется или почему часть источников не дала результата.",
      },
    ],
    related: [
      { to: "/blog/yandex-metrika-telegram-traffic", label: "Метрика и Telegram-трафик" },
      { to: "/blog/utm-telegram-campaigns", label: "UTM для Telegram" },
    ],
  },
  "telegram-source-selection": {
    slug: "telegram-source-selection",
    badge: "Источники",
    title: "Как выбирать Telegram-источники для AI-комментариев и кампаний",
    description:
      "Как выбирать каналы, чаты и обсуждения для Telegram-кампаний: релевантность, активность, правила, комментарии, риски, UTM и качество источника.",
    intro:
      "Сильная Telegram-кампания начинается не с текста комментария, а с выбора источников. Если площадка нерелевантна, даже аккуратный AI-комментарий будет выглядеть чужеродно и не даст качественных переходов.",
    category: "Источники",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "Telegram источники для рекламы",
      "как выбрать Telegram каналы",
      "источники для нейрокомментинга",
      "Telegram чаты для продвижения",
      "поиск Telegram источников",
    ],
    sections: [
      {
        title: "Сначала проверяйте релевантность",
        paragraphs: [
          "Источник должен совпадать с задачей кампании: темой, языком, географией, уровнем аудитории и допустимым форматом общения. Большой канал не всегда лучше небольшого обсуждения, где люди реально задают вопросы.",
          "Для AI-комментариев особенно важен контекст. Если в обсуждении нет боли, вопроса или живой темы, комментарий будет выглядеть рекламной вставкой.",
        ],
        bullets: [
          "тема источника совпадает с оффером",
          "аудитория говорит на нужном языке",
          "комментарии или обсуждения открыты",
          "есть живые реакции, а не только просмотры",
        ],
      },
      {
        title: "Оценивайте правила и риск жалоб",
        paragraphs: [
          "Перед запуском проверьте правила чата, тон модерации и историю обсуждений. Некоторые площадки терпимо относятся к полезным ссылкам, другие удаляют любые внешние переходы.",
          "Если источник агрессивно модерируется или аудитория плохо реагирует на коммерческие намеки, лучше использовать его для анализа спроса, а не для публикации комментариев.",
        ],
        bullets: [
          "есть ли запрет на ссылки",
          "как реагируют администраторы",
          "часто ли удаляют сообщения",
          "можно ли отвечать без прямой рекламы",
        ],
      },
      {
        title: "Как измерять качество источника",
        paragraphs: [
          "Хороший источник дает не только клики. Смотрите глубину просмотра, signup_submit, активацию workspace, ответы людей и ошибки Telegram по аккаунтам. Иногда источник дает мало переходов, но сильные регистрации; иногда наоборот.",
          "Каждый источник лучше размечать отдельным utm_content или короткой ссылкой. Тогда через Метрику и продуктовые события можно понять, какие каналы и чаты стоит расширять.",
        ],
      },
    ],
    faq: [
      {
        question: "Что лучше для старта: каналы или чаты?",
        answer:
          "Для первых тестов часто полезнее чаты и обсуждения, где есть живой контекст. Каналы хороши, если у постов открыты комментарии и аудитория реагирует по теме.",
      },
      {
        question: "Сколько источников нужно для теста?",
        answer:
          "Начните с небольшого списка релевантных источников и разных UTM. Важнее качество реакции и регистраций, чем большой список без понимания результата.",
      },
    ],
    related: [
      { to: "/help/sources", label: "Инструкция по источникам" },
      { to: "/blog/telegram-campaign-reporting", label: "Отчеты Telegram-кампаний" },
    ],
  },
  "telegram-proxy-for-accounts": {
    slug: "telegram-proxy-for-accounts",
    badge: "Прокси",
    title: "Прокси для Telegram-аккаунтов: что важно для кампаний и прогрева",
    description:
      "Как выбирать и контролировать прокси для Telegram-аккаунтов: стабильность, география, связка аккаунт-прокси, ошибки, прогрев и риск ограничений.",
    intro:
      "Прокси не делает Telegram-аккаунт безопасным сам по себе. Он только часть связки: номер, возраст аккаунта, история действий, география, стабильность сессии и темп кампании. Если прокси плохой или постоянно меняется, даже аккуратный сценарий может выглядеть подозрительно.",
    category: "Аккаунты",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "прокси для Telegram аккаунтов",
      "Telegram прокси для маркетинга",
      "прокси Telegram кампании",
      "аккаунт прокси Telegram",
      "прогрев аккаунтов прокси",
    ],
    sections: [
      {
        title: "Стабильность важнее красивого списка IP",
        paragraphs: [
          "Для Telegram-кампаний важнее стабильная связка аккаунт-прокси, чем большое количество случайных IP. Частая смена сети, географии или качества соединения может выглядеть хуже, чем умеренная активность с одного предсказуемого прокси.",
          "Если аккаунт уже прогревался с одной географии, резкий перенос на другую стоит делать осторожно. Сначала лучше проверить авторизацию, ошибки и небольшие безопасные действия.",
        ],
        bullets: [
          "не меняйте прокси без причины",
          "держите географию аккаунта понятной",
          "проверяйте Telegram connectivity перед кампанией",
          "сохраняйте историю ошибок по аккаунту и прокси",
        ],
      },
      {
        title: "Как понять, что прокси создает риск",
        paragraphs: [
          "Прокси становится подозрительным, если через него часто появляются ошибки авторизации, нестабильные сессии, резкие таймауты, одинаковые ограничения на нескольких аккаунтах или ухудшение доставки действий.",
          "Один сбой не всегда означает проблему. Но повторяющийся паттерн по нескольким аккаунтам лучше считать сигналом: прокси надо вынести из активных кампаний и проверить отдельно.",
        ],
        bullets: [
          "несколько аккаунтов получили ограничения на одной связке",
          "частые таймауты и ошибки соединения",
          "география не совпадает с историей аккаунта",
          "прокси используется слишком большим числом аккаунтов",
        ],
      },
      {
        title: "Как связать прокси с отчетами",
        paragraphs: [
          "В отчетах Telegram-кампаний полезно видеть не только источник и текст, но и аккаунт, прокси, ошибку и статус действия. Тогда можно понять, проблема в оффере, источнике, аккаунте или инфраструктуре.",
          "В demo-platform прокси рассматривается как часть операционного контура: аккаунты, прогрев, источники, лимиты, ошибки и отчеты должны анализироваться вместе.",
        ],
      },
    ],
    faq: [
      {
        question: "Можно ли работать без прокси?",
        answer:
          "Для одного собственного аккаунта иногда можно, но для нескольких аккаунтов и кампаний лучше контролировать сетевую связку явно и не смешивать все действия в одной среде.",
      },
      {
        question: "Прокси гарантирует отсутствие блокировок?",
        answer:
          "Нет. Блокировки и ограничения зависят от поведения аккаунта, истории, жалоб, текстов, источников и решений Telegram. Прокси только снижает часть инфраструктурного хаоса.",
      },
    ],
    related: [
      { to: "/help/proxies", label: "Инструкция по прокси" },
      { to: "/blog/telegram-account-warmup-guide", label: "Прогрев Telegram-аккаунтов" },
    ],
  },
  "telegram-comments-examples": {
    slug: "telegram-comments-examples",
    badge: "Примеры",
    title: "Примеры Telegram-комментариев: хороший ответ, слабый ответ и спам",
    description:
      "Разбор примеров Telegram-комментариев для нейрокомментинга: как отличить полезный ответ от рекламного спама, какие CTA использовать и что измерять.",
    intro:
      "Команда может настроить лимиты, прокси и UTM, но испортить результат одним типом текста: одинаковым рекламным комментарием под разными постами. Поэтому полезно заранее договориться, какие комментарии считаются хорошими, какие слабыми, а какие лучше не публиковать вообще.",
    category: "Нейрокомментинг",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "примеры Telegram комментариев",
      "примеры нейрокомментинга",
      "AI комментарии примеры",
      "комментарии Telegram без спама",
      "как писать комментарии в Telegram",
    ],
    sections: [
      {
        title: "Хороший комментарий связан с постом",
        paragraphs: [
          "Сильный комментарий отвечает на конкретную мысль поста или вопрос в обсуждении. Он может дать пример, уточнение, короткий опыт или аккуратную ссылку на материал, если ссылка действительно помогает.",
          "Хороший AI-профиль должен уметь отказаться от публикации, если контекста мало. Молчание иногда безопаснее и полезнее, чем слабая рекламная вставка.",
        ],
        bullets: [
          "есть связь с темой поста",
          "нет одинакового шаблонного CTA",
          "тон соответствует обсуждению",
          "ссылка появляется только когда она уместна",
        ],
      },
      {
        title: "Слабый комментарий звучит универсально",
        paragraphs: [
          "Слабый комментарий можно вставить почти куда угодно: он не спорит, не уточняет и не добавляет пользы. Такие ответы редко вызывают доверие, даже если формально не выглядят грубым спамом.",
          "Если AI часто пишет общие фразы вроде «интересная мысль, у нас есть решение», нужно усилить промпт: добавить роль, запреты, критерии уместности и примеры плохих ответов.",
        ],
      },
      {
        title: "Спам начинается с повторяемости",
        paragraphs: [
          "Один коммерческий комментарий может быть уместным, если он отвечает на явный запрос. Но десять похожих комментариев с одинаковой ссылкой в разных источниках быстро создают риск жалоб и ограничений.",
          "В отчете нужно смотреть не только клики, но и жалобы, удаления, ошибки Telegram, глубину просмотра и signup_submit. Так видно, где комментарии создают спрос, а где только шум.",
        ],
        bullets: [
          "одинаковый текст под разными постами",
          "ссылка в каждом ответе",
          "обещания без контекста",
          "публикация после негативной реакции",
        ],
      },
    ],
    faq: [
      {
        question: "Нужны ли примеры плохих комментариев в промпте?",
        answer:
          "Да. Примеры плохих ответов помогают модели избегать шаблонных CTA, лишних ссылок и универсальных рекламных фраз.",
      },
      {
        question: "Можно ли автоматизировать комментарии без модерации?",
        answer:
          "После тестов и настройки лимитов можно автоматизировать часть сценариев, но первые запуски лучше проверять вручную и смотреть реакцию источников.",
      },
    ],
    related: [
      { to: "/blog/ai-comments-telegram-guide", label: "AI-комментарии в Telegram" },
      { to: "/blog/neurocommenting-vs-spam", label: "Нейрокомментинг и спам" },
    ],
  },
  "telegram-comments-moderation": {
    slug: "telegram-comments-moderation",
    badge: "Модерация",
    title: "Модерация AI-комментариев в Telegram: что проверять перед публикацией",
    description:
      "Как выстроить модерацию AI-комментариев в Telegram: контекст, стоп-слова, CTA, ссылки, лимиты, ручная проверка и критерии отказа от публикации.",
    intro:
      "Даже хороший AI-профиль иногда пишет слишком общо, рекламно или не в тон обсуждению. Поэтому первые кампании лучше запускать через модерацию: не чтобы тормозить процесс, а чтобы быстро научить систему, какие ответы можно публиковать.",
    category: "Нейрокомментинг",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "модерация AI комментариев",
      "проверка комментариев Telegram",
      "AI комментарии без спама",
      "ручная модерация нейрокомментинга",
      "контроль автокомментариев Telegram",
    ],
    sections: [
      {
        title: "Проверяйте связь с конкретным постом",
        paragraphs: [
          "Первый фильтр модерации простой: комментарий должен отвечать на конкретную мысль поста или вопрос в обсуждении. Если текст можно вставить под десять разных постов без изменений, он слабый.",
          "Для demo-platform это особенно важно, потому что продукт продает не массовый шум, а управляемый нейрокомментинг с источниками, лимитами и аналитикой.",
        ],
        bullets: [
          "есть привязка к теме поста",
          "нет универсального рекламного шаблона",
          "комментарий не спорит с фактом из исходного текста",
          "тон соответствует площадке",
        ],
      },
      {
        title: "Ссылки и CTA должны быть редкими",
        paragraphs: [
          "Ссылка в каждом комментарии быстро превращает даже полезный ответ в рекламу. На старте лучше чередовать ответы без ссылки, мягкие переходы и комментарии, где CTA действительно уместен.",
          "Модератору полезно отмечать причину отказа: лишняя ссылка, слишком сильное обещание, слабый контекст, повтор формулировки или риск жалобы. Эти причины потом улучшают промпт.",
        ],
      },
      {
        title: "Модерация должна кормить аналитику",
        paragraphs: [
          "Ручная проверка полезна только если ее результаты попадают обратно в процесс: какие источники требуют строгой модерации, какие аккаунты дают ошибки, какие формулировки получают клики и какие комментарии лучше отклонять заранее.",
          "Так модерация становится не ручным узким местом, а обучающим слоем для кампаний, UTM-отчетов и SEO-контента.",
        ],
        bullets: [
          "сохраняйте причину отклонения",
          "смотрите клики и signup_submit по типам CTA",
          "отдельно анализируйте жалобы и удаления",
          "переносите удачные правила в AI-профиль",
        ],
      },
    ],
    faq: [
      {
        question: "Когда можно отключать ручную модерацию?",
        answer:
          "Только после серии малых тестов, где видны стабильное качество текстов, низкий риск жалоб, понятные лимиты и нормальная конверсия по UTM и целям.",
      },
      {
        question: "Что важнее модерировать: текст или источник?",
        answer:
          "Оба слоя. Даже хороший текст может быть неуместен в плохом источнике, а хороший источник можно испортить слабым универсальным комментарием.",
      },
    ],
    related: [
      { to: "/telegram-auto-comments", label: "Автокомментарии Telegram" },
      { to: "/blog/telegram-comments-examples", label: "Примеры Telegram-комментариев" },
    ],
  },
  "telegram-comment-bot-comparison": {
    slug: "telegram-comment-bot-comparison",
    badge: "Сравнение",
    title: "Бот для комментариев или платформа Telegram-маркетинга: что выбрать",
    description:
      "Сравнение простого Telegram comment bot и платформы Telegram-маркетинга: источники, AI-профили, лимиты, UTM, отчеты, модерация и безопасность.",
    intro:
      "Простой бот для комментариев кажется быстрым решением: дал текст, выбрал чат, получил публикации. Но для реального маркетинга обычно нужны источники, лимиты, роли аккаунтов, UTM, отчеты и безопасная остановка.",
    category: "Платформа",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "бот для комментариев Telegram",
      "telegram comment bot сравнение",
      "платформа Telegram маркетинга",
      "сервис автокомментариев Telegram",
      "нейрокомментинг платформа",
    ],
    sections: [
      {
        title: "Простой бот решает только публикацию",
        paragraphs: [
          "Бот может отправить комментарий, но часто не отвечает на вопросы, которые важны маркетологу: почему выбран этот источник, какой аккаунт пишет, где лимиты, что делать после FloodWait и как связать клик с регистрацией.",
          "Если задача разовая и маленькая, простого инструмента может хватить. Если команда хочет регулярный Telegram-маркетинг, нужен операционный контур.",
        ],
        bullets: [
          "бот отвечает за действие",
          "платформа отвечает за процесс",
          "боту часто не хватает отчетности",
          "платформе важны лимиты, роли и аналитика",
        ],
      },
      {
        title: "Платформа нужна, когда важен результат",
        paragraphs: [
          "В demo-platform комментарии связаны с источниками, AI-профилями, аккаунтами, UTM-ссылками, логами и отчетами. Это помогает видеть не только факт публикации, но и путь до signup, workspace и первого действия в продукте.",
          "Такой подход ближе к маркетинговой системе, чем к скрипту: можно сравнивать источники, промпты, аккаунты, CTA и посадочные страницы.",
        ],
      },
      {
        title: "Как выбирать инструмент",
        paragraphs: [
          "Если вам нужен короткий эксперимент, выбирайте самый простой безопасный сценарий с ручной проверкой. Если нужно вести кампании регулярно, смотрите на лимиты, модерацию, аналитику, историю ошибок и возможность работать командой.",
          "Главный критерий - не количество отправленных комментариев, а управляемая цепочка: источник, контекстный ответ, переход, регистрация и активация.",
        ],
        bullets: [
          "есть ли UTM и цели",
          "видны ли ошибки Telegram",
          "можно ли модерировать тексты",
          "есть ли отчеты по источникам и аккаунтам",
        ],
      },
    ],
    faq: [
      {
        question: "Чем demo-platform отличается от обычного comment bot?",
        answer:
          "demo-platform строит процесс вокруг источников, AI-профилей, лимитов, UTM, логов и отчетов. Обычный бот чаще закрывает только отправку комментария.",
      },
      {
        question: "Можно ли начать с малого теста?",
        answer:
          "Да. Лучше начать с малого списка источников, ручной модерации, UTM-меток и аккуратных лимитов, а потом масштабировать только работающие связки.",
      },
    ],
    related: [
      { to: "/telegram-comment-bot", label: "Бот для комментариев Telegram" },
      { to: "/telegram-marketing-platform", label: "Платформа Telegram-маркетинга" },
    ],
  },
  "telegram-ai-comments-prompts": {
    slug: "telegram-ai-comments-prompts",
    badge: "Промпты",
    title: "Промпты для AI-комментариев в Telegram: роль, тон, запреты и отказ",
    description:
      "Как составлять промпты для AI-комментариев в Telegram: роль аккаунта, контекст поста, tone of voice, стоп-слова, CTA, ссылки и правила отказа от публикации.",
    intro:
      "Хороший промпт для AI-комментариев не должен просто просить «напиши ответ». Он задает роль аккаунта, допустимый тон, границы обещаний, условия отказа и правила ссылок, чтобы комментарий выглядел как участие в обсуждении, а не как рекламная вставка.",
    category: "Промпты",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "промпты для AI комментариев",
      "промпт Telegram комментарии",
      "AI комментарии Telegram промпт",
      "нейрокомментинг промпт",
      "как писать AI комментарии",
    ],
    sections: [
      {
        title: "Начинайте с роли аккаунта",
        paragraphs: [
          "Модель должна понимать, кто говорит: основатель, маркетолог, технический специалист, пользователь продукта или нейтральный участник обсуждения. Без роли ответы часто получаются слишком общими и рекламными.",
          "Роль не должна притворяться реальным человеком с выдуманным опытом. Лучше описывать безопасную позицию: «отвечай как представитель сервиса», «не обещай результат», «не спорь агрессивно».",
        ],
        bullets: [
          "кто отвечает и от имени кого",
          "какой опыт можно упоминать",
          "какие обещания запрещены",
          "какой тон считается нормальным для источника",
        ],
      },
      {
        title: "Запреты важнее красивого CTA",
        paragraphs: [
          "Для Telegram-комментариев опасны не только плохие формулировки, но и слишком частые ссылки, одинаковые призывы, обещания обхода ограничений и попытка продать там, где человек просто обсуждает тему.",
          "В промпте должны быть стоп-условия: если контекста мало, тема нерелевантна, пост конфликтный или ответ требует неподтвержденных обещаний, лучше не публиковать комментарий.",
        ],
        bullets: [
          "не ставить ссылку в каждый ответ",
          "не использовать одинаковый шаблон",
          "не обещать обход ограничений Telegram",
          "не отвечать, если нет связи с постом",
        ],
      },
      {
        title: "Промпт должен быть связан с отчетом",
        paragraphs: [
          "Промпт нельзя оценивать отдельно от результата. Удачный текст - это не тот, который красиво звучит, а тот, который не вызывает жалоб, уместен в источнике и помогает пользователю перейти к следующему шагу.",
          "В demo-platform промпты стоит анализировать вместе с UTM, источником, аккаунтом, модерацией, ошибками Telegram и событиями signup. Тогда можно улучшать не только текст, но и всю кампанию.",
        ],
      },
    ],
    faq: [
      {
        question: "Нужен ли один универсальный промпт для всех чатов?",
        answer:
          "Нет. Лучше иметь базовый профиль и адаптировать правила под тип источника, тему, допустимость ссылок и уровень модерации.",
      },
      {
        question: "Что важнее: промпт или модерация?",
        answer:
          "На старте важны оба слоя. Модерация показывает, где промпт ошибается, а затем эти причины отказа нужно переносить обратно в AI-профиль.",
      },
    ],
    related: [
      { to: "/blog/telegram-comments-moderation", label: "Модерация AI-комментариев" },
      { to: "/blog/telegram-comments-examples", label: "Примеры Telegram-комментариев" },
    ],
  },
  "telegram-lead-generation-funnel": {
    slug: "telegram-lead-generation-funnel",
    badge: "Воронка",
    title: "Воронка лидогенерации из Telegram: комментарий, клик, signup и активация",
    description:
      "Как строить воронку лидогенерации из Telegram: источник, AI-комментарий, UTM-ссылка, посадочная страница, регистрация, workspace, первое действие и оплата.",
    intro:
      "Telegram-кампания не заканчивается на опубликованном комментарии. Для бизнеса важнее понять весь путь: где человек увидел ответ, почему кликнул, дошел ли до регистрации, создал ли workspace и сделал ли первое полезное действие.",
    category: "Лидогенерация",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "лидогенерация из Telegram",
      "воронка Telegram маркетинга",
      "Telegram signup аналитика",
      "нейрокомментинг лиды",
      "Telegram UTM регистрация",
    ],
    sections: [
      {
        title: "Первый шаг - не комментарий, а источник",
        paragraphs: [
          "Лидогенерация начинается с выбора источника: канал, чат или обсуждение должны совпадать с темой оффера и форматом общения. Если источник нерелевантен, даже хороший AI-комментарий даст слабые переходы.",
          "Каждый источник лучше размечать отдельным utm_content или короткой ссылкой. Тогда в отчете видно не только количество кликов, но и качество дальнейших действий.",
        ],
        bullets: [
          "источник совпадает с темой оффера",
          "комментарии или обсуждения открыты",
          "есть отдельная UTM-метка",
          "известен аккаунт и текст ответа",
        ],
      },
      {
        title: "Клик без регистрации - это еще не лид",
        paragraphs: [
          "Переход из Telegram полезен, но сам по себе не доказывает результат. Нужно смотреть посадочную страницу, глубину просмотра, signup_submit, ошибки формы и последующий вход в кабинет.",
          "Если клики есть, а регистраций нет, проблема может быть не в источнике, а в оффере, заголовке страницы, скорости, доверии или слишком резком CTA.",
        ],
        bullets: [
          "клик по UTM-ссылке",
          "просмотр релевантной посадочной",
          "signup_submit",
          "переход в кабинет",
        ],
      },
      {
        title: "Активация важнее красивого отчета",
        paragraphs: [
          "Для SaaS важнее не просто регистрация, а первое полезное действие: создан workspace, добавлен Telegram-аккаунт, подключен источник, создана кампания или открыт тариф.",
          "В demo-platform отчет по Telegram-кампании должен связывать комментарий, источник, UTM, регистрацию и активацию. Только так можно понять, какие темы и площадки стоит масштабировать.",
        ],
      },
    ],
    faq: [
      {
        question: "Какой этап воронки считать лидом?",
        answer:
          "Минимально лидом можно считать signup, но для SaaS сильнее считать активированного пользователя: workspace создан и сделано первое полезное действие.",
      },
      {
        question: "Что делать, если есть клики, но нет регистраций?",
        answer:
          "Проверить соответствие комментария посадочной странице, скорость загрузки, оффер, доверительные элементы, форму регистрации и качество источника по UTM.",
      },
    ],
    related: [
      { to: "/telegram-lead-generation", label: "Лиды из Telegram" },
      { to: "/blog/telegram-campaign-reporting", label: "Отчеты Telegram-кампаний" },
    ],
  },
  "telegram-campaign-utm-template": {
    slug: "telegram-campaign-utm-template",
    badge: "UTM-шаблон",
    title: "UTM-шаблон для Telegram-кампаний: источники, аккаунты и комментарии",
    description:
      "Готовая схема UTM-меток для Telegram-кампаний: как называть source, medium, campaign и content, чтобы видеть источники, аккаунты, тексты и регистрации.",
    intro:
      "Если UTM-метки назвать хаотично, отчет быстро развалится: один и тот же источник будет жить под разными именами, аккаунты смешаются, а регистрации нельзя будет связать с конкретным комментарием. Поэтому шаблон UTM лучше договорить до запуска.",
    category: "Аналитика",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "UTM шаблон Telegram",
      "UTM Telegram кампании",
      "utm_content Telegram аккаунт",
      "аналитика Telegram комментариев",
      "Telegram campaign tracking",
    ],
    sections: [
      {
        title: "Базовая схема UTM",
        paragraphs: [
          "Для Telegram-кампаний обычно хватает четырех параметров: source, medium, campaign и content. Их задача - не красиво выглядеть, а стабильно связывать источник, механику, запуск и конкретный вариант.",
          "Если команда использует demo-platform для AI-комментариев, utm_content лучше резервировать под связку аккаунт-источник-вариант, потому что именно там чаще всего находится ответ на вопрос «что сработало».",
        ],
        bullets: [
          "utm_source=telegram",
          "utm_medium=neurocommenting или broadcast",
          "utm_campaign=nc_launch_2026_06",
          "utm_content=acc01_src07_v1",
        ],
      },
      {
        title: "Не меняйте словарь в середине запуска",
        paragraphs: [
          "Главная ошибка - каждый раз придумывать новые названия. Если сегодня источник называется tg_chat_ai, завтра ai_chat, а послезавтра source7, отчет станет шумным и ручным.",
          "Словарь лучше хранить рядом с кампанией: код аккаунта, код источника, версия текста, посадочная страница и дата запуска. Тогда Метрика, продуктовая аналитика и XLSX-отчеты говорят на одном языке.",
        ],
      },
      {
        title: "Как читать результат",
        paragraphs: [
          "После запуска смотрите не только визиты, но и глубину, signup_submit, переход в кабинет, workspace и первое действие. UTM-метка должна доживать до этапа, где понятно, стал ли клик реальным лидом.",
          "Если один utm_content дает клики без регистраций, проверьте релевантность комментария и посадочной страницы. Если регистрация есть, но нет активации, проблема может быть уже в onboarding.",
        ],
        bullets: [
          "клики по каждому utm_content",
          "signup_submit и вход в кабинет",
          "создание workspace",
          "первое действие после регистрации",
        ],
      },
    ],
    faq: [
      {
        question: "Нужно ли делать отдельную ссылку под каждый комментарий?",
        answer:
          "Не всегда. Для малых тестов достаточно связки аккаунт-источник-вариант. Для важных гипотез можно делать отдельную короткую ссылку под конкретный комментарий.",
      },
      {
        question: "Что писать в utm_medium для AI-комментариев?",
        answer:
          "Лучше использовать стабильное значение вроде neurocommenting или ai_comments и не менять его между запусками без причины.",
      },
    ],
    related: [
      { to: "/blog/utm-telegram-campaigns", label: "UTM-метки для Telegram-кампаний" },
      { to: "/blog/telegram-lead-generation-funnel", label: "Воронка лидогенерации" },
    ],
  },
  "telegram-comment-quality-checklist": {
    slug: "telegram-comment-quality-checklist",
    badge: "Checklist",
    title: "Checklist качества AI-комментариев в Telegram перед масштабированием",
    description:
      "Чеклист для проверки AI-комментариев в Telegram: контекст поста, тон, CTA, ссылки, стоп-слова, жалобы, UTM, модерация и готовность к масштабированию.",
    intro:
      "Перед тем как увеличивать лимиты, нужно убедиться, что комментарии не просто публикуются, а действительно подходят источнику, не повторяются, не вызывают жалоб и ведут к измеримому следующему шагу.",
    category: "Checklist",
    readTime: "7 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "чеклист AI комментариев",
      "качество Telegram комментариев",
      "проверка нейрокомментинга",
      "AI комментарии без спама",
      "масштабирование Telegram кампаний",
    ],
    sections: [
      {
        title: "Проверка перед публикацией",
        paragraphs: [
          "Первый уровень качества - уместность. Комментарий должен быть связан с конкретным постом, звучать естественно для источника и не выглядеть как универсальный рекламный шаблон.",
          "Если ответ можно опубликовать под любым постом, его лучше отклонить или отправить на доработку промпта.",
        ],
        bullets: [
          "есть связь с исходным постом",
          "тон подходит площадке",
          "нет неподтвержденных обещаний",
          "ссылка нужна именно в этом ответе",
        ],
      },
      {
        title: "Проверка после публикации",
        paragraphs: [
          "После первых тестов смотрите не только клики, но и удаления, жалобы, ответы людей, ошибки Telegram, глубину просмотра и signup_submit. Комментарий может давать трафик, но портить репутацию источника.",
          "Если есть клики без регистрации, проверьте соответствие текста, посадочной страницы и ожиданий пользователя.",
        ],
        bullets: [
          "нет жалоб и массовых удалений",
          "есть переходы по UTM",
          "пользователь не уходит сразу",
          "есть signup или другое целевое действие",
        ],
      },
      {
        title: "Когда можно масштабировать",
        paragraphs: [
          "Масштабировать стоит только связки, где совпали источник, промпт, аккаунт, UTM и посадочная страница. Увеличение объема без качества чаще усиливает ошибки, а не результат.",
          "В demo-platform checklist полезно использовать как stop-loss: если падает качество, растут жалобы или исчезает активация после регистрации, кампанию лучше замедлить и пересобрать правила.",
        ],
      },
    ],
    faq: [
      {
        question: "Сколько комментариев нужно проверить перед масштабированием?",
        answer:
          "Начните с малого теста по нескольким релевантным источникам. Важнее увидеть стабильное качество, отсутствие жалоб и первые целевые действия, чем быстро набрать объем.",
      },
      {
        question: "Что делать с плохими комментариями?",
        answer:
          "Не публиковать повторно, сохранить причину отказа и перенести ее в промпт, правила модерации или список стоп-условий кампании.",
      },
    ],
    related: [
      { to: "/blog/telegram-comments-moderation", label: "Модерация AI-комментариев" },
      { to: "/blog/telegram-ai-comments-prompts", label: "Промпты для AI-комментариев" },
    ],
  },
  "telegram-free-seo-channels": {
    slug: "telegram-free-seo-channels",
    badge: "Free SEO",
    title: "Бесплатные SEO-каналы для Telegram-маркетинг SaaS: профили, каталоги и посты",
    description:
      "Как продвигать Telegram-маркетинг SaaS бесплатными SEO-методами: Яндекс Бизнес, Bing Webmaster, профили, каталоги, founder mentions, статьи и UTM-контроль.",
    intro:
      "Бесплатное SEO для SaaS не сводится к массовой регистрации в каталогах. Сильнее работает аккуратная система: технически чистый сайт, полезные статьи, понятная about-страница, несколько качественных профилей и измерение referral-трафика.",
    category: "Free SEO",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "бесплатное SEO SaaS",
      "продвижение Telegram сервиса",
      "SaaS каталоги бесплатно",
      "Яндекс Бизнес SaaS",
      "referral traffic audit",
    ],
    sections: [
      {
        title: "Начинайте с entity-страниц",
        paragraphs: [
          "Перед внешними размещениями у сайта должны быть понятные страницы, на которые не стыдно ссылаться: главная, about, платформа, тарифы, статьи и карта сайта. Это помогает поисковикам связать бренд, продукт и темы.",
          "Для demo-platform базовая связка уже выглядит так: /about для описания бренда, /platform для продукта, /blog для экспертных материалов и тематические посадочные под нейрокомментинг, автокомментарии и лидогенерацию.",
        ],
        bullets: [
          "about-страница для профилей",
          "product-страница для каталогов",
          "статьи для экспертных ссылок",
          "sitemap и RSS для обхода",
        ],
      },
      {
        title: "Выбирайте качество, а не тысячу каталогов",
        paragraphs: [
          "Массовые каталоги часто дают слабый или вредный сигнал: одинаковые описания, мусорные страницы, noindex, подозрительное соседство и нулевая аудитория. Лучше выбрать меньше площадок, но заполнить их нормально.",
          "Для бесплатного SEO полезнее профили с реальным описанием: Яндекс Бизнес, Bing Webmaster, GitHub/README, Telegram-пост, SaaS-каталог, профиль основателя или статья с практической пользой.",
        ],
      },
      {
        title: "Измеряйте referral-трафик",
        paragraphs: [
          "Каждая внешняя ссылка должна вести на подходящую страницу и быть понятной в аналитике. Для профиля можно использовать чистый canonical URL, а для постов и анонсов - UTM с понятным source и medium.",
          "Через неделю после размещений стоит смотреть не только визиты, но и signup, workspace, первое действие и качество сессий. Так бесплатные размещения превращаются в управляемый канал, а не в список ссылок ради списка.",
        ],
        bullets: [
          "профили ведут на /about или /platform",
          "посты ведут на релевантную статью",
          "UTM используется для анонсов",
          "отчет смотрит signup и активацию",
        ],
      },
    ],
    faq: [
      {
        question: "Стоит ли покупать массовую регистрацию в каталогах?",
        answer:
          "Нет. Для молодого SaaS безопаснее качественные профили, полезные статьи и измеримые referral-размещения, чем тысячи однотипных ссылок.",
      },
      {
        question: "Куда лучше ссылаться из внешнего профиля?",
        answer:
          "Для описания компании используйте /about, для продукта - /platform, для конкретной темы - релевантную статью или посадочную страницу.",
      },
    ],
    related: [
      { to: "/about", label: "О demo-platform" },
      { to: "/blog/telegram-campaign-utm-template", label: "UTM-шаблон кампаний" },
    ],
  },
  "telegram-referral-traffic-audit": {
    slug: "telegram-referral-traffic-audit",
    badge: "Analytics",
    title: "Аудит referral-трафика из каталогов, профилей и Telegram-посевов",
    description:
      "Как проверять referral-трафик в Яндекс.Метрике: источники, UTM, качество сессий, signup, активация, мусорные каталоги и полезные внешние размещения.",
    intro:
      "Переход из внешнего сайта сам по себе еще не значит, что размещение полезное. Важно понять, кто пришел, с какой страницы, что человек сделал после клика и превратился ли визит в регистрацию, workspace или другой продуктовый сигнал.",
    category: "Analytics",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "аудит referral трафика",
      "реферальный трафик Яндекс Метрика",
      "UTM аудит Telegram",
      "качество трафика из каталогов",
      "SaaS SEO аналитика",
    ],
    sections: [
      {
        title: "Сначала отделите источник от канала",
        paragraphs: [
          "В Метрике referral может выглядеть как один переход с неизвестного сайта, но за ним бывают разные сценарии: каталог, профиль сервиса, пост в подборке, скопированная ссылка или технический переход из браузера.",
          "Поэтому первое действие - не радоваться визиту и не удалять источник из отчета, а открыть страницу-реферер, проверить контекст ссылки и сопоставить его с посадочной страницей.",
        ],
        bullets: [
          "откуда пришел переход",
          "на какую страницу он попал",
          "есть ли UTM или только referer",
          "похож ли источник на реальную площадку",
        ],
      },
      {
        title: "Оценивайте качество, а не только количество",
        paragraphs: [
          "Пятьдесят визитов из каталога могут быть бесполезны, если люди уходят через несколько секунд. Один визит из сильной статьи может быть ценнее, если пользователь читает, переходит в тарифы и регистрируется.",
          "Для demo-platform referral-аудит должен смотреть глубину, время, достижение signup_submit, переход в кабинет и последующую активацию: добавлен ли Telegram-аккаунт, создан ли источник или кампания.",
        ],
        bullets: [
          "время на сайте и глубина просмотра",
          "переходы на /pricing, /signup и /platform",
          "signup_submit или регистрация",
          "первое продуктовое действие после регистрации",
        ],
      },
      {
        title: "Используйте UTM там, где можно управлять ссылкой",
        paragraphs: [
          "Для профиля компании можно оставить чистый canonical URL, чтобы не плодить мусорные варианты главной страницы. Но для постов, анонсов, founder mentions и подборок лучше ставить UTM.",
          "Хорошая UTM-структура помогает быстро понять, что сработало: профиль, пост, каталог, Telegram-посев или конкретный комментарий. Без этого все успешные и слабые размещения смешиваются в одном referral-отчете.",
        ],
        bullets: [
          "utm_source - площадка или каталог",
          "utm_medium - profile, post, directory, referral",
          "utm_campaign - seo_free или название теста",
          "utm_content - конкретный пост, блок или описание",
        ],
      },
      {
        title: "Решайте, что делать с источником",
        paragraphs: [
          "После первой недели источник нужно отнести к одной из групп: оставить и усилить, переписать описание, заменить посадочную страницу, добавить UTM, временно наблюдать или игнорировать как шум.",
          "Если источник дает живых пользователей, стоит поставить ссылку на более точную страницу: например, на статью про UTM, безопасность Telegram-маркетинга или страницу нейрокомментинга, а не всегда на главную.",
        ],
      },
    ],
    faq: [
      {
        question: "Что делать, если в Метрике появился странный referral-сайт?",
        answer:
          "Проверьте страницу-источник, контекст ссылки, поведение пользователей и цели. Если есть чтение, переходы и регистрация, источник можно развивать. Если только короткие визиты без действий, считайте его шумом.",
      },
      {
        question: "Нужно ли ставить UTM на все внешние ссылки?",
        answer:
          "Не обязательно. Для постоянных профилей часто достаточно чистого canonical URL, а для постов, подборок и управляемых размещений лучше использовать UTM.",
      },
    ],
    related: [
      { to: "/blog/yandex-metrika-telegram-traffic", label: "Telegram-трафик в Метрике" },
      { to: "/blog/telegram-campaign-utm-template", label: "UTM-шаблон кампаний" },
    ],
  },
  "telegram-profile-description-examples": {
    slug: "telegram-profile-description-examples",
    badge: "Profiles",
    title: "Примеры описаний demo-platform для профилей, каталогов и внешних размещений",
    description:
      "Готовые шаблоны описаний Telegram-маркетинг платформы для Яндекс Бизнес, SaaS-каталогов, Telegram-постов, founder mentions и SEO-профилей.",
    intro:
      "Внешние профили помогают SEO только тогда, когда описание выглядит живым, уникальным и точно объясняет продукт. Один и тот же текст на десятках площадок хуже, чем несколько аккуратно адаптированных описаний под разные контексты.",
    category: "Profiles",
    readTime: "9 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "описание SaaS для каталога",
      "описание Telegram сервиса",
      "профиль компании SEO",
      "Яндекс Бизнес описание сервиса",
      "demo-platform описание",
    ],
    sections: [
      {
        title: "Короткое описание для профиля",
        paragraphs: [
          "demo-platform - платформа для безопасных Telegram-маркетинг операций: AI-комментарии, прогрев аккаунтов, источники, прокси, UTM-метки, логи кампаний и аналитика результата.",
          "Такой вариант подходит для карточек, где есть ограничение по длине и важна ясность с первых слов.",
        ],
        bullets: [
          "что делает сервис",
          "для какой площадки он нужен",
          "какие модули есть внутри",
          "почему это не просто бот для комментариев",
        ],
      },
      {
        title: "Описание для SaaS-каталога",
        paragraphs: [
          "demo-platform помогает маркетологам и агентствам запускать Telegram-кампании с контролем качества: подбирать источники, прогревать аккаунты, публиковать контекстные AI-комментарии, вести UTM-разметку и смотреть, какие размещения приводят к регистрации и активации.",
          "В отличие от простых автокомментаторов, платформа делает упор на безопасность, лимиты Telegram, логи действий, модерацию и измеримость каждого канала.",
        ],
      },
      {
        title: "Описание для экспертного поста",
        paragraphs: [
          "Мы строим demo-platform как операционную систему для Telegram-маркетинга: не спам-машину, а рабочий кабинет, где источник, аккаунт, комментарий, ссылка, UTM и результат связаны в одну цепочку.",
          "Главный фокус - сделать нейрокомментинг управляемым: меньше одинаковых рекламных ответов, больше контекста, прозрачные лимиты, понятные отчеты и возможность остановить кампанию, если падает качество.",
        ],
      },
      {
        title: "Как не испортить внешнее SEO",
        paragraphs: [
          "Не копируйте один текст во все каталоги. Меняйте акценты: для Яндекс Бизнес - что делает компания, для SaaS-каталога - продуктовые модули, для экспертного поста - подход и практический опыт.",
          "Ссылку тоже лучше выбирать по контексту: профиль компании ведет на /about, продуктовая карточка - на /platform, а обсуждение аналитики или безопасности - на соответствующую статью.",
        ],
        bullets: [
          "не использовать массовые одинаковые описания",
          "не обещать гарантированные лиды и обход лимитов",
          "не ставить ссылку только на главную в каждом месте",
          "проверять referral-трафик после размещения",
        ],
      },
    ],
    faq: [
      {
        question: "Можно ли использовать эти описания без изменений?",
        answer:
          "Лучше адаптировать под площадку: оставить смысл, но поменять порядок акцентов, длину, первую фразу и ссылку.",
      },
      {
        question: "Какая ссылка лучше для внешнего профиля?",
        answer:
          "Для профиля компании используйте /about, для продуктовой карточки - /platform, для тематического поста - релевантную статью или посадочную страницу.",
      },
    ],
    related: [
      { to: "/about", label: "О demo-platform" },
      { to: "/blog/telegram-free-seo-channels", label: "Бесплатные SEO-каналы" },
    ],
  },
  "telegram-saas-activation-metrics": {
    slug: "telegram-saas-activation-metrics",
    badge: "SaaS Metrics",
    title: "SaaS activation metrics после регистрации из Telegram и referral-трафика",
    description:
      "Какие activation metrics смотреть после signup из Telegram, каталогов и профилей: workspace, аккаунт, источник, кампания, UTM, первое действие и оплата.",
    intro:
      "Для SaaS мало знать, что человек пришел на сайт и зарегистрировался. Настоящее качество SEO, Telegram и referral-трафика видно после signup: создал ли пользователь рабочее пространство, подключил ли Telegram-аккаунт, выбрал ли источник и дошел ли до первой кампании.",
    category: "SaaS Metrics",
    readTime: "8 минут",
    publishedAt: "2026-06-30",
    keywords: [
      "SaaS activation metrics",
      "активация SaaS после регистрации",
      "Telegram traffic activation",
      "метрики активации SaaS",
      "referral traffic signup quality",
    ],
    sections: [
      {
        title: "Разделите signup и активацию",
        paragraphs: [
          "Регистрация показывает интерес, но не доказывает, что канал привел подходящего пользователя. Человек мог открыть кабинет из любопытства, не понять продукт или уйти до первого действия.",
          "Для demo-platform активация начинается тогда, когда пользователь делает шаг к реальной Telegram-кампании: создает workspace, добавляет аккаунт, настраивает источник, смотрит лимиты или собирает первую связку.",
        ],
        bullets: [
          "signup - пользователь создал доступ",
          "workspace_created - появился рабочий контекст",
          "telegram_account_added - пользователь готовит инфраструктуру",
          "source_or_campaign_created - есть намерение запускать маркетинг",
        ],
      },
      {
        title: "Смотрите путь от UTM до первого действия",
        paragraphs: [
          "Если UTM заканчивается на странице регистрации, аналитика обрывается слишком рано. Нужно связывать source, medium, campaign и content с событиями внутри продукта.",
          "Так можно увидеть, что, например, каталог дает много signup без действий, а статья про безопасность Telegram-маркетинга дает меньше регистраций, но больше добавленных аккаунтов и созданных источников.",
        ],
        bullets: [
          "utm_source и utm_campaign сохраняются после signup",
          "первое действие привязано к пользователю или workspace",
          "отчет сравнивает каналы по activation rate",
          "плохой канал отключается не по ощущениям, а по данным",
        ],
      },
      {
        title: "Минимальный набор activation metrics",
        paragraphs: [
          "Не нужно сразу строить сложную BI-систему. Достаточно нескольких событий, которые показывают, что пользователь понял продукт и начал собирать рабочую кампанию.",
          "Для Telegram-маркетинг SaaS полезно смотреть регистрацию, создание workspace, добавление Telegram-аккаунта, подключение прокси, выбор источника, запуск или черновик кампании и переход к оплате.",
        ],
        bullets: [
          "signup_submit",
          "dashboard_open",
          "workspace_created",
          "telegram_account_added",
          "source_created",
          "campaign_created",
          "checkout_opened",
        ],
      },
      {
        title: "Как использовать метрики в SEO",
        paragraphs: [
          "SEO-страница может получать мало трафика, но приводить сильных пользователей. Поэтому статьи и внешние размещения нужно сравнивать не только по просмотрам, а по активации после регистрации.",
          "Если страница стабильно приводит людей, которые добавляют аккаунты и источники, ее стоит усиливать внутренними ссылками, новыми примерами, внешними упоминаниями и похожими материалами.",
        ],
      },
    ],
    faq: [
      {
        question: "Какая метрика важнее: signup или activation rate?",
        answer:
          "Для раннего SEO важны обе. Signup показывает спрос, а activation rate показывает качество канала и соответствие ожиданий продукту.",
      },
      {
        question: "Через сколько дней оценивать новый источник?",
        answer:
          "Первые сигналы можно смотреть через 3-7 дней, но устойчивые выводы лучше делать после накопления регистраций и продуктовых действий, а не по одному визиту.",
      },
    ],
    related: [
      { to: "/blog/telegram-referral-traffic-audit", label: "Аудит referral-трафика" },
      { to: "/blog/telegram-lead-generation-funnel", label: "Воронка лидогенерации" },
    ],
  },
  "yandex-metrika-telegram-utm": {
    slug: "yandex-metrika-telegram-utm",
    badge: "Metrika",
    title: "Как читать Telegram UTM-трафик в Яндекс.Метрике без самообмана",
    description:
      "Практический чеклист по Яндекс.Метрике для Telegram-кампаний: UTM source/medium/campaign/content, direct, internal traffic, dashboard-сессии и цели регистрации.",
    intro:
      "Telegram-трафик легко переоценить: часть переходов приходит как direct, часть оказывается внутренними заходами команды, а часть дает просмотры без регистрации. Поэтому UTM-отчет нужно читать вместе с целями, страницами входа и продуктовой активацией.",
    category: "Metrika",
    readTime: "8 минут",
    publishedAt: "2026-07-06",
    keywords: [
      "Яндекс Метрика Telegram UTM",
      "Telegram UTM отчет",
      "как читать UTM Метрика",
      "direct traffic Telegram",
      "Метрика цели регистрации",
    ],
    sections: [
      {
        title: "Начните с UTM-меток",
        paragraphs: [
          "В отчете Метрики откройте источники по UTM и сначала смотрите не общую строку кампании, а связку source, medium, campaign и content. Именно content чаще всего отвечает на вопрос, какой аккаунт, источник или текст дал переход.",
          "Для demo-platform базовая структура выглядит так: utm_source=telegram, utm_medium=neurocommenting, utm_campaign=nc_launch_2026_05, utm_content=account_01 или другая конкретная связка.",
        ],
        bullets: [
          "utm_source показывает площадку или канал",
          "utm_medium отделяет нейрокомментинг, пост, профиль или referral",
          "utm_campaign группирует запуск",
          "utm_content сравнивает аккаунты, источники и варианты текста",
        ],
      },
      {
        title: "Проверьте страницы входа",
        paragraphs: [
          "Если Telegram-переходы ведут только на главную, часть людей может не понять конкретный оффер. Смотрите, с каких страниц начиналась сессия: главная, pricing, signup, статья или посадочная под нейрокомментинг.",
          "Для холодного трафика часто лучше вести не всегда на главную, а на страницу, которая совпадает с контекстом комментария: безопасность, UTM, примеры комментариев, платформа или конкретный use case.",
        ],
      },
      {
        title: "Отделите direct и internal",
        paragraphs: [
          "Direct не всегда означает, что человек набрал адрес руками. Telegram, браузеры и повторные заходы могут скрывать referrer. Internal traffic часто появляется от ваших же проверок, кабинета, dashboard-сессий и переходов команды.",
          "Перед выводами по рекламе исключите свои проверки, смотрите новые пользователи, глубину, время, signup_submit, signup_success и дальнейшие продуктовые действия.",
        ],
        bullets: [
          "не считать dashboard-сессии лидогенерацией",
          "не смешивать проверки команды с холодным трафиком",
          "сравнивать UTM не по визитам, а по действиям",
          "после регистрации смотреть workspace и первый шаг",
        ],
      },
      {
        title: "Когда канал можно усиливать",
        paragraphs: [
          "Канал стоит усиливать, если он дает не просто визиты, а понятные следующие действия: регистрация, вход в кабинет, создание workspace, добавление Telegram-аккаунта, источника или кампании.",
          "Если есть клики без регистрации, проверьте текст комментария, ожидания пользователя и посадочную страницу. Если есть регистрация без активации, проблема может быть уже в onboarding или первом экране продукта.",
        ],
      },
    ],
    faq: [
      {
        question: "Почему Telegram-переходы иногда не видны как Telegram?",
        answer:
          "Мессенджер, браузер или промежуточное открытие ссылки могут не передать referrer. Поэтому для управляемых размещений лучше использовать UTM и короткие ссылки.",
      },
      {
        question: "Какая цель важнее для Telegram-кампаний?",
        answer:
          "Для ранней проверки смотрите signup_submit/signup_success, но окончательно оценивайте канал по активации: workspace, аккаунт, источник, кампания и checkout.",
      },
    ],
    related: [
      { to: "/blog/yandex-metrika-telegram-traffic", label: "Telegram-трафик в Метрике" },
      { to: "/blog/telegram-campaign-utm-template", label: "UTM-шаблон кампаний" },
    ],
  },
};

const legalLinkClass =
  "font-medium text-[#006d6d] underline underline-offset-4";

export function PageMeta({
  title,
  description,
  canonicalPath,
  noIndex = false,
  structuredData,
}: {
  title: string;
  description: string;
  canonicalPath?: string;
  noIndex?: boolean;
  structuredData?: Record<string, unknown>;
}) {
  React.useEffect(() => {
    const canonicalUrl = `${siteUrl}${canonicalPath || window.location.pathname}`;
    document.title = title;

    const setMeta = (
      selector: string,
      attrs: Record<string, string>,
      content: string,
    ) => {
      let tag = document.querySelector<HTMLMetaElement>(selector);
      if (!tag) {
        tag = document.createElement("meta");
        Object.entries(attrs).forEach(([key, value]) =>
          tag?.setAttribute(key, value),
        );
        document.head.appendChild(tag);
      }
      tag.content = content;
    };

    setMeta('meta[name="description"]', { name: "description" }, description);
    setMeta(
      'meta[name="robots"]',
      { name: "robots" },
      noIndex ? "noindex, nofollow" : "index, follow",
    );
    setMeta('meta[property="og:type"]', { property: "og:type" }, "website");
    setMeta(
      'meta[property="og:site_name"]',
      { property: "og:site_name" },
      "demo-platform",
    );
    setMeta('meta[property="og:title"]', { property: "og:title" }, title);
    setMeta(
      'meta[property="og:description"]',
      { property: "og:description" },
      description,
    );
    setMeta('meta[property="og:url"]', { property: "og:url" }, canonicalUrl);
    setMeta(
      'meta[property="og:image"]',
      { property: "og:image" },
      defaultOgImage,
    );
    setMeta(
      'meta[name="twitter:card"]',
      { name: "twitter:card" },
      "summary_large_image",
    );
    setMeta('meta[name="twitter:title"]', { name: "twitter:title" }, title);
    setMeta(
      'meta[name="twitter:description"]',
      { name: "twitter:description" },
      description,
    );
    setMeta(
      'meta[name="twitter:image"]',
      { name: "twitter:image" },
      defaultOgImage,
    );

    let canonicalTag = document.querySelector<HTMLLinkElement>(
      'link[rel="canonical"]',
    );
    if (!canonicalTag) {
      canonicalTag = document.createElement("link");
      canonicalTag.rel = "canonical";
      document.head.appendChild(canonicalTag);
    }
    canonicalTag.href = canonicalUrl;

    const scriptId = "structured-data";
    document.getElementById(scriptId)?.remove();
    if (structuredData) {
      const script = document.createElement("script");
      script.id = scriptId;
      script.type = "application/ld+json";
      script.text = JSON.stringify(structuredData);
      document.head.appendChild(script);
    }
  }, [canonicalPath, description, noIndex, structuredData, title]);

  return null;
}

function softwareSchema(path = "/") {
  return {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    name: "demo-platform",
    applicationCategory: "BusinessApplication",
    operatingSystem: "Web",
    url: `${siteUrl}${path}`,
    description:
      "SaaS-платформа для Telegram-аккаунтов, AI-комментариев, прогрева, источников, прокси, кампаний и отчетов.",
    offers: {
      "@type": "AggregateOffer",
      priceCurrency: "RUB",
      lowPrice: "0",
      highPrice: "19900",
      offerCount: "4",
      url: `${siteUrl}/pricing`,
    },
    publisher: {
      "@type": "Organization",
      name: "demo-platform",
      url: siteUrl,
      logo: `${siteUrl}/favicon.svg`,
    },
  };
}

function organizationSchema() {
  return {
    "@type": "Organization",
    name: "demo-platform",
    url: siteUrl,
    logo: `${siteUrl}/favicon.svg`,
    contactPoint: {
      "@type": "ContactPoint",
      contactType: "customer support",
      url: `${siteUrl}/contact`,
      availableLanguage: ["ru"],
    },
  };
}

function websiteSchema() {
  return {
    "@type": "WebSite",
    name: "demo-platform",
    url: siteUrl,
    inLanguage: "ru-RU",
    publisher: organizationSchema(),
  };
}

function breadcrumbSchema(items: { name: string; path: string }[]) {
  return {
    "@type": "BreadcrumbList",
    itemListElement: items.map((item, index) => ({
      "@type": "ListItem",
      position: index + 1,
      name: item.name,
      item: `${siteUrl}${item.path}`,
    })),
  };
}

function homePageSchema() {
  return {
    "@context": "https://schema.org",
    "@graph": [organizationSchema(), websiteSchema(), softwareSchema("/")],
  };
}

function aboutPageSchema() {
  return {
    "@context": "https://schema.org",
    "@graph": [
      organizationSchema(),
      websiteSchema(),
      softwareSchema("/about"),
      {
        "@type": "AboutPage",
        name: "О demo-platform",
        url: `${siteUrl}/about`,
        description:
          "Информация о demo-platform: SaaS-платформа для AI-комментариев, Telegram-маркетинга, прогрева аккаунтов, источников, UTM-ссылок, логов и отчетов.",
        inLanguage: "ru-RU",
        mainEntity: {
          "@type": "Organization",
          name: "demo-platform",
          url: siteUrl,
          logo: `${siteUrl}/favicon.svg`,
        },
      },
      breadcrumbSchema([
        { name: "Главная", path: "/" },
        { name: "О сервисе", path: "/about" },
      ]),
    ],
  };
}

function faqSchema() {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faqs.map((faq) => ({
      "@type": "Question",
      name: faq.question,
      acceptedAnswer: {
        "@type": "Answer",
        text: faq.answer,
      },
    })),
  };
}

function seoLandingSchema(page: SeoLandingPageData) {
  return {
    "@context": "https://schema.org",
    "@graph": [
      softwareSchema(`/${page.slug}`),
      {
        "@type": "WebPage",
        name: page.title,
        url: `${siteUrl}/${page.slug}`,
        description: page.description,
        inLanguage: "ru-RU",
        about: page.keywords.map((keyword) => ({
          "@type": "Thing",
          name: keyword,
        })),
      },
      breadcrumbSchema([
        { name: "Главная", path: "/" },
        { name: page.title, path: `/${page.slug}` },
      ]),
      {
        "@type": "FAQPage",
        mainEntity: page.faq.map((item) => ({
          "@type": "Question",
          name: item.question,
          acceptedAnswer: {
            "@type": "Answer",
            text: item.answer,
          },
        })),
      },
    ],
  };
}

function blogIndexSchema() {
  return {
    "@context": "https://schema.org",
    "@graph": [
      organizationSchema(),
      websiteSchema(),
      {
        "@type": "CollectionPage",
        name: "Статьи demo-platform по Telegram-маркетингу",
        url: `${siteUrl}/blog`,
        inLanguage: "ru-RU",
        description:
          "Практические статьи demo-platform про Telegram-маркетинг, AI-комментарии, UTM-метки, FloodWait, прогрев аккаунтов и безопасность.",
      },
      {
        "@type": "ItemList",
        name: "Статьи demo-platform по Telegram-маркетингу",
        url: `${siteUrl}/blog`,
        inLanguage: "ru-RU",
        itemListElement: Object.values(blogArticles).map((article, index) => ({
          "@type": "ListItem",
          position: index + 1,
          url: `${siteUrl}/blog/${article.slug}`,
          name: article.title,
        })),
      },
      breadcrumbSchema([
        { name: "Главная", path: "/" },
        { name: "Статьи", path: "/blog" },
      ]),
    ],
  };
}

function blogArticleSchema(article: BlogArticleData) {
  return {
    "@context": "https://schema.org",
    "@graph": [
      softwareSchema(`/blog/${article.slug}`),
      {
        "@type": "Article",
        headline: article.title,
        description: article.description,
        datePublished: article.publishedAt,
        dateModified: article.publishedAt,
        inLanguage: "ru-RU",
        url: `${siteUrl}/blog/${article.slug}`,
        mainEntityOfPage: `${siteUrl}/blog/${article.slug}`,
        author: {
          "@type": "Organization",
          name: "demo-platform",
          url: siteUrl,
        },
        publisher: {
          "@type": "Organization",
          name: "demo-platform",
          url: siteUrl,
          logo: {
            "@type": "ImageObject",
            url: `${siteUrl}/favicon.svg`,
          },
        },
        about: article.keywords.map((keyword) => ({
          "@type": "Thing",
          name: keyword,
        })),
      },
      {
        "@type": "FAQPage",
        mainEntity: article.faq.map((item) => ({
          "@type": "Question",
          name: item.question,
          acceptedAnswer: {
            "@type": "Answer",
            text: item.answer,
          },
        })),
      },
      breadcrumbSchema([
        { name: "Главная", path: "/" },
        { name: "Статьи", path: "/blog" },
        { name: article.title, path: `/blog/${article.slug}` },
      ]),
    ],
  };
}

function sitemapPageSections() {
  return [
    {
      title: "Продукт",
      links: [
        { to: "/", label: "Главная demo-platform" },
        { to: "/about", label: "О сервисе demo-platform" },
        { to: "/platform", label: "Платформа" },
        { to: "/use-cases", label: "Сценарии" },
        { to: "/pricing", label: "Тарифы" },
        { to: "/faq", label: "FAQ" },
        { to: "/contact", label: "Контакты и поддержка" },
      ],
    },
    {
      title: "SEO-посадочные",
      links: Object.values(seoLandingPages).map((page) => ({
        to: `/${page.slug}`,
        label: page.title,
      })),
    },
    {
      title: "Статьи",
      links: [
        { to: "/blog", label: "Все статьи" },
        ...Object.values(blogArticles).map((article) => ({
          to: `/blog/${article.slug}`,
          label: article.title,
        })),
      ],
    },
    {
      title: "Инструкции",
      links: [
        { to: "/help/", label: "База знаний" },
        { to: "/help/getting-started", label: "Первый безопасный запуск" },
        { to: "/help/accounts", label: "Telegram-аккаунты" },
        { to: "/help/proxies", label: "Прокси" },
        { to: "/help/sources", label: "Источники" },
        { to: "/help/warmup", label: "Прогрев" },
        { to: "/help/ai-comments", label: "AI-комментарии" },
        { to: "/help/safety", label: "Безопасность" },
        { to: "/help/billing", label: "Тарифы и доступы" },
      ],
    },
    {
      title: "Документы",
      links: [
        { to: "/legal/privacy", label: "Политика конфиденциальности" },
        { to: "/legal/offer", label: "Оферта" },
        { to: "/legal/terms", label: "Пользовательское соглашение" },
        { to: "/legal/payment", label: "Оплата и возвраты" },
      ],
    },
  ];
}

function sitemapPageSchema() {
  const sections = sitemapPageSections();
  const links = sections.flatMap((section) => section.links);

  return {
    "@context": "https://schema.org",
    "@graph": [
      organizationSchema(),
      websiteSchema(),
      {
        "@type": "CollectionPage",
        name: "Карта сайта demo-platform",
        url: `${siteUrl}/sitemap`,
        description:
          "HTML-карта публичных страниц demo-platform: продукт, SEO-посадочные, статьи, инструкции и документы.",
        inLanguage: "ru-RU",
      },
      {
        "@type": "ItemList",
        name: "Публичные страницы demo-platform",
        url: `${siteUrl}/sitemap`,
        itemListElement: links.map((link, index) => ({
          "@type": "ListItem",
          position: index + 1,
          url: `${siteUrl}${link.to}`,
          name: link.label,
        })),
      },
      breadcrumbSchema([
        { name: "Главная", path: "/" },
        { name: "Карта сайта", path: "/sitemap" },
      ]),
    ],
  };
}

function useMarketingRouteScroll() {
  const location = useLocation();

  React.useEffect(() => {
    if (typeof window === "undefined") return;

    window.setTimeout(() => {
      if (location.hash) {
        const target = document.getElementById(location.hash.slice(1));
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "start" });
          return;
        }
      }
      window.scrollTo({ top: 0, left: 0, behavior: "smooth" });
    }, 0);
  }, [location.hash, location.pathname]);
}

export function MarketingLayout({ children }: { children: React.ReactNode }) {
  useMarketingRouteScroll();
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const { session } = useMarketingSession();
  const { access } = useMarketingAccess(session);
  const { items } = useMarketingCart(session, access);
  const cartCount = cartProducts(items).length;
  const profileLabel = session?.user.email || session?.user.full_name || null;

  return (
    <div className="marketing-shell min-h-svh bg-[#f6f4ed] text-[#171915]">
      <header className="sticky top-0 z-50 border-b border-[#171915]/10 bg-[#f6f4ed]/92 backdrop-blur-xl">
        <div className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
          <Link
            to="/"
            className="flex items-center gap-2 font-semibold tracking-normal"
            onClick={() => setMobileOpen(false)}
          >
            <span className="flex size-8 items-center justify-center rounded-md bg-[#171915] text-[#d7ff46]">
              <MessageSquareTextIcon className="size-4" />
            </span>
            <BrandWord aiClassName="text-[#008f8b]" />
          </Link>
          <nav className="hidden items-center gap-1 md:flex">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-3 py-2 text-sm text-[#171915]/68 transition hover:bg-[#171915]/6 hover:text-[#171915]",
                    isActive &&
                      "bg-[#171915] text-[#f6f4ed] hover:bg-[#171915] hover:text-[#f6f4ed]",
                  )
                }
                end={item.to === "/"}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-2">
            <Button
              asChild
              variant="outline"
              className="relative border-[#171915]/14 bg-transparent text-[#171915] hover:bg-[#171915]/8"
            >
              <Link to={cartPageUrl} aria-label={`Корзина: ${cartCount}`}>
                <ShoppingCartIcon />
                <span className="hidden sm:inline">Корзина</span>
                {cartCount > 0 && (
                  <span className="ml-1 rounded-full bg-[#d7ff46] px-2 py-0.5 text-xs font-semibold text-[#171915]">
                    {cartCount}
                  </span>
                )}
              </Link>
            </Button>
            {profileLabel ? (
              <Button
                asChild
                variant="ghost"
                className="hidden max-w-[220px] truncate text-[#171915] hover:bg-[#171915]/8 sm:inline-flex"
              >
                <Link to="/dashboard/settings" title={profileLabel}>
                  {profileLabel}
                </Link>
              </Button>
            ) : (
              <Button
                asChild
                variant="ghost"
                className="hidden text-[#171915] hover:bg-[#171915]/8 sm:inline-flex"
              >
                <Link to="/login">Войти</Link>
              </Button>
            )}
            <Button
              asChild
              className="hidden bg-[#171915] text-[#f6f4ed] hover:bg-[#2b2f27] sm:inline-flex"
            >
              <Link to={profileLabel ? "/dashboard" : "/signup"}>
                {profileLabel ? "Кабинет" : "Начать"}
                <ArrowRightIcon />
              </Link>
            </Button>
            <Button
              aria-label={mobileOpen ? "Закрыть меню" : "Открыть меню"}
              variant="outline"
              size="icon"
              className="border-[#171915]/14 bg-transparent md:hidden"
              onClick={() => setMobileOpen((value) => !value)}
            >
              {mobileOpen ? <XIcon /> : <MenuIcon />}
            </Button>
          </div>
        </div>
        {mobileOpen && (
          <div className="border-t border-[#171915]/10 bg-[#f6f4ed] px-4 py-3 md:hidden">
            <nav className="grid gap-1">
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      "rounded-md px-3 py-3 text-sm font-medium text-[#171915]/72",
                      isActive && "bg-[#171915] text-[#f6f4ed]",
                    )
                  }
                  end={item.to === "/"}
                  onClick={() => setMobileOpen(false)}
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <Button
                asChild
                variant="outline"
                className="col-span-2 border-[#171915]/14 bg-transparent"
              >
                <Link to={cartPageUrl} onClick={() => setMobileOpen(false)}>
                  <ShoppingCartIcon />
                  Корзина{cartCount ? ` · ${cartCount}` : ""}
                </Link>
              </Button>
              <Button
                asChild
                variant="outline"
                className="border-[#171915]/14 bg-transparent"
              >
                <Link
                  to={profileLabel ? "/dashboard/settings" : "/login"}
                  onClick={() => setMobileOpen(false)}
                >
                  {profileLabel || "Войти"}
                </Link>
              </Button>
              <Button
                asChild
                className="!bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27] hover:!text-[#f6f4ed]"
              >
                <Link
                  to={profileLabel ? "/dashboard" : "/signup"}
                  onClick={() => setMobileOpen(false)}
                >
                  {profileLabel ? "Кабинет" : "Начать"}
                </Link>
              </Button>
            </div>
          </div>
        )}
      </header>
      <div className="marketing-page">{children}</div>
      <FloatingAssistantWidget surface="landing" />
      <footer className="overflow-hidden border-t border-[#171915]/10 bg-[#11140f] text-[#f6f4ed]">
        <div className="mx-auto w-full max-w-7xl px-4 py-10 sm:px-6 sm:py-12 lg:px-8">
          <div className="grid gap-8 lg:grid-cols-[1.05fr_1.55fr] lg:gap-12">
            <div className="max-w-xl">
              <Link
                to="/"
                className="inline-flex items-center gap-3 font-semibold tracking-normal"
              >
                <span className="flex size-9 items-center justify-center rounded-md bg-[#d7ff46] text-[#11140f]">
                  <MessageSquareTextIcon className="size-5" />
                </span>
                <BrandWord aiClassName="text-[#73d8d2]" className="text-lg" />
              </Link>
              <p className="mt-5 max-w-md text-sm leading-6 text-[#f6f4ed]/66">
                Рабочий кабинет для AI-комментариев, парсинга, прогрева
                аккаунтов и управляемых кампаний в Telegram.
              </p>
              <div className="mt-6 grid gap-3 sm:grid-cols-2">
                <Link
                  to="/signup"
                  className="group inline-flex min-h-12 items-center justify-between rounded-md bg-[#d7ff46] px-4 text-sm font-semibold text-[#11140f] transition hover:bg-[#c6f532]"
                >
                  Создать кабинет
                  <ArrowRightIcon className="size-4 transition group-hover:translate-x-0.5" />
                </Link>
                <Link
                  to="/help"
                  className="inline-flex min-h-12 items-center justify-between rounded-md border border-[#f6f4ed]/16 px-4 text-sm font-semibold text-[#f6f4ed] transition hover:border-[#73d8d2]/70 hover:bg-[#73d8d2]/10"
                >
                  Инструкции
                  <ClipboardListIcon className="size-4 text-[#73d8d2]" />
                </Link>
              </div>
              <div className="mt-6 flex flex-wrap gap-2 text-xs font-medium text-[#f6f4ed]/70">
                <span className="rounded-full border border-[#f6f4ed]/12 px-3 py-1.5">
                  AI-комментарии
                </span>
                <span className="rounded-full border border-[#f6f4ed]/12 px-3 py-1.5">
                  Парсинг
                </span>
                <span className="rounded-full border border-[#f6f4ed]/12 px-3 py-1.5">
                  Прогрев
                </span>
              </div>
            </div>

            <div className="grid gap-6 sm:grid-cols-3">
              {footerSections.map((section) => (
                <div key={section.title}>
                  <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-[#73d8d2]">
                    {section.title}
                  </h2>
                  <nav className="mt-4 grid gap-3 text-sm text-[#f6f4ed]/68">
                    {section.links.map((link) =>
                      link.support ? (
                        <button
                          key={`${section.title}-${link.to}-${link.label}`}
                          type="button"
                          className="w-fit text-left transition hover:text-[#d7ff46]"
                          onClick={() => openAssistantSupport()}
                        >
                          {link.label}
                        </button>
                      ) : (
                        <Link
                          key={`${section.title}-${link.to}-${link.label}`}
                          to={link.to}
                          className="w-fit transition hover:text-[#d7ff46]"
                        >
                          {link.label}
                        </Link>
                      ),
                    )}
                  </nav>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-10 flex flex-col gap-3 border-t border-[#f6f4ed]/10 pt-5 text-xs text-[#f6f4ed]/46 sm:flex-row sm:items-center sm:justify-between">
            <p>© {new Date().getFullYear()} demo-platform</p>
            <div className="flex flex-wrap gap-x-4 gap-y-2">
              <Link to="/legal/privacy" className="transition hover:text-[#f6f4ed]">
                Конфиденциальность
              </Link>
              <Link to="/legal/payment" className="transition hover:text-[#f6f4ed]">
                Оплата
              </Link>
              <button
                type="button"
                className="transition hover:text-[#f6f4ed]"
                onClick={() => openAssistantSupport()}
              >
                Поддержка
              </button>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}

function ProductBackdrop() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 overflow-hidden"
    >
      <div className="absolute inset-0 bg-[linear-gradient(105deg,rgba(23,25,21,0.98)_0%,rgba(23,25,21,0.94)_42%,rgba(23,25,21,0.66)_64%,rgba(23,25,21,0.92)_100%)]" />
      <div className="absolute left-[62%] top-28 hidden w-[560px] rotate-[-7deg] rounded-xl border border-white/18 bg-[#191c17]/90 p-3 opacity-90 shadow-2xl shadow-black/30 lg:block xl:left-[57%] xl:w-[640px]">
        <div className="grid grid-cols-[170px_1fr] gap-3">
          <div className="rounded-lg bg-[#f6f4ed] p-3 text-[#171915]">
            <div className="mb-5 h-2 w-20 rounded-full bg-[#171915]" />
            {["Дашборд", "Аккаунты", "Источники", "Кампании", "Прогрев"].map(
              (item, index) => (
                <div
                  key={item}
                  className={cn(
                    "mb-2 rounded-md px-3 py-2 text-xs",
                    index === 0
                      ? "bg-[#d7ff46] font-semibold"
                      : "bg-[#171915]/6",
                  )}
                >
                  {item}
                </div>
              ),
            )}
          </div>
          <div className="rounded-lg bg-[#f6f4ed] p-4 text-[#171915]">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <div className="h-3 w-32 rounded-full bg-[#171915]" />
                <div className="mt-2 h-2 w-48 rounded-full bg-[#171915]/20" />
              </div>
              <div className="rounded-md bg-[#ff6f59] px-3 py-2 text-xs font-semibold text-white">
                Live
              </div>
            </div>
            <div className="grid grid-cols-3 gap-3">
              {[124, 38, 97].map((value, index) => (
                <div
                  key={value}
                  className="rounded-lg border border-[#171915]/12 bg-white p-3"
                >
                  <div className="text-2xl font-semibold">{value}</div>
                  <div className="mt-2 h-2 rounded-full bg-[#00a6a6]/35" />
                  <div
                    className={cn(
                      "mt-1 h-2 rounded-full",
                      index === 1
                        ? "w-1/2 bg-[#ff6f59]/45"
                        : "w-3/4 bg-[#d7ff46]",
                    )}
                  />
                </div>
              ))}
            </div>
            <div className="mt-4 rounded-lg border border-[#171915]/12 bg-white p-3">
              {[72, 55, 88, 64].map((width, index) => (
                <div
                  key={width}
                  className="mb-3 flex items-center gap-3 last:mb-0"
                >
                  <div
                    className={cn(
                      "size-7 rounded-md",
                      index % 2 ? "bg-[#00a6a6]" : "bg-[#d7ff46]",
                    )}
                  />
                  <div
                    className="h-2 rounded-full bg-[#171915]/18"
                    style={{ width: `${width}%` }}
                  />
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Hero() {
  return (
    <section className="relative isolate min-h-[calc(100svh-4rem)] overflow-hidden bg-[#171915] text-[#f6f4ed]">
      <ProductBackdrop />
      <div className="relative z-10 mx-auto flex min-h-[calc(100svh-4rem)] max-w-7xl items-center px-4 pb-20 pt-16 sm:px-6 lg:px-8">
        <div className="max-w-2xl lg:max-w-[620px] xl:max-w-3xl">
          <Badge className="mb-5 border-[#d7ff46]/40 bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]">
            7 дней бесплатно для новых пользователей
          </Badge>
          <h1 className="max-w-4xl text-5xl font-semibold leading-[0.95] tracking-normal text-balance sm:text-7xl lg:text-[5.8rem] xl:text-8xl">
            <BrandWord />
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-8 text-[#f6f4ed]/82 sm:text-xl">
            Запускайте AI-комментарии и рассылки через живые Telegram-аккаунты:
            аккаунты, прокси, источники, логи и отчеты в одном рабочем кабинете.
          </p>
          <p className="mt-4 max-w-xl text-sm leading-6 text-[#f6f4ed]/66">
            Зарегистрируйтесь и проверьте платформу без оплаты: тестовый доступ
            открывает рабочие модули на 7 дней с лимитами, которые помогают
            попробовать сценарий без лишнего риска.
          </p>
          <div className="mt-9 flex flex-col gap-3 sm:flex-row">
            <Button
              asChild
              size="lg"
              className="h-11 !bg-[#d7ff46] px-5 !text-[#171915] hover:!bg-[#c5ef31] hover:!text-[#171915] focus-visible:!bg-[#c5ef31] focus-visible:!text-[#171915]"
            >
              <Link to="/signup">
                Попробовать бесплатно
                <ArrowRightIcon />
              </Link>
            </Button>
            <Button
              asChild
              size="lg"
              variant="outline"
              className="h-11 border-[#f6f4ed]/24 bg-transparent px-5 text-[#f6f4ed] hover:bg-[#f6f4ed]/10 hover:text-[#f6f4ed]"
            >
              <a href={pricingUrl}>Посмотреть тарифы</a>
            </Button>
          </div>
          <div className="mt-8 grid max-w-2xl gap-3 text-sm text-[#f6f4ed]/70 sm:grid-cols-3">
            <div className="rounded-lg border border-[#f6f4ed]/12 bg-white/5 p-3">
              <strong className="block text-lg text-[#f6f4ed]">7 дней</strong>
              тестовый доступ к рабочим модулям
            </div>
            <div className="rounded-lg border border-[#f6f4ed]/12 bg-white/5 p-3">
              <strong className="block text-lg text-[#f6f4ed]">AI</strong>
              тон, промпты и humanizer-правила
            </div>
            <div className="rounded-lg border border-[#f6f4ed]/12 bg-white/5 p-3">
              <strong className="block text-lg text-[#f6f4ed]">Логи</strong>
              видно каждый комментарий и ошибку
            </div>
          </div>
        </div>
      </div>
      <div className="absolute bottom-0 left-0 right-0 h-16 border-t border-[#f6f4ed]/12 bg-[#f6f4ed] text-[#171915]">
        <div className="mx-auto flex h-full max-w-7xl items-center gap-6 overflow-hidden px-4 text-sm font-medium sm:px-6 lg:px-8">
          <span>AI-комментарии</span>
          <span className="text-[#00a6a6]">Парсинг</span>
          <span>Прокси</span>
          <span className="text-[#ff6f59]">Прогрев</span>
          <span className="text-[#00a6a6]">7 дней бесплатно</span>
          <span>Модульные тарифы</span>
        </div>
      </div>
    </section>
  );
}

function SectionIntro({
  eyebrow,
  title,
  text,
}: {
  eyebrow: string;
  title: string;
  text: string;
}) {
  return (
    <div className="mx-auto max-w-3xl text-center">
      <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#00a6a6]">
        {eyebrow}
      </div>
      <h2 className="text-3xl font-semibold leading-tight text-balance sm:text-5xl">
        {title}
      </h2>
      <p className="mt-4 text-base leading-7 text-[#171915]/66 sm:text-lg">
        {text}
      </p>
    </div>
  );
}

function ModulesGrid() {
  return (
    <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-20">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <SectionIntro
          eyebrow="Модули"
          title="Не один бот, а рабочая система для Telegram-операций"
          text="Все основные процессы собраны рядом: от импорта аккаунтов до запуска кампаний и контроля доступов."
        />
        <div className="mt-12 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {modules.map((module) => (
            <article
              key={module.title}
              className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6 shadow-sm"
            >
              <module.icon className="mb-5 size-7 text-[#00a6a6]" />
              <h3 className="text-xl font-semibold">{module.title}</h3>
              <p className="mt-3 leading-7 text-[#171915]/68">{module.text}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function PlatformHeroSection() {
  return (
    <section className="relative isolate overflow-hidden bg-[#171915] text-[#f6f4ed]">
      <div className="absolute inset-x-0 bottom-0 h-2 bg-[linear-gradient(90deg,#d7ff46,#00a6a6,#ff6f59)]" />
      <div className="mx-auto grid min-h-[760px] max-w-7xl items-center gap-10 px-4 py-20 sm:px-6 lg:grid-cols-[0.9fr_1.1fr] lg:px-8">
        <div className="relative z-10">
          <Badge className="mb-5 bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]">
            Платформа
          </Badge>
          <h1 className="max-w-4xl text-4xl font-semibold leading-[1.02] text-balance sm:text-6xl">
            demo-platform собирает Telegram-операции в один управляемый контур
          </h1>
          <p className="mt-6 max-w-2xl text-lg leading-8 text-[#f6f4ed]/76">
            Это не один бот и не набор разрозненных скриптов. Внутри кабинета
            живут аккаунты, прокси, источники, база каналов, AI-комментарии,
            прогрев, парсинг, контент-завод, логи и отчеты.
          </p>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row">
            <Button
              asChild
              size="lg"
              className="h-11 !bg-[#d7ff46] px-5 !text-[#171915] hover:!bg-[#c5ef31] hover:!text-[#171915]"
            >
              <Link to="/signup">
                Создать кабинет
                <ArrowRightIcon />
              </Link>
            </Button>
            <Button
              asChild
              size="lg"
              variant="outline"
              className="h-11 border-[#f6f4ed]/24 bg-transparent px-5 text-[#f6f4ed] hover:bg-[#f6f4ed]/10 hover:text-[#f6f4ed]"
            >
              <Link to="/help">Посмотреть инструкции</Link>
            </Button>
          </div>
        </div>
        <div className="relative">
          <div className="absolute -inset-8 rounded-[2rem] bg-[#00a6a6]/18 blur-3xl" />
          <div className="relative overflow-hidden rounded-xl border border-[#f6f4ed]/16 bg-[#f6f4ed] p-2 shadow-2xl shadow-black/40">
            <img
              src="/help/screenshots/01-dashboard-overview.png"
              alt="Дашборд платформы demo-platform"
              className="aspect-[16/10] w-full rounded-lg object-cover object-left-top"
            />
          </div>
          <div className="absolute -bottom-6 left-5 right-5 grid gap-2 rounded-lg border border-[#f6f4ed]/14 bg-[#11140f]/92 p-3 shadow-xl backdrop-blur sm:left-auto sm:w-[360px]">
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="text-[#f6f4ed]/62">Состояние кампаний</span>
              <span className="rounded-full bg-[#d7ff46] px-2.5 py-1 text-xs font-semibold text-[#171915]">
                live
              </span>
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="rounded-md bg-white/8 p-3">
                <strong className="block text-xl">13</strong>
                <span className="text-xs text-[#f6f4ed]/58">каналов</span>
              </div>
              <div className="rounded-md bg-white/8 p-3">
                <strong className="block text-xl">1 280</strong>
                <span className="text-xs text-[#f6f4ed]/58">постов</span>
              </div>
              <div className="rounded-md bg-white/8 p-3">
                <strong className="block text-xl">97%</strong>
                <span className="text-xs text-[#f6f4ed]/58">видимость</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function PlatformMetricsSection() {
  return (
    <section className="border-b border-[#171915]/10 bg-[#d7ff46] py-8 text-[#171915]">
      <div className="mx-auto grid max-w-7xl gap-3 px-4 sm:px-6 md:grid-cols-4 lg:px-8">
        {platformMetrics.map((metric) => (
          <div
            key={metric.value}
            className="rounded-lg border border-[#171915]/12 bg-[#f6f4ed]/76 p-5"
          >
            <div className="text-2xl font-semibold">{metric.value}</div>
            <p className="mt-2 text-sm leading-6 text-[#171915]/68">
              {metric.label}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function PlatformScreensSection() {
  return (
    <section className="bg-[#f6f4ed] py-20">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="grid gap-8 lg:grid-cols-[0.75fr_1.25fr] lg:items-end">
          <div>
            <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#00a6a6]">
              Экраны продукта
            </div>
            <h2 className="text-3xl font-semibold leading-tight sm:text-5xl">
              Видно не только запуск, но и последствия каждого действия
            </h2>
          </div>
          <p className="text-base leading-7 text-[#171915]/66">
            Платформа нужна не для красивой кнопки “запустить”. Главная ценность
            в том, что команда видит источники, аккаунты, черновики, ошибки,
            ограничения, результаты и может быстро понять, где процесс сломался.
          </p>
        </div>
        <div className="mt-10 grid gap-5 md:grid-cols-2">
          {platformScreenshots.map((screen, index) => (
            <article
              key={screen.title}
              className={cn(
                "overflow-hidden rounded-xl border border-[#171915]/10 bg-[#fffdf7] shadow-sm",
                index === 0 && "md:col-span-2",
              )}
            >
              <img
                src={screen.src}
                alt={`${screen.title} demo-platform`}
                className={cn(
                  "w-full border-b border-[#171915]/10 object-cover object-left-top",
                  index === 0 ? "aspect-[16/7]" : "aspect-[16/9]",
                )}
              />
              <div className="p-5">
                <h3 className="text-xl font-semibold">{screen.title}</h3>
                <p className="mt-2 text-sm leading-6 text-[#171915]/66">
                  {screen.text}
                </p>
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function PlatformFlowSection() {
  return (
    <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-20">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <SectionIntro
          eyebrow="Процесс"
          title="От источника до отчета без ручной диспетчерской"
          text="demo-platform связывает подготовку аккаунтов, поиск площадок, AI-генерацию, публикацию и контроль ошибок в один рабочий маршрут."
        />
        <div className="mt-12 grid gap-4 md:grid-cols-4">
          {platformFlow.map((step, index) => (
            <article
              key={step.title}
              className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
            >
              <div className="mb-5 flex size-10 items-center justify-center rounded-md bg-[#171915] font-semibold text-[#d7ff46]">
                {index + 1}
              </div>
              <h3 className="text-lg font-semibold leading-6">{step.title}</h3>
              <p className="mt-3 text-sm leading-7 text-[#171915]/66">
                {step.text}
              </p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function UseCasesHeroSection() {
  return (
    <section className="relative overflow-hidden bg-[#171915] text-[#f6f4ed]">
      <div className="absolute inset-x-0 bottom-0 h-2 bg-[linear-gradient(90deg,#d7ff46,#00a6a6,#ff6f59)]" />
      <div className="mx-auto grid max-w-7xl gap-10 px-4 py-20 sm:px-6 lg:grid-cols-[0.9fr_1.1fr] lg:px-8">
        <div className="self-center">
          <Badge className="mb-5 bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]">
            Сценарии
          </Badge>
          <h1 className="max-w-4xl text-4xl font-semibold leading-[1.02] text-balance sm:text-6xl">
            Готовые Telegram-сценарии, которые можно собрать в кабинете
          </h1>
          <p className="mt-6 max-w-2xl text-lg leading-8 text-[#f6f4ed]/76">
            Здесь не абстрактные “кейсы”, а рабочие маршруты: какие источники
            собрать, какие модули включить, как ограничить риск и что смотреть в
            результате.
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {scenarioPlaybooks.map((scenario) => (
            <div
              key={scenario.title}
              className="rounded-lg border border-[#f6f4ed]/12 bg-white/6 p-5"
            >
              <div className="mb-4 text-xs font-semibold uppercase tracking-[0.14em] text-[#73d8d2]">
                {scenario.audience}
              </div>
              <h2 className="text-xl font-semibold">{scenario.title}</h2>
              <p className="mt-3 text-sm leading-6 text-[#f6f4ed]/64">
                {scenario.result}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function ScenarioPlaybooksSection() {
  return (
    <section className="bg-[#f6f4ed] py-20">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <SectionIntro
          eyebrow="Playbooks"
          title="Не просто список возможностей, а готовые последовательности"
          text="Каждый сценарий можно начать с малого лимита, проверить результат и масштабировать только после понятных сигналов."
        />
        <div className="mt-12 grid gap-5 lg:grid-cols-2">
          {scenarioPlaybooks.map((scenario) => (
            <article
              key={scenario.title}
              className="rounded-xl border border-[#171915]/10 bg-[#fffdf7] p-6 shadow-sm"
            >
              <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <div className="mb-3 text-sm font-semibold text-[#00a6a6]">
                    {scenario.audience}
                  </div>
                  <h3 className="text-2xl font-semibold leading-tight">
                    {scenario.title}
                  </h3>
                </div>
                <div className="flex flex-wrap gap-2 sm:max-w-[260px] sm:justify-end">
                  {scenario.modules.map((module) => (
                    <Badge
                      key={module}
                      variant="outline"
                      className="border-[#171915]/12 bg-[#f6f4ed] text-[#171915]"
                    >
                      {module}
                    </Badge>
                  ))}
                </div>
              </div>
              <div className="mt-5 rounded-lg bg-[#d7ff46]/35 p-4 text-sm leading-6 text-[#171915]/74">
                <strong className="text-[#171915]">Цель: </strong>
                {scenario.result}
              </div>
              <ol className="mt-5 grid gap-3">
                {scenario.steps.map((step, index) => (
                  <li key={step} className="flex gap-3 text-sm leading-6">
                    <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md bg-[#171915] text-xs font-semibold text-[#d7ff46]">
                      {index + 1}
                    </span>
                    <span className="text-[#171915]/70">{step}</span>
                  </li>
                ))}
              </ol>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function UseCasesProofSection() {
  return (
    <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-20">
      <div className="mx-auto grid max-w-7xl gap-8 px-4 sm:px-6 lg:grid-cols-[0.9fr_1.1fr] lg:px-8">
        <div>
          <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#ff6f59]">
            Что получает команда
          </div>
          <h2 className="text-3xl font-semibold leading-tight sm:text-5xl">
            Сценарий заканчивается не “AI написал текст”, а понятным отчетом
          </h2>
          <p className="mt-5 leading-7 text-[#171915]/66">
            В хорошей Telegram-кампании важно видеть, какие источники сработали,
            где аккаунт уперся в ошибку, какой комментарий ушел, почему часть
            постов не прошла фильтр и что можно повторить.
          </p>
          <div className="mt-7 flex flex-col gap-3 sm:flex-row">
            <Button
              asChild
              className="!bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27]"
            >
              <Link to="/signup">
                Собрать свой сценарий
                <ArrowRightIcon />
              </Link>
            </Button>
            <Button asChild variant="outline" className="border-[#171915]/18">
              <Link to="/platform">Посмотреть платформу</Link>
            </Button>
          </div>
        </div>
        <div className="overflow-hidden rounded-xl border border-[#171915]/10 bg-[#f6f4ed] shadow-sm">
          <img
            src="/help/screenshots/05-ai-comments.png"
            alt="Экран AI-комментариев demo-platform"
            className="aspect-[16/10] w-full object-cover object-left-top"
          />
          <div className="grid gap-3 border-t border-[#171915]/10 p-5 sm:grid-cols-3">
            {["Черновик", "Отправлено", "Ошибка"].map((status) => (
              <div key={status} className="rounded-lg bg-[#fffdf7] p-4">
                <div className="text-sm font-semibold">{status}</div>
                <p className="mt-2 text-xs leading-5 text-[#171915]/62">
                  Статусы остаются в истории, чтобы команда понимала, что
                  реально произошло.
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function PricingPreview() {
  const { session } = useMarketingSession();
  const { access } = useMarketingAccess(session);
  const { items, addItem } = useMarketingCart(session, access);
  return (
    <section id="pricing-plans" className="bg-[#f6f4ed] py-20">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="flex flex-col justify-between gap-6 md:flex-row md:items-end">
          <div>
            <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#ff6f59]">
              Тарифы
            </div>
            <h2 className="max-w-3xl text-3xl font-semibold leading-tight sm:text-5xl">
              Стартуем с понятной базы, масштабируемся модулями
            </h2>
          </div>
          <p className="max-w-md text-sm leading-6 text-[#171915]/62">
            Оплата открывает доступ на фиксированный срок. Автоматических
            списаний и скрытого продления нет.
          </p>
        </div>
        <p className="mt-5 max-w-3xl text-base leading-7 text-[#171915]/66">
          Начните с 7-дневного теста. Для самостоятельной работы подключите
          Core, а для первого внедрения с нашей помощью выберите пилот.
        </p>
        <div className="mt-6 rounded-lg border border-[#00a6a6]/24 bg-[#00a6a6]/8 p-4 text-sm leading-6 text-[#171915]/72">
          <strong className="text-[#171915]">Тестовый период:</strong> 7 дней
          доступа ко всем рабочим модулям с ограниченными лимитами. Этого
          хватает, чтобы завести первые аккаунты, проверить источники и понять,
          какой тариф нужен для постоянной работы.
        </div>
        <div className="mt-10 grid items-stretch gap-5 md:grid-cols-3">
          {pricingPlans.map((plan) => {
            const code = plan.code;
            const price = plan.price;
            const owned = productAlreadyOwned(code, access);
            const inCart = items.includes(code);
            return (
              <article
                key={plan.name}
                className={cn(
                  "flex min-h-[540px] flex-col rounded-lg border p-6",
                  plan.featured
                    ? "border-[#171915] bg-[#171915] text-[#f6f4ed]"
                    : "border-[#171915]/10 bg-[#fffdf7]",
                )}
              >
                <div className="flex min-h-10 items-start justify-between gap-3">
                  <h3 className="text-2xl font-semibold leading-none">
                    {plan.name}
                  </h3>
                  <Badge
                    className={cn(
                      "shrink-0 whitespace-nowrap",
                      plan.featured
                        ? "bg-[#d7ff46] text-[#171915]"
                        : "bg-[#171915] text-[#f6f4ed]",
                    )}
                  >
                    {plan.note}
                  </Badge>
                </div>
                <div className="mt-10 text-4xl font-semibold leading-none">
                  {price ? formatRub(price) : "0 ₽"}
                </div>
                <div
                  className={cn(
                    "mt-2 text-sm",
                    plan.featured ? "text-[#f6f4ed]/62" : "text-[#171915]/54",
                  )}
                >
                  {price ? "за 30 дней" : "без оплаты"}
                </div>
                <p
                  className={cn(
                    "mt-5 min-h-[116px] leading-7",
                    plan.featured ? "text-[#f6f4ed]/72" : "text-[#171915]/66",
                  )}
                >
                  {plan.description}
                </p>
                <div className="mt-4 grid gap-3">
                  {plan.features.map((feature) => (
                    <div
                      key={feature}
                      className="flex items-start gap-2 text-sm"
                    >
                      <CheckIcon
                        className={cn(
                          "mt-0.5 size-4",
                          plan.featured ? "text-[#d7ff46]" : "text-[#00a6a6]",
                        )}
                      />
                      <span>{feature}</span>
                    </div>
                  ))}
                </div>
                {plan.href ? (
                  <div className="mt-auto pt-7">
                    <Button
                      asChild
                      className={cn(
                        "h-10 w-full text-sm",
                        plan.featured
                          ? "!bg-[#d7ff46] !text-[#171915] hover:!bg-[#c5ef31] hover:!text-[#171915]"
                          : "!bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27] hover:!text-[#f6f4ed]",
                      )}
                    >
                      <Link to={plan.href}>
                        {plan.cta}
                        <ArrowRightIcon />
                      </Link>
                    </Button>
                  </div>
                ) : (
                  <div className="mt-auto pt-7">
                    <Button
                      type="button"
                      className={cn(
                        "h-10 w-full text-sm",
                        plan.featured
                          ? "!bg-[#d7ff46] !text-[#171915] hover:!bg-[#c5ef31] hover:!text-[#171915]"
                          : "!bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27] hover:!text-[#f6f4ed]",
                      )}
                      disabled={owned}
                      onClick={() => addItem(code)}
                    >
                      {owned
                        ? "Уже подключено"
                        : inCart
                          ? "В корзине"
                          : plan.cta}
                      {owned ? <CheckIcon /> : <ShoppingCartIcon />}
                    </Button>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function SafetyOperationsSection() {
  const controls = [
    {
      icon: GaugeIcon,
      title: "Лимиты и интервалы",
      text: "Задавайте дневные лимиты, задержки, частоту комментариев и случайный диапазон постов, чтобы кампания не выглядела как механический спам.",
    },
    {
      icon: ShieldCheckIcon,
      title: "Фильтры мусорных постов",
      text: "Стоп-слова, ручная проверка черновиков и humanizer-правила помогают не комментировать рекламу, промокоды, конкурсы и другой шум.",
    },
    {
      icon: RadioTowerIcon,
      title: "Логи и карантин",
      text: "Ошибки Telegram, спам-блоки, проблемы прокси и недоступные источники видны в админке, а опасные аккаунты уходят в карантин.",
    },
  ];

  return (
    <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="mb-8 flex flex-col justify-between gap-4 md:flex-row md:items-end">
          <div>
            <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#00a6a6]">
              Контроль
            </div>
            <h2 className="max-w-3xl text-3xl font-semibold leading-tight sm:text-4xl">
              Кампании должны работать аккуратно, а не сжигать аккаунты
            </h2>
            <p className="mt-4 max-w-2xl leading-7 text-[#171915]/66">
              demo-platform не пытается обходить Telegram. Платформа помогает
              запускать активность осторожнее: через лимиты, паузы, фильтры,
              понятные логи и ручной контроль.
            </p>
          </div>
          <Button
            asChild
            className="!bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27] hover:!text-[#f6f4ed]"
          >
            <Link to="/signup">
              Создать кабинет
              <ArrowRightIcon />
            </Link>
          </Button>
        </div>
        <div className="grid gap-4 md:grid-cols-3">
          {controls.map((item) => (
            <article
              key={item.title}
              className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
            >
              <item.icon className="mb-5 size-7 text-[#00a6a6]" />
              <h3 className="text-xl font-semibold">{item.title}</h3>
              <p className="mt-3 text-sm leading-7 text-[#171915]/66">
                {item.text}
              </p>
            </article>
          ))}
        </div>
        <p className="mt-5 rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-4 text-sm leading-6 text-[#171915]/64">
          Важно: Telegram может ограничивать аккаунты из-за жалоб, однотипных
          действий, плохих прокси или агрессивных настроек. Поэтому безопасные
          лимиты и мониторинг - часть рабочего процесса, а не декоративная
          настройка.
        </p>
      </div>
    </section>
  );
}

export function PricingComparison() {
  return (
    <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="mb-8 flex flex-col justify-between gap-4 md:flex-row md:items-end">
          <div>
            <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#00a6a6]">
              Сравнение
            </div>
            <h2 className="max-w-3xl text-3xl font-semibold leading-tight sm:text-4xl">
              Что входит в каждый уровень доступа
            </h2>
          </div>
          <p className="max-w-md leading-7 text-[#171915]/66">
            После подключения тарифа кабинет получает нужные модули, лимиты и
            доступы для работы команды.
          </p>
        </div>
        <div className="overflow-x-auto rounded-lg border border-[#171915]/10">
          <table className="w-full min-w-[760px] border-collapse bg-[#f6f4ed] text-sm">
            <thead>
              <tr className="bg-[#171915] text-left text-[#f6f4ed]">
                <th className="px-5 py-4 font-semibold">Возможность</th>
                <th className="px-5 py-4 font-semibold">Free</th>
                <th className="px-5 py-4 font-semibold">Start</th>
                <th className="px-5 py-4 font-semibold">Core</th>
                <th className="px-5 py-4 font-semibold">Agency</th>
              </tr>
            </thead>
            <tbody>
              {comparisonRows.map((row) => (
                <tr key={row.feature} className="border-t border-[#171915]/10">
                  <td className="px-5 py-4 font-medium">{row.feature}</td>
                  <td className="px-5 py-4 text-[#171915]/68">{row.free}</td>
                  <td className="px-5 py-4 text-[#171915]/68">{row.start}</td>
                  <td className="px-5 py-4 text-[#171915]/68">{row.core}</td>
                  <td className="px-5 py-4 text-[#171915]/68">{row.agency}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

export function ModulesMarketplace() {
  const [billingCycle, setBillingCycle] =
    React.useState<BillingCycle>("monthly");
  const { session } = useMarketingSession();
  const { access } = useMarketingAccess(session);
  const { items, addItem, setCartItems } = useMarketingCart(session, access);
  const available = standaloneProducts;

  return (
    <section id="modules" className="bg-[#f6f4ed] py-16">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="mb-8 flex flex-col justify-between gap-4 md:flex-row md:items-end">
          <div>
            <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#00a6a6]">
              Магазин модулей
            </div>
            <h2 className="max-w-3xl text-3xl font-semibold leading-tight sm:text-4xl">
              Подключайте только нужные инструменты
            </h2>
            <p className="mt-4 max-w-3xl leading-7 text-[#171915]/66">
              Выберите отдельный модуль или готовый тариф. На следующем шаге
              корзина соберет сумму и отправит в оплату.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <div className="inline-flex rounded-lg border border-[#171915]/12 bg-[#fffdf7] p-1">
              <button
                type="button"
                className={cn(
                  "rounded-md px-4 py-2 text-sm font-semibold transition",
                  billingCycle === "monthly"
                    ? "bg-[#171915] text-[#f6f4ed]"
                    : "text-[#171915]/62 hover:text-[#171915]",
                )}
                onClick={() => setBillingCycle("monthly")}
              >
                1 месяц
              </button>
              <button
                type="button"
                className={cn(
                  "rounded-md px-4 py-2 text-sm font-semibold transition",
                  billingCycle === "yearly"
                    ? "bg-[#171915] text-[#f6f4ed]"
                    : "text-[#171915]/62 hover:text-[#171915]",
                )}
                onClick={() => setBillingCycle("yearly")}
              >
                1 год
              </button>
            </div>
            <Button
              className="!bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27] hover:!text-[#f6f4ed]"
              type="button"
              onClick={() =>
                setCartItems([
                  cycleCode("module.neurocommenting", billingCycle),
                  cycleCode("module.message_broadcast", billingCycle),
                  cycleCode("module.warmup", billingCycle),
                ])
              }
            >
              Собрать 3 модуля
              <ShoppingCartIcon />
            </Button>
            <Button
              asChild
              variant="outline"
              className="border-[#171915]/18 bg-transparent"
            >
              <Link to={cartPageUrl}>
                Открыть корзину
                {cartProducts(items).length > 0 &&
                  ` · ${cartProducts(items).length}`}
              </Link>
            </Button>
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {available.map((product) => {
            const code = cycleCode(product.code, billingCycle);
            const price =
              billingCycle === "yearly"
                ? product.yearlyPrice || product.price
                : product.price;
            const owned = productAlreadyOwned(code, access);
            const inCart = items.includes(code);
            return (
              <article
                key={product.code}
                className="flex min-h-[310px] flex-col rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-6"
              >
                <div className="flex items-start justify-between gap-3">
                  <h3 className="text-xl font-semibold">{product.name}</h3>
                </div>
                <p className="mt-4 min-h-[84px] leading-7 text-[#171915]/66">
                  {product.description}
                </p>
                <div className="mt-4 text-2xl font-semibold">
                  {formatRub(price)} /{" "}
                  {billingCycle === "yearly" ? "год" : "месяц"}
                </div>
                <Button
                  type="button"
                  className="mt-auto !bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27] hover:!text-[#f6f4ed]"
                  disabled={owned}
                  onClick={() => addItem(code)}
                >
                  {owned
                    ? "Уже подключено"
                    : inCart
                      ? "В корзине"
                      : "Добавить в корзину"}
                  {owned ? <CheckIcon /> : <ShoppingCartIcon />}
                </Button>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

export function CheckoutPage() {
  const location = useLocation();
  const [provider, setProvider] = React.useState<"yookassa" | "nowpayments">(
    "yookassa",
  );
  const { session, loading: sessionLoading } = useMarketingSession();
  const { access, loading: accessLoading } = useMarketingAccess(session);
  const { items, removeItem, setCartItems, clearCart, syncing } =
    useMarketingCart(session, access);
  const queryItems = React.useMemo(() => {
    const params = new URLSearchParams(location.search);
    return normalizeCartItems(
      (params.get("items") || "")
        .split(",")
        .map((item) => decodeURIComponent(item).trim())
        .filter(Boolean),
    );
  }, [location.search]);

  React.useEffect(() => {
    if (queryItems.length) {
      setCartItems(queryItems);
      window.history.replaceState({}, "", cartPageUrl);
    }
  }, [queryItems, setCartItems]);

  const [checkoutBusy, setCheckoutBusy] = React.useState(false);
  const [checkoutError, setCheckoutError] = React.useState<string | null>(null);
  const selected = cartProducts(items);
  const total = selected.reduce((sum, product) => sum + product.price, 0);

  async function handleCartCheckout() {
    setCheckoutError(null);
    if (!selected.length) return;
    trackGoal("checkout_start", {
      provider,
      items: selected.map((product) => product.code).join(","),
      value: total,
    });
    const payableItems = filterOwnedCartItems(items, access);
    if (!payableItems.length) {
      setCheckoutError("Все товары в корзине уже подключены к вашему профилю.");
      return;
    }
    if (!session) {
      window.location.assign(`/login?next=${encodeURIComponent("/checkout")}`);
      return;
    }
    setCheckoutBusy(true);
    try {
      await updateCartRequest(
        session.token,
        session.workspaceId,
        payableItems,
        provider,
      );
      const response = await checkoutCartRequest(
        session.token,
        session.workspaceId,
        provider,
      );
      if (!response.checkout_url) {
        throw new Error("Платеж создан, но ссылка на оплату не получена.");
      }
      trackGoal("payment_redirect", { provider, value: total });
      window.location.assign(response.checkout_url);
    } catch (error) {
      setCheckoutError(
        error instanceof Error ? error.message : "Не удалось открыть оплату.",
      );
    } finally {
      setCheckoutBusy(false);
    }
  }

  return (
    <MarketingLayout>
      <PageMeta
        title="Корзина demo-platform"
        description="Корзина demo-platform: выбранные тарифы и модули, оплата картой, СБП или crypto invoice."
        canonicalPath="/checkout"
        noIndex
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge="Корзина"
          title="Проверьте выбранные модули перед оплатой"
          text="Оплата запускается из кабинета: после платежа workspace автоматически получает нужные доступы."
        />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto grid max-w-6xl gap-6 px-4 sm:px-6 lg:grid-cols-[1.2fr_0.8fr] lg:px-8">
            <div className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <h2 className="text-2xl font-semibold">Состав корзины</h2>
                  <p className="mt-2 text-sm leading-6 text-[#171915]/62">
                    Добавляйте тарифы и модули на странице тарифов, здесь можно
                    убрать лишнее и перейти к оплате.
                  </p>
                </div>
                {selected.length > 0 && (
                  <Button
                    type="button"
                    variant="outline"
                    className="border-[#171915]/14 bg-transparent"
                    onClick={clearCart}
                  >
                    <Trash2Icon />
                    Очистить
                  </Button>
                )}
              </div>
              <div className="mt-6 grid gap-3">
                {selected.length ? (
                  selected.map((product) => (
                    <div
                      key={product.code}
                      className="grid gap-4 rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-4 sm:grid-cols-[1fr_auto] sm:items-start"
                    >
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <div className="font-semibold">{product.name}</div>
                          {"billingCycle" in product &&
                            product.billingCycle === "yearly" && (
                              <Badge className="bg-[#d7ff46] text-[#171915]">
                                1 год
                              </Badge>
                            )}
                        </div>
                        <p className="mt-1 text-sm leading-6 text-[#171915]/62">
                          {product.description}
                        </p>
                      </div>
                      <div className="flex items-center justify-between gap-3 sm:justify-end">
                        <div className="shrink-0 text-lg font-semibold">
                          {formatRub(product.price)}
                        </div>
                        <Button
                          type="button"
                          variant="outline"
                          size="icon"
                          className="border-[#171915]/14 bg-transparent"
                          aria-label={`Удалить ${product.name}`}
                          onClick={() => removeItem(product.code)}
                        >
                          <XIcon />
                        </Button>
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="rounded-lg border border-dashed border-[#171915]/18 bg-[#fffdf7] p-8 text-center text-[#171915]/66">
                    <ShoppingCartIcon className="mx-auto mb-4 size-8 text-[#171915]/40" />
                    <div className="font-semibold text-[#171915]">
                      Корзина пустая
                    </div>
                    <p className="mt-2 text-sm">
                      Выберите тариф или модуль, а затем вернитесь сюда для
                      оплаты.
                    </p>
                    <Button
                      asChild
                      className="mt-5 !bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27] hover:!text-[#f6f4ed]"
                    >
                      <Link to="/pricing#pricing-plans">Перейти к тарифам</Link>
                    </Button>
                  </div>
                )}
              </div>
            </div>
            <aside className="rounded-lg border border-[#171915]/10 bg-[#171915] p-6 text-[#f6f4ed]">
              <div className="text-sm uppercase tracking-[0.16em] text-[#d7ff46]">
                К оплате
              </div>
              <div className="mt-4 text-5xl font-semibold">
                {formatRub(total)}
              </div>
              <p className="mt-4 leading-7 text-[#f6f4ed]/70">
                {session
                  ? `Корзина привязана к профилю ${session.user.email}. Оплата выдаст доступы этому workspace.`
                  : "Войдите в кабинет перед оплатой: корзина сохранится и будет привязана к вашему профилю."}
              </p>
              {(syncing || sessionLoading || accessLoading) && (
                <p className="mt-3 text-sm text-[#f6f4ed]/55">
                  Синхронизирую корзину с профилем...
                </p>
              )}
              {checkoutError && (
                <p className="mt-3 rounded-md bg-[#ff6f59]/16 p-3 text-sm text-[#ffb3a8]">
                  {checkoutError}
                </p>
              )}
              <div className="mt-8 rounded-lg border border-[#f6f4ed]/14 bg-[#f6f4ed]/6 p-2">
                <button
                  type="button"
                  className={cn(
                    "flex w-full items-center justify-between rounded-md px-4 py-3 text-left text-sm font-semibold transition",
                    provider === "yookassa"
                      ? "bg-[#d7ff46] text-[#171915]"
                      : "text-[#f6f4ed]/76 hover:bg-[#f6f4ed]/8",
                  )}
                  onClick={() => setProvider("yookassa")}
                >
                  Карта / СБП
                  <CreditCardIcon className="size-4" />
                </button>
                <button
                  type="button"
                  className={cn(
                    "mt-2 flex w-full items-center justify-between rounded-md px-4 py-3 text-left text-sm font-semibold transition",
                    provider === "nowpayments"
                      ? "bg-[#d7ff46] text-[#171915]"
                      : "text-[#f6f4ed]/76 hover:bg-[#f6f4ed]/8",
                  )}
                  onClick={() => setProvider("nowpayments")}
                >
                  Crypto invoice
                  <WalletCardsIcon className="size-4" />
                </button>
              </div>
              <div className="mt-8 grid gap-3">
                {selected.length > 0 ? (
                  <Button
                    type="button"
                    disabled={checkoutBusy || syncing || accessLoading}
                    className="!bg-[#d7ff46] !text-[#171915] hover:!bg-[#c5ef31] hover:!text-[#171915]"
                    onClick={() => void handleCartCheckout()}
                  >
                    {checkoutBusy
                      ? "Открываю оплату"
                      : session
                        ? "Оплатить"
                        : "Войти и оплатить"}
                    <ArrowRightIcon />
                  </Button>
                ) : (
                  <Button disabled className="!bg-[#d7ff46] !text-[#171915]">
                    Корзина пустая
                  </Button>
                )}
                <Button
                  asChild
                  variant="outline"
                  className="border-[#f6f4ed]/22 bg-transparent text-[#f6f4ed] hover:bg-[#f6f4ed]/10 hover:text-[#f6f4ed]"
                >
                  <Link to="/pricing#modules">Вернуться к тарифам</Link>
                </Button>
              </div>
            </aside>
          </div>
        </section>
      </main>
    </MarketingLayout>
  );
}

function PricingFaq() {
  return (
    <section className="bg-[#f6f4ed] py-16">
      <div className="mx-auto grid max-w-7xl gap-8 px-4 sm:px-6 lg:grid-cols-[0.8fr_1.2fr] lg:px-8">
        <div>
          <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#ff6f59]">
            Оплата и доступы
          </div>
          <h2 className="text-3xl font-semibold leading-tight sm:text-4xl">
            FAQ для тарифной модели
          </h2>
          <p className="mt-4 leading-7 text-[#171915]/66">
            Модули, лимиты, выдача доступа и старт без сложной настройки.
          </p>
        </div>
        <div className="grid gap-3">
          {pricingFaqs.map((faq) => (
            <details
              key={faq.question}
              className="group rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-5"
            >
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4 font-semibold">
                <span>{faq.question}</span>
                <CircleHelpIcon className="size-5 text-[#00a6a6] transition group-open:rotate-45" />
              </summary>
              <p className="mt-4 leading-7 text-[#171915]/68">{faq.answer}</p>
            </details>
          ))}
        </div>
      </div>
    </section>
  );
}

function WorkflowSection() {
  const steps = [
    [
      "1",
      "Создаете кабинет",
      "Получаете workspace, загружаете Telegram-аккаунты и привязываете прокси.",
    ],
    [
      "2",
      "Добавляете источники",
      "Каналы проверяются через Telegram API, комментарии и доступность отображаются в списке.",
    ],
    [
      "3",
      "Запускаете кампанию",
      "Выбираете аккаунты, источники, тон, промпт и режим отправки комментариев.",
    ],
    [
      "4",
      "Смотрите результат",
      "Внутри кампании видны комментарии, статусы, ошибки, ссылки на посты и выгрузка XLSX.",
    ],
  ];

  return (
    <section className="bg-[#f6f4ed] py-20">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <SectionIntro
          eyebrow="Как это работает"
          title="От регистрации до первого AI-комментария без лишних экранов"
          text="Лендинг приводит пользователя к оплате и регистрации, а кабинет ведет по понятному рабочему сценарию."
        />
        <div className="mt-12 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {steps.map(([number, title, text]) => (
            <article
              key={number}
              className="rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-6"
            >
              <div className="mb-5 flex size-10 items-center justify-center rounded-md bg-[#171915] font-semibold text-[#d7ff46]">
                {number}
              </div>
              <h3 className="text-xl font-semibold">{title}</h3>
              <p className="mt-3 text-sm leading-7 text-[#171915]/66">{text}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function CtaBand() {
  return (
    <section className="bg-[#00a6a6] py-16 text-white">
      <div className="mx-auto flex max-w-7xl flex-col justify-between gap-8 px-4 sm:px-6 md:flex-row md:items-center lg:px-8">
        <div>
          <h2 className="max-w-2xl text-3xl font-semibold leading-tight sm:text-5xl">
            Покажем платформу на ваших Telegram-сценариях
          </h2>
          <p className="mt-4 max-w-xl leading-7 text-white/78">
            Разберем источники, аккаунты, кампании, лимиты и путь до первого
            запуска.
          </p>
        </div>
        <Button
          asChild
          size="lg"
          className="h-11 bg-[#171915] text-[#f6f4ed] hover:bg-[#2b2f27]"
        >
          <Link to="/signup">
            Перейти к регистрации
            <ArrowRightIcon />
          </Link>
        </Button>
      </div>
    </section>
  );
}

function SeoKnowledgeSection() {
  const articles = Object.values(blogArticles).slice(0, 3);

  return (
    <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <SectionIntro
          eyebrow="SEO и практика"
          title="Разбираем Telegram-маркетинг по рабочим темам"
          text="Материалы помогают понять лимиты Telegram, AI-комментарии, UTM-метки и безопасный запуск кампаний до регистрации в кабинете."
        />
        <div className="mt-10 grid gap-4 md:grid-cols-3">
          {articles.map((article) => (
            <article
              key={article.slug}
              className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
            >
              <Badge
                variant="outline"
                className="mb-4 border-[#171915]/12 bg-[#fffdf7] text-[#171915]"
              >
                {article.category}
              </Badge>
              <h3 className="text-xl font-semibold leading-tight">
                <Link to={`/blog/${article.slug}`}>{article.title}</Link>
              </h3>
              <p className="mt-3 text-sm leading-7 text-[#171915]/66">
                {article.description}
              </p>
              <Link
                to={`/blog/${article.slug}`}
                className="mt-5 inline-flex items-center gap-2 text-sm font-semibold text-[#006d6d]"
              >
                Читать статью
                <ArrowRightIcon className="size-4" />
              </Link>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

export function HomePage() {
  return (
    <MarketingLayout>
      <PageMeta
        title="demo-platform — AI-комментарии и автоматизация Telegram"
        description="Платформа для AI-комментариев, прогрева Telegram-аккаунтов, прокси, источников, кампаний, логов и отчетов в одном кабинете."
        canonicalPath="/"
        structuredData={homePageSchema()}
      />
      <Hero />
      <ModulesGrid />
      <WorkflowSection />
      <SeoKnowledgeSection />
      <PricingPreview />
      <SafetyOperationsSection />
      <CtaBand />
    </MarketingLayout>
  );
}

export function PlatformPage() {
  return (
    <MarketingLayout>
      <PageMeta
        title="Платформа demo-platform — Telegram-аккаунты, прокси и кампании"
        description="Операционный центр для Telegram-маркетинга: аккаунты, прокси, источники, AI-комментарии, прогрев, логи, отчеты и командные доступы."
        canonicalPath="/platform"
        structuredData={softwareSchema("/platform")}
      />
      <main className="bg-[#f6f4ed]">
        <PlatformHeroSection />
        <PlatformMetricsSection />
        <PlatformScreensSection />
        <PlatformFlowSection />
        <ModulesGrid />
        <SafetyOperationsSection />
        <CtaBand />
      </main>
    </MarketingLayout>
  );
}

export function PricingPage() {
  return (
    <MarketingLayout>
      <PageMeta
        title="Тарифы demo-platform — цены на AI-комментарии и прогрев"
        description="Core на 30 дней или пилот с внедрением demo-platform без автосписаний: от подключения Telegram-аккаунта до первой кампании и отчета."
        canonicalPath="/pricing"
        structuredData={softwareSchema("/pricing")}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge="Тарифы"
          title="Один рабочий пакет и пилот с внедрением"
          text="Core для самостоятельной работы или 30-дневный пилот, в котором мы доводим первый Telegram-сценарий до результата."
        />
        <PricingPreview />
        <SafetyOperationsSection />
        <PricingFaq />
        <CtaBand />
      </main>
    </MarketingLayout>
  );
}

export function UseCasesPage() {
  return (
    <MarketingLayout>
      <PageMeta
        title="Сценарии demo-platform — AI-комментарии для Telegram-маркетинга"
        description="Как агентствам, маркетологам и продюсерам запускать Telegram-кампании, тестировать офферы, прогревать аккаунты и вести отчеты."
        canonicalPath="/use-cases"
        structuredData={softwareSchema("/use-cases")}
      />
      <main className="bg-[#fffdf7]">
        <UseCasesHeroSection />
        <ScenarioPlaybooksSection />
        <UseCasesProofSection />
        <SeoKnowledgeSection />
        <PricingPreview />
        <CtaBand />
      </main>
    </MarketingLayout>
  );
}

export function BlogIndexPage() {
  const articles = Object.values(blogArticles);

  return (
    <MarketingLayout>
      <PageMeta
        title="Статьи demo-platform — Telegram-маркетинг, AI-комментарии и прогрев"
        description="Практические статьи demo-platform про Telegram-маркетинг: AI-комментарии, UTM-метки, FloodWait, прогрев аккаунтов, безопасность и аналитика."
        canonicalPath="/blog"
        structuredData={blogIndexSchema()}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge="Статьи"
          title="Практический SEO-раздел по Telegram-маркетингу"
          text="Гайды для команд, которые хотят запускать Telegram-кампании аккуратно: с лимитами, UTM, логами, AI-комментариями и понятной аналитикой."
        />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
            <SectionIntro
              eyebrow="Полезные материалы"
              title="Запросы, по которым нас должны находить"
              text="Каждая статья закрывает отдельную боль: от FloodWait и прогрева до UTM-меток и безопасного нейрокомментинга."
            />
            <div className="mt-10 grid gap-4 md:grid-cols-2">
              {articles.map((article) => (
                <article
                  key={article.slug}
                  className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
                >
                  <div className="mb-4 flex flex-wrap items-center gap-2">
                    <Badge className="bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]">
                      {article.category}
                    </Badge>
                    <span className="text-sm text-[#171915]/52">
                      {article.readTime}
                    </span>
                  </div>
                  <h2 className="text-2xl font-semibold leading-tight">
                    <Link to={`/blog/${article.slug}`}>{article.title}</Link>
                  </h2>
                  <p className="mt-4 leading-7 text-[#171915]/68">
                    {article.description}
                  </p>
                  <div className="mt-5 flex flex-wrap gap-2">
                    {article.keywords.slice(0, 3).map((keyword) => (
                      <Badge
                        key={keyword}
                        variant="outline"
                        className="border-[#171915]/12 bg-[#fffdf7] text-[#171915]"
                      >
                        {keyword}
                      </Badge>
                    ))}
                  </div>
                  <Button
                    asChild
                    variant="outline"
                    className="mt-6 border-[#171915]/18 bg-transparent"
                  >
                    <Link to={`/blog/${article.slug}`}>
                      Читать
                      <ArrowRightIcon />
                    </Link>
                  </Button>
                </article>
              ))}
            </div>
          </div>
        </section>
        <CtaBand />
      </main>
    </MarketingLayout>
  );
}

export function BlogArticlePage({ articleKey }: { articleKey: string }) {
  const article = blogArticles[articleKey] || blogArticles["ai-comments-telegram-guide"];

  return (
    <MarketingLayout>
      <PageMeta
        title={`${article.title} — demo-platform`}
        description={article.description}
        canonicalPath={`/blog/${article.slug}`}
        structuredData={blogArticleSchema(article)}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge={article.badge}
          title={article.title}
          text={article.intro}
        />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto grid max-w-7xl gap-8 px-4 sm:px-6 lg:grid-cols-[0.72fr_0.28fr] lg:px-8">
            <article className="min-w-0">
              <div className="mb-8 flex flex-wrap items-center gap-3 text-sm text-[#171915]/58">
                <span>{article.category}</span>
                <span>•</span>
                <span>{article.readTime}</span>
                <span>•</span>
                <time dateTime={article.publishedAt}>
                  {article.publishedAt}
                </time>
              </div>
              <div className="grid gap-6">
                {article.sections.map((section) => (
                  <section
                    key={section.title}
                    className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
                  >
                    <h2 className="text-2xl font-semibold leading-tight">
                      {section.title}
                    </h2>
                    <div className="mt-4 grid gap-4 leading-7 text-[#171915]/70">
                      {section.paragraphs.map((paragraph) => (
                        <p key={paragraph}>{paragraph}</p>
                      ))}
                    </div>
                    {section.bullets && (
                      <ul className="mt-5 grid gap-3">
                        {section.bullets.map((bullet) => (
                          <li key={bullet} className="flex gap-3">
                            <CheckIcon className="mt-1 size-4 shrink-0 text-[#00a6a6]" />
                            <span className="leading-7 text-[#171915]/70">
                              {bullet}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>
                ))}
              </div>
              <section className="mt-8 rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6">
                <h2 className="text-2xl font-semibold">Короткие ответы</h2>
                <div className="mt-5 grid gap-3">
                  {article.faq.map((item) => (
                    <details
                      key={item.question}
                      className="group rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-5"
                    >
                      <summary className="flex cursor-pointer list-none items-center justify-between gap-4 font-semibold">
                        <span>{item.question}</span>
                        <CircleHelpIcon className="size-5 text-[#00a6a6] transition group-open:rotate-45" />
                      </summary>
                      <p className="mt-4 leading-7 text-[#171915]/68">
                        {item.answer}
                      </p>
                    </details>
                  ))}
                </div>
              </section>
            </article>
            <aside className="lg:sticky lg:top-24 lg:self-start">
              <div className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-5">
                <div className="mb-4 flex items-center gap-2 font-semibold">
                  <ClipboardListIcon className="size-5 text-[#ff6f59]" />
                  Связанные страницы
                </div>
                <div className="grid gap-2">
                  {article.related.map((link) => (
                    <Link
                      key={link.to}
                      to={link.to}
                      className="rounded-md border border-[#171915]/10 bg-[#fffdf7] px-3 py-2 text-sm font-medium text-[#171915]/74 transition hover:border-[#00a6a6]/60 hover:text-[#006d6d]"
                    >
                      {link.label}
                    </Link>
                  ))}
                  <Link
                    to="/blog"
                    className="rounded-md border border-[#171915]/10 bg-[#fffdf7] px-3 py-2 text-sm font-medium text-[#171915]/74 transition hover:border-[#00a6a6]/60 hover:text-[#006d6d]"
                  >
                    Все статьи
                  </Link>
                </div>
              </div>
            </aside>
          </div>
        </section>
        <CtaBand />
      </main>
    </MarketingLayout>
  );
}

export function SitemapPage() {
  const sections = sitemapPageSections();

  return (
    <MarketingLayout>
      <PageMeta
        title="Карта сайта demo-platform — все публичные страницы"
        description="HTML-карта сайта demo-platform: продуктовые страницы, SEO-посадочные, статьи, инструкции, документы и контакты."
        canonicalPath="/sitemap"
        structuredData={sitemapPageSchema()}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge="Карта сайта"
          title="Все публичные страницы demo-platform"
          text="Быстрая навигация по продукту, статьям, инструкциям и документам. Эта страница помогает пользователям и поисковикам увидеть структуру сайта."
        />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto grid max-w-7xl gap-5 px-4 sm:px-6 lg:grid-cols-2 lg:px-8">
            {sections.map((section) => (
              <section
                key={section.title}
                className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
              >
                <h2 className="text-2xl font-semibold">{section.title}</h2>
                <div className="mt-5 grid gap-2">
                  {section.links.map((link) => (
                    <Link
                      key={link.to}
                      to={link.to}
                      className="rounded-md border border-[#171915]/10 bg-[#fffdf7] px-4 py-3 text-sm font-medium text-[#171915]/72 transition hover:border-[#00a6a6]/50 hover:text-[#006d6d]"
                    >
                      {link.label}
                    </Link>
                  ))}
                </div>
              </section>
            ))}
          </div>
        </section>
      </main>
    </MarketingLayout>
  );
}

export function AboutPage() {
  const facts = [
    {
      icon: MessageSquareTextIcon,
      title: "Что делает demo-platform",
      text: "Помогает командам управлять Telegram-кампаниями: источники, AI-комментарии, аккаунты, прокси, прогрев, UTM-ссылки, логи и отчеты.",
    },
    {
      icon: ShieldCheckIcon,
      title: "Позиция по безопасности",
      text: "Сервис не обещает обход ограничений Telegram и не строится вокруг массового одинакового спама. Важны контекст, лимиты, паузы, модерация и прозрачные отчеты.",
    },
    {
      icon: GaugeIcon,
      title: "Как измеряется результат",
      text: "Мы смотрим не только на публикации и клики, но и на signup, workspace, добавление аккаунта, источника, кампании и последующую оплату.",
    },
  ];

  const profileLinks = [
    { to: "/telegram-comment-bot", label: "Бот для комментариев Telegram" },
    { to: "/telegram-auto-comments", label: "Автокомментарии Telegram" },
    { to: "/neurocommenting", label: "Нейрокомментинг" },
    { to: "/telegram-marketing-platform", label: "Платформа Telegram-маркетинга" },
    { to: "/blog/telegram-marketing-safety", label: "Безопасный Telegram-маркетинг" },
    { to: "/blog/telegram-comment-bot-comparison", label: "Бот или платформа" },
  ];

  return (
    <MarketingLayout>
      <PageMeta
        title="О demo-platform — AI-комментарии и Telegram-маркетинг"
        description="demo-platform — SaaS-платформа для AI-комментариев, Telegram-маркетинга, прогрева аккаунтов, источников, UTM-ссылок, логов и отчетов."
        canonicalPath="/about"
        structuredData={aboutPageSchema()}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge="О сервисе"
          title="demo-platform помогает вести Telegram-маркетинг как управляемый процесс"
          text="Эта страница собирает короткое описание продукта, принципы безопасности и ссылки, которые можно использовать в профилях, каталогах и публичных упоминаниях."
        />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto grid max-w-7xl gap-5 px-4 sm:px-6 lg:grid-cols-3 lg:px-8">
            {facts.map((item) => (
              <article
                key={item.title}
                className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
              >
                <item.icon className="mb-5 size-7 text-[#00a6a6]" />
                <h2 className="text-2xl font-semibold">{item.title}</h2>
                <p className="mt-4 leading-7 text-[#171915]/68">{item.text}</p>
              </article>
            ))}
          </div>
        </section>
        <section className="bg-[#f6f4ed] py-16">
          <div className="mx-auto grid max-w-7xl gap-8 px-4 sm:px-6 lg:grid-cols-[0.9fr_1.1fr] lg:px-8">
            <div>
              <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#00a6a6]">
                Для внешних профилей
              </div>
              <h2 className="text-3xl font-semibold leading-tight sm:text-4xl">
                Короткое описание бренда
              </h2>
              <div className="mt-5 grid gap-4 leading-7 text-[#171915]/70">
                <p>
                  demo-platform - русскоязычная SaaS-платформа для команд, которые
                  используют Telegram как канал маркетинга и хотят управлять
                  источниками, AI-комментариями, аккаунтами, лимитами и
                  аналитикой в одном кабинете.
                </p>
                <p>
                  Сервис подходит маркетологам, агентствам, продюсерам,
                  основателям и Telegram-командам, которым важны аккуратные
                  тесты, UTM-метки, журнал действий и понимание пути от
                  комментария до регистрации.
                </p>
              </div>
            </div>
            <div className="rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-6">
              <h2 className="text-2xl font-semibold">Куда ссылаться</h2>
              <p className="mt-3 leading-7 text-[#171915]/66">
                Для бесплатных профилей и каталогов лучше выбирать страницу под
                конкретный контекст, а не всегда вести только на главную.
              </p>
              <div className="mt-5 grid gap-2 sm:grid-cols-2">
                {profileLinks.map((link) => (
                  <Link
                    key={link.to}
                    to={link.to}
                    className="rounded-md border border-[#171915]/10 bg-[#f6f4ed] px-4 py-3 text-sm font-medium text-[#171915]/74 transition hover:border-[#00a6a6]/60 hover:text-[#006d6d]"
                  >
                    {link.label}
                  </Link>
                ))}
              </div>
            </div>
          </div>
        </section>
        <CtaBand />
      </main>
    </MarketingLayout>
  );
}

export function SeoLandingPage({ pageKey }: { pageKey: string }) {
  const page = seoLandingPages[pageKey] || seoLandingPages.neurocommenting;

  return (
    <MarketingLayout>
      <PageMeta
        title={`${page.title} — demo-platform`}
        description={page.description}
        canonicalPath={`/${page.slug}`}
        structuredData={seoLandingSchema(page)}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero badge={page.badge} title={page.title} text={page.intro} />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto grid max-w-7xl gap-8 px-4 sm:px-6 lg:grid-cols-[0.95fr_1.05fr] lg:px-8">
            <div>
              <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#00a6a6]">
                Ключевой запрос
              </div>
              <h2 className="text-3xl font-semibold leading-tight sm:text-4xl">
                {page.primaryKeyword}
              </h2>
              <p className="mt-4 leading-7 text-[#171915]/66">
                Эта страница собрана под поисковые запросы людей, которые ищут
                не абстрактный AI, а рабочий способ управлять Telegram-каналами,
                чатами, аккаунтами, комментариями и аналитикой.
              </p>
              <div className="mt-6 flex flex-wrap gap-2">
                {page.keywords.map((keyword) => (
                  <Badge
                    key={keyword}
                    variant="outline"
                    className="border-[#171915]/12 bg-[#f6f4ed] text-[#171915]"
                  >
                    {keyword}
                  </Badge>
                ))}
              </div>
            </div>
            <div className="grid gap-3">
              {page.pains.map((pain) => (
                <article
                  key={pain}
                  className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-5"
                >
                  <div className="mb-3 flex size-9 items-center justify-center rounded-md bg-[#171915] text-[#d7ff46]">
                    <CheckIcon className="size-4" />
                  </div>
                  <p className="leading-7 text-[#171915]/72">{pain}</p>
                </article>
              ))}
            </div>
          </div>
        </section>
        <section className="bg-[#f6f4ed] py-16">
          <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
            <SectionIntro
              eyebrow="Как работает"
              title="От хаотичных действий к управляемой Telegram-кампании"
              text="demo-platform строится вокруг аккуратной автоматизации: источники, AI-профили, лимиты, короткие ссылки, цели и отчеты соединены в один процесс."
            />
            <div className="mt-10 grid gap-4 md:grid-cols-3">
              {page.workflow.map((step, index) => (
                <article
                  key={step.title}
                  className="rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-6"
                >
                  <div className="mb-5 flex size-10 items-center justify-center rounded-md bg-[#171915] font-semibold text-[#d7ff46]">
                    {index + 1}
                  </div>
                  <h3 className="text-xl font-semibold">{step.title}</h3>
                  <p className="mt-3 text-sm leading-7 text-[#171915]/66">
                    {step.text}
                  </p>
                </article>
              ))}
            </div>
          </div>
        </section>
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto grid max-w-7xl gap-8 px-4 sm:px-6 lg:grid-cols-[0.9fr_1.1fr] lg:px-8">
            <div>
              <div className="mb-3 text-sm font-semibold uppercase tracking-[0.16em] text-[#ff6f59]">
                Безопасность
              </div>
              <h2 className="text-3xl font-semibold leading-tight sm:text-4xl">
                Автоматизация не должна превращаться в спам
              </h2>
              <p className="mt-4 leading-7 text-[#171915]/66">
                Платформа не обещает обход ограничений Telegram. Задача
                demo-platform - помочь работать аккуратнее: соблюдать паузы,
                контролировать лимиты, видеть ошибки и не публиковать одинаковые
                сообщения без контекста.
              </p>
              <div className="mt-6 flex flex-col gap-3 sm:flex-row">
                <Button
                  asChild
                  className="!bg-[#171915] !text-[#f6f4ed] hover:!bg-[#2b2f27]"
                >
                  <Link to="/signup">
                    Создать кабинет
                    <ArrowRightIcon />
                  </Link>
                </Button>
                <Button
                  asChild
                  variant="outline"
                  className="border-[#171915]/18 bg-transparent"
                >
                  <Link to="/pricing">Посмотреть тарифы</Link>
                </Button>
              </div>
            </div>
            <div className="grid gap-3">
              {page.faq.map((item) => (
                <details
                  key={item.question}
                  className="group rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-5"
                >
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-4 font-semibold">
                    <span>{item.question}</span>
                    <CircleHelpIcon className="size-5 text-[#00a6a6] transition group-open:rotate-45" />
                  </summary>
                  <p className="mt-4 leading-7 text-[#171915]/68">
                    {item.answer}
                  </p>
                </details>
              ))}
            </div>
          </div>
        </section>
        <CtaBand />
      </main>
    </MarketingLayout>
  );
}

export function FaqPage() {
  return (
    <MarketingLayout>
      <PageMeta
        title="FAQ demo-platform — вопросы о Telegram-автоматизации"
        description="Ответы о регистрации, тарифах, доступах, AI-комментариях, прогреве аккаунтов, ограничениях Telegram, оплате и личном кабинете."
        canonicalPath="/faq"
        structuredData={faqSchema()}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge="FAQ"
          title="Коротко о продукте, доступах и следующем этапе"
          text="Ответы на вопросы, которые обычно возникают перед регистрацией и подключением модулей."
        />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto max-w-4xl px-4 sm:px-6 lg:px-8">
            <div className="grid gap-3">
              {faqs.map((faq) => (
                <details
                  key={faq.question}
                  className="group rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-5"
                >
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-4 font-semibold">
                    <span>{faq.question}</span>
                    <CircleHelpIcon className="size-5 text-[#00a6a6] transition group-open:rotate-45" />
                  </summary>
                  <p className="mt-4 leading-7 text-[#171915]/68">
                    {faq.answer}
                  </p>
                </details>
              ))}
            </div>
          </div>
        </section>
      </main>
    </MarketingLayout>
  );
}

export function ContactPage() {
  React.useEffect(() => {
    const timeoutId = window.setTimeout(() => openAssistantSupport(), 0);
    return () => window.clearTimeout(timeoutId);
  }, []);

  return (
    <MarketingLayout>
      <PageMeta
        title="Связаться с demo-platform — внедрение Telegram-автоматизации"
        description="Напишите команде demo-platform по вопросам AI-комментариев, Telegram-кампаний, тарифов, оплаты, прогрева аккаунтов и внедрения."
        canonicalPath="/contact"
      />
      <main className="bg-[#f6f4ed]">
        <PageHero
          badge="Поддержка"
          title="Помощь по demo-platform"
          text="Сначала AI-помощник быстро отвечает по инструкциям и тарифам. Если вопрос требует человека, отправьте заявку в поддержку прямо из чата."
        />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto grid max-w-6xl gap-8 px-4 sm:px-6 lg:grid-cols-[0.95fr_1.05fr] lg:px-8">
            <div className="grid gap-4">
              {[
                {
                  icon: UsersRoundIcon,
                  title: "Консультация",
                  text: "Разберем ваш сценарий и подскажем, с чего лучше начать.",
                },
                {
                  icon: Layers3Icon,
                  title: "Внедрение",
                  text: "Поможем собрать рабочий контур: аккаунты, источники, кампании и лимиты.",
                },
                {
                  icon: LockKeyholeIcon,
                  title: "Оплата",
                  text: "Ответим по тарифам, доступам, счетам и подключению модулей.",
                },
              ].map((item) => (
                <article
                  key={item.title}
                  className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
                >
                  <item.icon className="mb-5 size-7 text-[#ff6f59]" />
                  <h3 className="text-xl font-semibold">{item.title}</h3>
                  <p className="mt-3 leading-7 text-[#171915]/68">
                    {item.text}
                  </p>
                </article>
              ))}
            </div>
            <div className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6">
              <div className="mb-6 flex items-center gap-3">
                <span className="flex size-10 items-center justify-center rounded-md bg-[#171915] text-[#d7ff46]">
                  <MessageSquareTextIcon className="size-5" />
                </span>
                <div>
                  <h2 className="text-2xl font-semibold">Открыть поддержку</h2>
                  <p className="text-sm text-[#171915]/62">
                    Форма поддержки откроется в чате справа внизу.
                  </p>
                </div>
              </div>
              <div className="grid gap-4 text-sm leading-6 text-[#171915]/68">
                <p>
                  Напишите вопрос в AI-помощник. Он даст инструкцию по базе
                  знаний demo-platform, а если ответа недостаточно, нажмите
                  «Передать вопрос человеку» и оставьте контакт.
                </p>
                <Button
                  type="button"
                  className="h-11 bg-[#171915] text-[#f6f4ed] hover:bg-[#2b2f27]"
                  onClick={() => openAssistantSupport()}
                >
                  Открыть поддержку
                  <ArrowRightIcon />
                </Button>
              </div>
            </div>
          </div>
        </section>
      </main>
    </MarketingLayout>
  );
}

const legalSections: Record<
  string,
  { intro: string; blocks: { title: string; text: React.ReactNode }[] }
> = {
  "Политика конфиденциальности": {
    intro:
      "Документ описывает, какие данные нужны demo-platform для регистрации, работы кабинета, платежей, поддержки и безопасности аккаунтов.",
    blocks: [
      {
        title: "Какие данные обрабатываются",
        text: "Email, имя пользователя, данные workspace, настройки Telegram-аккаунтов, источники, кампании, логи действий, платежные статусы, технические сведения браузера и сервера. Содержимое Telegram-сессий хранится только для выполнения действий, которые пользователь запускает в кабинете.",
      },
      {
        title: "Telegram-данные и сессии",
        text: "Пользователь передает Telegram-сессии, аккаунты, прокси, источники, промпты и кампании добровольно и подтверждает, что имеет право использовать эти данные. Сервис обрабатывает их только для авторизации, проверки статуса, выполнения настроенных действий, прогрева, отчетов и поддержки.",
      },
      {
        title: "Зачем это нужно",
        text: "Для авторизации, подтверждения email, выдачи доступов, запуска AI-комментариев, ведения логов, поддержки, безопасности, предотвращения злоупотреблений и исполнения оплаченного тарифа.",
      },
      {
        title: "Платежные данные",
        text: "Реквизиты карт и кошельков не хранятся в интерфейсе demo-platform. Оплата проводится через подключенного платежного провайдера, банк, СБП-сценарий или crypto invoice. В сервис сохраняются только статус, тариф, сумма, идентификатор платежа и период доступа.",
      },
      {
        title: "Передача третьим лицам",
        text: "Данные могут передаваться хостингу, платежному провайдеру, email-провайдеру, сервисам аналитики и подрядчикам поддержки только в объеме, необходимом для работы сервиса.",
      },
      {
        title: "Права пользователя",
        text: "Пользователь может запросить экспорт, исправление или удаление персональных данных, если это не противоречит требованиям учета, безопасности и исполнению договора.",
      },
      {
        title: "Безопасность и ограничения платформ",
        text: "Мы можем анализировать технические логи, статусы аккаунтов и ошибки Telegram, чтобы предотвращать злоупотребления, диагностировать FloodWait, временные лимиты и недоступность источников. Такая обработка не означает, что сервис гарантирует сохранность аккаунтов от решений Telegram или других внешних площадок.",
      },
    ],
  },
  Оферта: {
    intro:
      "Публичная оферта описывает условия доступа к SaaS-платформе demo-platform, платным модулям, лимитам и правилам использования.",
    blocks: [
      {
        title: "Предмет",
        text: "Исполнитель предоставляет доступ к веб-платформе demo-platform: кабинет, Telegram-аккаунты, прокси, источники, AI-комментарии, отчеты и иные модули по мере их готовности.",
      },
      {
        title: "Доступ и тарифы",
        text: "Базовый кабинет может быть бесплатным. Платный доступ сейчас продается к AI-комментариям, рассылкам сообщений, прогреву и готовым рабочим разделам. AI-чат, AI диалоги, парсеры и AI реакции могут отображаться как будущие модули, но публично доступны только после запуска.",
      },
      {
        title: "Ответственность пользователя",
        text: "Пользователь самостоятельно отвечает за законность использования своих Telegram-аккаунтов, источников, прокси, промптов, комментариев и сообщений, а также за соблюдение правил Telegram и законодательства своей юрисдикции.",
      },
      {
        title: "Блокировки, баны и лимиты",
        text: "Исполнитель не гарантирует отсутствие ограничений, банов, FloodWait, жалоб, отклонения сообщений, блокировки аккаунтов, отключения Telegram-сессий или недоступности отдельных источников. Такие решения принимает Telegram или администраторы чатов. Платформа предоставляет инструменты лимитов, логов, прогрева и безопасных настроек, но итоговый риск использования аккаунтов остается на пользователе.",
      },
      {
        title: "Нет обхода ограничений",
        text: (
          <>
            demo-platform не продает и не обещает обход блокировок, лимитов,
            антиспам-систем, FloodWait, модерации Telegram или правил сторонних
            площадок. Сервис предназначен для автоматизации действий, которые
            пользователь вправе выполнять сам, и должен использоваться с учетом{" "}
            <a
              className={legalLinkClass}
              href="https://telegram.org/tos"
              rel="noreferrer"
              target="_blank"
            >
              условий Telegram
            </a>
            ,{" "}
            <a
              className={legalLinkClass}
              href="https://telegram.org/faq_spam"
              rel="noreferrer"
              target="_blank"
            >
              Spam FAQ
            </a>{" "}
            и{" "}
            <a
              className={legalLinkClass}
              href="https://core.telegram.org/api/terms"
              rel="noreferrer"
              target="_blank"
            >
              правил Telegram API
            </a>
            .
          </>
        ),
      },
      {
        title: "Осторожное использование",
        text: "Пользователь обязан самостоятельно выбирать частоту действий, объемы кампаний, тексты, источники, ссылки, прокси и аккаунты так, чтобы не создавать спам, не вводить людей в заблуждение, не нарушать правила чатов и не провоцировать жалобы. Рекомендации сервиса по прогреву, паузам и лимитам не являются гарантией результата.",
      },
      {
        title: "Оплата",
        text: "Оплата тарифа подтверждает акцепт оферты. Доступ выдается на оплаченный период после получения подтверждения платежа от провайдера или ручного подтверждения оплаты.",
      },
    ],
  },
  "Пользовательское соглашение": {
    intro:
      "Соглашение фиксирует правила работы в кабинете demo-platform и допустимое использование модулей автоматизации.",
    blocks: [
      {
        title: "Аккаунт и workspace",
        text: "Пользователь регистрируется по email или через подключенный OAuth-провайдер. Каждый workspace изолирует аккаунты, источники, кампании, отчеты и доступы от других пользователей.",
      },
      {
        title: "Правила использования",
        text: "Запрещено использовать сервис для спама, мошенничества, массового нарушения правил площадок, публикации незаконного контента, обхода ограничений и действий без законного основания.",
      },
      {
        title: "Telegram и антиспам",
        text: (
          <>
            При работе с Telegram пользователь обязан соблюдать правила самой
            площадки: не отправлять нежелательные сообщения, не публиковать спам
            в группах, не добавлять людей в нежелательные группы и каналы, не
            использовать сервис для скама или незаконных действий. Telegram
            прямо указывает, что нежелательные коммерческие сообщения, ссылки,
            приглашения и повторные нарушения могут привести к ограничениям или
            потере возможности писать незнакомым пользователям. Подробнее:{" "}
            <a
              className={legalLinkClass}
              href="https://telegram.org/faq_spam"
              rel="noreferrer"
              target="_blank"
            >
              Telegram Spam FAQ
            </a>
            .
          </>
        ),
      },
      {
        title: "AI-контент",
        text: "AI-комментарии и сообщения являются черновиками или автоматическими публикациями по настройкам пользователя. Пользователь отвечает за промпты, тон, источники и решение отправлять результат автоматически.",
      },
      {
        title: "Прогрев и лимиты",
        text: "Перед активными кампаниями пользователь должен проверять аккаунты, постепенно повышать активность, соблюдать паузы между действиями, не использовать одинаковые тексты и ссылки массово, учитывать возраст аккаунта, историю номера, качество прокси и правила конкретных чатов. Если Telegram вернул FloodWait, ошибку, ограничение или предупреждение, пользователь обязан остановить или снизить активность.",
      },
      {
        title: "Действия администратора",
        text: "Мы можем ограничить доступ к отдельным функциям, приостановить кампанию или заблокировать workspace, если видим признаки спама, скама, обхода ограничений, массовых жалоб, незаконного контента или угрозу инфраструктуре сервиса.",
      },
      {
        title: "Безопасность",
        text: "Пользователь обязан хранить доступ к кабинету, Telegram-сессиям, прокси и платежным данным безопасно и не передавать их третьим лицам без необходимости.",
      },
    ],
  },
  "Оплата и возвраты": {
    intro:
      "Страница описывает планируемую схему оплаты тарифов, подключение модулей, возвраты и платежные каналы.",
    blocks: [
      {
        title: "Способы оплаты",
        text: "Основные варианты: СБП или счет для рублевых платежей, банковская карта через платежного провайдера, crypto invoice через подключенный крипто-провайдер. Конкретный способ показывается на checkout-странице тарифа.",
      },
      {
        title: "Комиссии",
        text: "Комиссии зависят от банка, платежного провайдера, оборота и способа оплаты. demo-platform стремится использовать каналы с низкой комиссией, но не обещает обход налогов или обязательных платежей.",
      },
      {
        title: "Выдача доступа",
        text: "После успешного платежа workspace получает доступ к оплаченному модулю и лимитам. Если платеж требует ручной сверки, доступ выдается после подтверждения поступления.",
      },
      {
        title: "Возвраты",
        text: "Возврат рассматривается по обращению пользователя, если доступ не был использован или услуга не была предоставлена по технической причине на стороне сервиса. Ограничения, FloodWait, жалобы, блокировки, баны Telegram-аккаунтов, недоступность источников или нарушение правил площадки пользователем сами по себе не являются основанием для возврата. Частичный возврат по активному периоду согласуется индивидуально.",
      },
    ],
  },
};

const legalSlugByTitle: Record<string, string> = {
  "Политика конфиденциальности": "privacy",
  Оферта: "offer",
  "Пользовательское соглашение": "terms",
  "Оплата и возвраты": "payment",
};

export function LegalPage({ title }: { title: string }) {
  const page =
    legalSections[title] || legalSections["Пользовательское соглашение"];

  return (
    <MarketingLayout>
      <PageMeta
        title={`${title} demo-platform`}
        description={`${title} demo-platform: условия платформы, оплаты, обработки данных, Telegram-аккаунтов и безопасного использования сервиса.`}
        canonicalPath={`/legal/${legalSlugByTitle[title] || "terms"}`}
      />
      <main className="bg-[#f6f4ed]">
        <PageHero badge="Документы" title={title} text={page.intro} />
        <section className="border-y border-[#171915]/10 bg-[#fffdf7] py-16">
          <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
            <div className="mb-8 flex items-start gap-4 rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-5">
              <FileTextIcon className="mt-1 size-6 shrink-0 text-[#00a6a6]" />
              <div>
                <h2 className="text-xl font-semibold">
                  Редакция от 28.05.2026
                </h2>
                <p className="mt-2 text-sm leading-6 text-[#171915]/66">
                  Документ подготовлен для публичного запуска сервиса. Реквизиты
                  исполнителя и платежного провайдера указываются в счете,
                  checkout или инвойсе перед оплатой.
                </p>
              </div>
            </div>
            <div className="grid gap-4">
              {page.blocks.map((block) => (
                <article
                  key={block.title}
                  className="rounded-lg border border-[#171915]/10 bg-[#f6f4ed] p-6"
                >
                  <h2 className="text-2xl font-semibold">{block.title}</h2>
                  <p className="mt-3 leading-7 text-[#171915]/68">
                    {block.text}
                  </p>
                </article>
              ))}
            </div>
            <div className="mt-8 rounded-lg border border-[#171915]/10 bg-[#171915] p-6 text-[#f6f4ed]">
              <ReceiptTextIcon className="mb-4 size-7 text-[#d7ff46]" />
              <h2 className="text-2xl font-semibold">Контакт по документам</h2>
              <p className="mt-3 leading-7 text-[#f6f4ed]/72">
                Вопросы по доступам, платежам, данным и документам можно
                отправить через страницу контакта или на рабочий email,
                указанный в счете/инвойсе.
              </p>
            </div>
          </div>
        </section>
      </main>
    </MarketingLayout>
  );
}

function PageHero({
  badge,
  title,
  text,
}: {
  badge: string;
  title: string;
  text: string;
}) {
  return (
    <section className="relative overflow-hidden bg-[#171915] py-24 text-[#f6f4ed]">
      <div className="absolute inset-x-0 bottom-0 h-2 bg-[linear-gradient(90deg,#d7ff46,#00a6a6,#ff6f59)]" />
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <Badge className="mb-5 bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]">
          {badge}
        </Badge>
        <h1 className="max-w-4xl text-4xl font-semibold leading-tight text-balance sm:text-6xl">
          {title}
        </h1>
        <p className="mt-5 max-w-2xl text-lg leading-8 text-[#f6f4ed]/72">
          {text}
        </p>
        <div className="mt-7 flex flex-col gap-3 sm:flex-row">
          <Button
            asChild
            className="!bg-[#d7ff46] !text-[#171915] hover:!bg-[#c5ef31] hover:!text-[#171915]"
          >
            <Link to="/signup">
              Попробовать 7 дней бесплатно
              <ArrowRightIcon />
            </Link>
          </Button>
          <Button
            asChild
            variant="outline"
            className="border-[#f6f4ed]/24 bg-transparent text-[#f6f4ed] hover:bg-[#f6f4ed]/10 hover:text-[#f6f4ed]"
          >
            <Link to="/pricing">Посмотреть тарифы</Link>
          </Button>
        </div>
      </div>
    </section>
  );
}

export function NotFoundPage() {
  return (
    <MarketingLayout>
      <PageMeta
        title="Страница не найдена — demo-platform"
        description="Страница demo-platform не найдена."
        noIndex
      />
      <main className="grid min-h-[70svh] place-items-center bg-[#f6f4ed] px-4 text-center">
        <div>
          <SparklesIcon className="mx-auto mb-5 size-10 text-[#00a6a6]" />
          <h1 className="text-4xl font-semibold">Страница не найдена</h1>
          <p className="mt-3 text-[#171915]/66">
            Вернитесь на главную или войдите в кабинет.
          </p>
          <div className="mt-7 flex justify-center gap-3">
            <Button asChild variant="outline">
              <Link to="/">Главная</Link>
            </Button>
            <Button asChild>
              <Link to="/login">Войти</Link>
            </Button>
          </div>
        </div>
      </main>
    </MarketingLayout>
  );
}
