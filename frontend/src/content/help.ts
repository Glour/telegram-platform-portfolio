export type HelpDifficulty = "start" | "basic" | "advanced";

export type HelpCategory =
  | "start"
  | "accounts"
  | "campaigns"
  | "safety"
  | "content"
  | "billing";

export type HelpScreenshot = {
  id: string;
  src: string;
  alt: string;
  caption: string;
  viewport: "desktop-1920x1080";
};

export type HelpSection = {
  id: string;
  title: string;
  body: string[];
  steps?: string[];
  warning?: string;
};

export type WarmupPreparationStage = {
  id: string;
  label: string;
  title: string;
  description: string;
  details: string[];
  riskNote?: string;
};

export type HelpArticle = {
  slug: string;
  title: string;
  description: string;
  moduleId?: string;
  category: HelpCategory;
  difficulty: HelpDifficulty;
  estimatedTimeMinutes: number;
  prerequisites: string[];
  outcome: string;
  screenshots: HelpScreenshot[];
  sections: HelpSection[];
  checklist: string[];
  related: string[];
  preparationFlow?: WarmupPreparationStage[];
  faq?: { question: string; answer: string }[];
};

export const helpCategoryLabels: Record<HelpCategory, string> = {
  start: "Начать",
  accounts: "Аккаунты",
  campaigns: "Кампании",
  safety: "Безопасность",
  content: "Контент",
  billing: "Тарифы",
};

const screenshotLibrary: Record<string, HelpScreenshot> = {
  dashboard: {
    id: "dashboard",
    src: "/help/screenshots/01-dashboard-overview.png",
    alt: "Обзор кабинета demo-platform с демо-данными",
    caption: "Общий кабинет: аккаунты, источники, кампании и события.",
    viewport: "desktop-1920x1080",
  },
  accounts: {
    id: "accounts",
    src: "/help/screenshots/02-accounts-list.png",
    alt: "Список Telegram-аккаунтов и статусы",
    caption: "Аккаунты с health score, прокси и статусами ограничений.",
    viewport: "desktop-1920x1080",
  },
  proxies: {
    id: "proxies",
    src: "/help/screenshots/03-proxies.png",
    alt: "Прокси с проверкой Telegram connectivity",
    caption: "Прокси с маскированными адресами и результатами проверки.",
    viewport: "desktop-1920x1080",
  },
  sources: {
    id: "sources",
    src: "/help/screenshots/04-sources.png",
    alt: "Источники Telegram и проверка комментариев",
    caption: "Источники: каналы, группы и диалоги с результатами проверки.",
    viewport: "desktop-1920x1080",
  },
  comments: {
    id: "comments",
    src: "/help/screenshots/05-ai-comments.png",
    alt: "Кампания AI-комментариев в режиме черновиков",
    caption: "AI-комментарии: источники, аккаунты, prompt и безопасный режим.",
    viewport: "desktop-1920x1080",
  },
  warmup: {
    id: "warmup",
    src: "/help/screenshots/06-warmup.png",
    alt: "Прогрев аккаунтов и step logs",
    caption: "Прогрев: пресет, расписание, stop-loss и журналы шагов.",
    viewport: "desktop-1920x1080",
  },
  parsing: {
    id: "parsing",
    src: "/help/screenshots/07-parsing.png",
    alt: "Парсинг Telegram-данных",
    caption: "Парсинг: выбор аккаунта, источника, лимита и окно результатов.",
    viewport: "desktop-1920x1080",
  },
  contentFactory: {
    id: "content-factory",
    src: "/help/screenshots/08-content-factory.png",
    alt: "Контент-завод с черновиками и базой знаний",
    caption: "Контент-завод: целевой канал, источники, база знаний и черновик.",
    viewport: "desktop-1920x1080",
  },
  safety: {
    id: "safety",
    src: "/help/screenshots/09-safety.png",
    alt: "Safety preflight и риски запуска",
    caption: "Safety preflight: риски аккаунтов, источников и лимитов перед запуском.",
    viewport: "desktop-1920x1080",
  },
  billing: {
    id: "billing",
    src: "/help/screenshots/10-billing.png",
    alt: "Тарифы, модули и доступы",
    caption: "Доступы: активный план, отдельные модули и locked-состояния.",
    viewport: "desktop-1920x1080",
  },
};

const safeStart =
  "Безопасный старт: начните с черновиков, проверьте аккаунт и прокси, поставьте малые лимиты, не запускайте свежий аккаунт в тяжелые сценарии и остановите кампанию при FloodWait, limited или ошибках доступа.";

function sectionSet(moduleName: string, steps: string[], warning?: string): HelpSection[] {
  return [
    {
      id: "what",
      title: "Что делает модуль",
      body: [
        `${moduleName} помогает выполнить конкретную операционную задачу в Telegram без хаоса: подготовить данные, проверить ограничения, запустить малый тест и увидеть результат в логах.`,
        "Раздел написан как рабочая инструкция: откройте кабинет и повторяйте шаги по порядку.",
      ],
    },
    {
      id: "when",
      title: "Когда использовать",
      body: [
        "Используйте модуль, когда уже понятна цель запуска и есть подготовленные аккаунты, прокси и источники. Для первого раза выбирайте минимальные лимиты и режим черновиков, если он доступен.",
      ],
    },
    {
      id: "prepare",
      title: "Что подготовить",
      body: [
        "Проверьте, что аккаунт не limited и не blocked, прокси отвечает, источники доступны, а текст или prompt не выглядит как массовый спам.",
      ],
    },
    {
      id: "steps",
      title: "Пошаговый запуск",
      body: ["Двигайтесь сверху вниз. Не пропускайте проверку доступа и preflight."],
      steps,
    },
    {
      id: "check",
      title: "Как проверить, что все работает",
      body: [
        "После запуска откройте логи, статусы и отчет. Нормальная первая проверка - это маленький тест без резкого роста действий.",
      ],
    },
    {
      id: "errors",
      title: "Частые ошибки",
      body: [
        "Чаще всего запуск ломается из-за невалидной сессии, недоступного источника, слабого прокси, слишком больших лимитов или попытки сразу отправлять вместо подготовки черновиков.",
      ],
    },
    {
      id: "safe",
      title: "Безопасные настройки для старта",
      body: [
        "Для нового сценария ставьте дневной лимит ниже рабочего, добавляйте задержки, проверяйте источники и не смешивайте свежие аккаунты с агрессивными действиями.",
      ],
      warning: warning || safeStart,
    },
    {
      id: "next",
      title: "Что делать дальше",
      body: [
        "Если тест прошел спокойно, увеличивайте объем постепенно. Если появились FloodWait, limited, blocked или ошибки доступа, остановите run и разберите причину.",
      ],
    },
  ];
}

function article(input: HelpArticle): HelpArticle {
  return input;
}

export const helpArticles: HelpArticle[] = [
  article({
    slug: "getting-started",
    title: "Первый безопасный запуск в demo-platform",
    description:
      "Короткий путь от регистрации до первого безопасного теста: аккаунт, прокси, источник, прогрев, черновик и отчет.",
    category: "start",
    difficulty: "start",
    estimatedTimeMinutes: 12,
    prerequisites: ["Доступ к кабинету", "Один тестовый Telegram-аккаунт", "Один рабочий прокси", "1-2 безопасных источника"],
    outcome: "Кабинет готов к первому мягкому запуску без резкого роста действий.",
    screenshots: [screenshotLibrary.dashboard, screenshotLibrary.accounts, screenshotLibrary.proxies, screenshotLibrary.sources, screenshotLibrary.comments],
    sections: sectionSet("Быстрый старт", [
      "Зарегистрируйтесь или войдите в кабинет.",
      "Добавьте прокси и проверьте Telegram connectivity.",
      "Добавьте Telegram-аккаунт и назначьте ему прокси.",
      "Запустите проверку аккаунта и дождитесь статуса.",
      "Добавьте источник и проверьте доступ к комментариям.",
      "Запустите мягкий прогрев или создайте AI-комментарии в режиме черновиков.",
      "Откройте логи и отчет, проверьте ошибки и рекомендации.",
    ]),
    checklist: ["Прокси проверен", "Аккаунт активен", "Источник доступен", "Комментарии проверены", "Первый запуск идет через черновики или малый лимит"],
    related: ["accounts", "proxies", "sources", "ai-comments", "warmup", "safety"],
    faq: [
      { question: "Можно сразу отправлять комментарии?", answer: "Для первого запуска лучше использовать черновики и маленький лимит." },
      { question: "Что считать нормальным первым результатом?", answer: "Отсутствие критических ошибок, понятные логи и несколько корректных черновиков." },
    ],
  }),
  article({
    slug: "accounts",
    title: "Как добавить Telegram-аккаунт",
    description:
      "Что такое аккаунт в demo-platform, как импортировать сессию, назначить прокси и проверить ограничения.",
    moduleId: "accounts",
    category: "accounts",
    difficulty: "basic",
    estimatedTimeMinutes: 8,
    prerequisites: ["Рабочий Telegram-аккаунт", "Прокси для этого аккаунта", "Понимание, откуда взята session/tdata"],
    outcome: "Аккаунт добавлен, привязан к прокси и готов к проверке перед модулями.",
    screenshots: [screenshotLibrary.accounts, screenshotLibrary.proxies, screenshotLibrary.safety],
    sections: sectionSet("Менеджер аккаунтов", [
      "Откройте раздел Аккаунты.",
      "Нажмите Добавить аккаунт.",
      "Выберите способ импорта: session, tdata или номер, если он поддержан в кабинете.",
      "Заполните данные без лишних пробелов и сохраните.",
      "Назначьте аккаунту прокси.",
      "Нажмите Проверить и дождитесь статуса.",
      "Не используйте аккаунт в тяжелых сценариях, если он new, limited, blocked или quarantined.",
    ]),
    checklist: ["Аккаунт виден в списке", "Прокси назначен", "Runtime-статус без ограничений", "Health score не критический", "Нет статуса blocked или limited"],
    related: ["proxies", "safety", "warmup", "ai-comments"],
  }),
  article({
    slug: "proxies",
    title: "Как настроить прокси",
    description:
      "Зачем нужны прокси, как импортировать SOCKS5/HTTP, проверить соединение и не перегрузить один IP.",
    moduleId: "proxies",
    category: "accounts",
    difficulty: "basic",
    estimatedTimeMinutes: 7,
    prerequisites: ["Список прокси", "Тип прокси: SOCKS5 или HTTP", "Логин и пароль, если они есть"],
    outcome: "Прокси добавлены, проверены и готовы к назначению аккаунтам.",
    screenshots: [screenshotLibrary.proxies, screenshotLibrary.accounts],
    sections: sectionSet("Прокси", [
      "Откройте Прокси.",
      "Нажмите Добавить.",
      "Вставьте один или несколько прокси.",
      "Выберите тип SOCKS5 или HTTP.",
      "Запустите проверку Telegram connectivity.",
      "Назначьте прокси аккаунту.",
      "Проверьте, что один прокси не перегружен количеством аккаунтов.",
    ]),
    checklist: ["Формат прокси корректный", "Проверка успешна", "IP замаскирован в интерфейсе", "На прокси нет перегруза", "Аккаунт привязан к своему прокси"],
    related: ["accounts", "safety", "getting-started"],
  }),
  article({
    slug: "sources",
    title: "Как добавить источники",
    description:
      "Что такое источник, как добавлять каналы, группы и диалоги, проверять доступ и комментарии.",
    moduleId: "sources",
    category: "accounts",
    difficulty: "basic",
    estimatedTimeMinutes: 8,
    prerequisites: ["Ссылки или usernames источников", "Проверенный аккаунт", "Понимание типа: канал, группа или диалог"],
    outcome: "Источники проверены и готовы для прогрева, AI-комментариев или парсинга.",
    screenshots: [screenshotLibrary.sources, screenshotLibrary.safety],
    sections: sectionSet("Источники", [
      "Откройте Источники.",
      "Нажмите Добавить.",
      "Вставьте username или ссылку.",
      "Выберите тип источника.",
      "Запустите проверку доступа.",
      "Проверьте, открыты ли комментарии.",
      "Архивируйте invalid, private и join request источники, если доступ не нужен.",
    ]),
    checklist: ["Источник доступен", "Тип указан верно", "Комментарии проверены", "Private/join request разобраны", "Источник не конфликтует с целью кампании"],
    related: ["channel-base", "parsing", "ai-comments", "warmup"],
  }),
  article({
    slug: "channel-base",
    title: "Как пользоваться базой каналов",
    description:
      "Как искать каналы по тематике, добавлять их в источники и затем проверять доступ.",
    moduleId: "channel-base",
    category: "content",
    difficulty: "basic",
    estimatedTimeMinutes: 6,
    prerequisites: ["Понимание тематики", "Проверенный аккаунт для проверки источников"],
    outcome: "Найдены релевантные каналы, перенесены в источники и готовы к проверке.",
    screenshots: [screenshotLibrary.sources, screenshotLibrary.parsing],
    sections: sectionSet("База каналов", [
      "Откройте База каналов.",
      "Выберите категорию или введите запрос.",
      "Откройте карточку канала.",
      "Добавьте канал в источники.",
      "Перейдите в Источники и запустите проверку доступа.",
    ], "База каналов - это справочник. Перед кампанией все равно проверяйте доступ и комментарии через Telegram-аккаунт."),
    checklist: ["Тематика выбрана", "Канал добавлен в источники", "Доступ проверен", "Комментарии проверены", "Нерелевантные каналы исключены"],
    related: ["sources", "parsing", "ai-comments"],
  }),
  article({
    slug: "parsing",
    title: "Как запустить парсинг",
    description:
      "Как парсить каналы, группы, пользователей, комментарии или посты без смешивания парсинга с рассылкой.",
    moduleId: "parsing",
    category: "content",
    difficulty: "basic",
    estimatedTimeMinutes: 9,
    prerequisites: ["Проверенный аккаунт", "Источник или список источников", "Лимит задачи"],
    outcome: "Результаты парсинга сохранены, доступны для экспорта или дальнейшей проверки.",
    screenshots: [screenshotLibrary.parsing, screenshotLibrary.sources],
    sections: sectionSet("Парсинг", [
      "Откройте Парсинг.",
      "Выберите аккаунт.",
      "Выберите тип парсинга.",
      "Укажите источник, usernames или лимиты.",
      "Запустите задачу.",
      "Посмотрите найденные сущности в фиксированном окне результатов.",
      "Сохраните в источники, аудитории или экспортируйте.",
    ], "Парсинг не равен рассылке. Не используйте найденных пользователей для холодного спама."),
    checklist: ["Аккаунт выбран", "Тип парсинга понятен", "Лимит задан", "Результаты не пустые", "Экспорт или сохранение выполнены"],
    related: ["sources", "channel-base", "message-broadcasts"],
  }),
  article({
    slug: "warmup",
    title: "Как запустить прогрев аккаунтов",
    description:
      "Как выбрать пресет, аккаунты, источники, расписание и читать warmup score без ложного ощущения полной безопасности.",
    moduleId: "warmup",
    category: "safety",
    difficulty: "basic",
    estimatedTimeMinutes: 10,
    prerequisites: ["Аккаунты с прокси", "Безопасные источники", "Понимание лимитов"],
    outcome: "Запущен мягкий прогрев с понятными шагами, логами и stop-loss.",
    screenshots: [screenshotLibrary.warmup, screenshotLibrary.accounts, screenshotLibrary.safety],
    preparationFlow: [
      {
        id: "intake",
        label: "01",
        title: "Входной фильтр",
        description:
          "Не начинайте с аккаунтов без истории. Чем свежее аккаунт, тем мягче должен быть первый сценарий.",
        details: [
          "Лучше брать аккаунты с возрастом и нормальной историей входов.",
          "Нулевые аккаунты не отправляйте сразу в комментарии, реакции или массовые join.",
        ],
      },
      {
        id: "landing",
        label: "02",
        title: "Тихая посадка",
        description:
          "Добавьте аккаунт в кабинет, закрепите за ним прокси и дайте системе спокойно увидеть новую связку.",
        details: [
          "После добавления оставьте паузу 12-24 часа без публичных действий.",
          "Прокси должен быть постоянным для этого аккаунта, без прыжков между странами.",
        ],
      },
      {
        id: "soft-activity",
        label: "03",
        title: "Мягкая активность",
        description:
          "Первый прогрев - это короткие сессии чтения и просмотра, а не резкий набор действий.",
        details: [
          "Начните с нескольких действий в час и делайте длинные паузы между волнами.",
          "Если появились FloodWait или ограничения, остановите run и разберите причину.",
        ],
      },
      {
        id: "dry-run",
        label: "04",
        title: "Холостой контур",
        description:
          "Прогоните будущую схему без публичных действий: проверьте источники, доступы и закрепление аккаунтов.",
        details: [
          "Зафиксируйте, какие аккаунты работают с какими источниками.",
          "После dry-run оставьте паузу и только потом переносите схему в рабочий модуль.",
        ],
      },
      {
        id: "profile",
        label: "05",
        title: "Живой профиль",
        description:
          "Оформление должно выглядеть естественно: имя, аватар, краткое описание и небольшая история активности.",
        details: [
          "Не меняйте все поля подряд за одну минуту.",
          "Ссылка в bio - заметный сигнал. Stories и аккуратная история выглядят мягче.",
        ],
        riskNote: "Профиль без контекста хуже переносит первые публичные действия.",
      },
      {
        id: "rest-after-profile",
        label: "06",
        title: "Пауза после оформления",
        description:
          "После изменений в профиле аккаунту нужна отлежка, чтобы не смешивать оформление и рабочую нагрузку.",
        details: [
          "Дайте несколько часов паузы после аватара, bio или других правок.",
          "Можно мягко пройти свои каналы чтением и просмотрами, без массовых реакций.",
        ],
      },
      {
        id: "pilot",
        label: "07",
        title: "Пилотная группа",
        description:
          "Запускайте не весь пул сразу. Сначала проверьте малую группу аккаунтов на той же логике.",
        details: [
          "Смотрите логи, ошибки доступа, FloodWait, limited и скорость выполнения.",
          "Если пилот чистый, расширяйте объем постепенно, а не одним скачком.",
        ],
      },
      {
        id: "workload",
        label: "08",
        title: "Рабочий режим",
        description:
          "Основной запуск делайте на уже знакомой связке: те же прокси, похожие источники и понятный темп.",
        details: [
          "Не меняйте одновременно аккаунты, источники, прокси и лимиты.",
          "Повышайте нагрузку только после спокойного пилота и прозрачных логов.",
        ],
      },
    ],
    sections: sectionSet("Прогрев аккаунтов", [
      "Откройте Прогрев.",
      "Выберите безопасный preset.",
      "Выберите аккаунты.",
      "Выберите проверенные источники.",
      "Настройте расписание и дневные лимиты.",
      "Запустите прогрев.",
      "Смотрите step logs, ошибки и score.",
      "Остановите run при risk-сигналах.",
    ], "Прогрев снижает резкость поведения, но не гарантирует отсутствие ограничений Telegram."),
    checklist: ["Preset выбран", "Свежие аккаунты не перегружены", "Источники проверены", "Stop-loss включен", "Логи читаются"],
    related: ["accounts", "proxies", "safety", "ai-comments"],
  }),
  article({
    slug: "ai-comments",
    title: "Как запустить AI-комментарии",
    description:
      "Как создать кампанию AI-комментариев, выбрать источники, аккаунты, prompt и начать с черновиков.",
    moduleId: "ai-comments",
    category: "campaigns",
    difficulty: "basic",
    estimatedTimeMinutes: 12,
    prerequisites: ["Проверенные аккаунты", "Прокси", "Источники с комментариями", "Prompt и тон"],
    outcome: "Кампания создана, preflight пройден, первые комментарии сформированы безопасно.",
    screenshots: [screenshotLibrary.comments, screenshotLibrary.sources, screenshotLibrary.safety],
    sections: sectionSet("AI-комментарии", [
      "Подготовьте аккаунт, прокси и источники.",
      "Откройте AI-комментарии.",
      "Нажмите Создать кампанию.",
      "Выберите источники.",
      "Выберите аккаунты.",
      "Задайте prompt, tone и ограничения длины.",
      "Выберите режим Черновики.",
      "Запустите dry-run или preflight.",
      "Запустите кампанию.",
      "Проверьте черновики и отчет.",
    ]),
    checklist: ["Источники с комментариями", "Аккаунты активны", "Prompt не спамный", "Режим черновиков включен для первого теста", "Отчет просмотрен"],
    related: ["getting-started", "sources", "safety", "warmup"],
    faq: [
      { question: "Чем draft_only отличается от отправки?", answer: "draft_only только готовит текст и дает проверить качество до публикации." },
    ],
  }),
  article({
    slug: "message-broadcasts",
    title: "Как настроить рассылки сообщений",
    description:
      "Как работать с диалогами, лимитами, задержками и малым тестом перед отправкой сообщений.",
    moduleId: "message-broadcasts",
    category: "campaigns",
    difficulty: "advanced",
    estimatedTimeMinutes: 12,
    prerequisites: ["Источники типа dialog", "Проверенный аккаунт", "Готовый текст", "Лимиты и задержки"],
    outcome: "Рассылка подготовлена и протестирована на малом объеме.",
    screenshots: [screenshotLibrary.parsing, screenshotLibrary.safety],
    sections: sectionSet("Рассылки сообщений", [
      "Добавьте или спарсите диалоги.",
      "Проверьте аккаунт.",
      "Откройте Рассылки.",
      "Выберите диалоги.",
      "Напишите текст.",
      "Установите лимиты и задержки.",
      "Запустите малый тест.",
      "Проверьте результаты.",
    ]),
    checklist: ["Диалоги подготовлены", "Текст не выглядит массовым спамом", "Задержки включены", "Daily cap малый", "Ошибки обработаны"],
    related: ["parsing", "accounts", "safety"],
  }),
  article({
    slug: "ai-chat",
    title: "Как включить AI-чат",
    description:
      "Как отвечать в группах релевантно, с min quality score, reply_to_only и лимитами.",
    moduleId: "ai-chat",
    category: "campaigns",
    difficulty: "advanced",
    estimatedTimeMinutes: 14,
    prerequisites: ["Группа как источник", "Проверенный доступ", "Prompt роли", "Лимиты ответов"],
    outcome: "AI-чат запущен в контролируемом режиме черновиков или точечных ответов.",
    screenshots: [screenshotLibrary.comments, screenshotLibrary.safety],
    sections: sectionSet("AI-чат", [
      "Добавьте группу как источник.",
      "Проверьте доступ.",
      "Откройте AI-чат.",
      "Выберите группу и аккаунты.",
      "Напишите prompt.",
      "Выберите draft_only для первого запуска.",
      "Настройте лимиты и min quality score.",
      "Запустите и смотрите skipped/replies.",
    ]),
    checklist: ["Группа доступна", "reply_to_only включен для старта", "Quality score задан", "Лимиты малы", "Skipped/replies просмотрены"],
    related: ["sources", "safety", "ai-comments"],
  }),
  article({
    slug: "ai-dialogs",
    title: "Как настроить AI-диалоги",
    description:
      "Как отвечать на личные диалоги, не смешивая модуль с холодной рассылкой.",
    moduleId: "ai-dialogs",
    category: "campaigns",
    difficulty: "advanced",
    estimatedTimeMinutes: 14,
    prerequisites: ["Источники типа dialog", "Сценарий ответа", "Проверенные аккаунты"],
    outcome: "AI-диалоги отвечают на входящие в режиме черновиков или ручного контроля.",
    screenshots: [screenshotLibrary.parsing, screenshotLibrary.comments],
    sections: sectionSet("AI-диалоги", [
      "Добавьте личные диалоги в источники.",
      "Проверьте, что источник типа dialog.",
      "Откройте AI диалоги.",
      "Выберите аккаунты и диалоги.",
      "Напишите сценарий.",
      "Включите only_reply_to_incoming.",
      "Запустите в режиме черновиков.",
      "Проверьте ответы.",
    ]),
    checklist: ["Диалоги проверены", "only_reply_to_incoming включен", "Tone задан", "Ответы просмотрены", "Нет холодного старта"],
    related: ["message-broadcasts", "safety", "accounts"],
  }),
  article({
    slug: "ai-reactions",
    title: "Как запустить AI-реакции",
    description:
      "Как выбрать реакции, probability, delays и cooldown без превращения модуля в накрутку.",
    moduleId: "ai-reactions",
    category: "campaigns",
    difficulty: "advanced",
    estimatedTimeMinutes: 10,
    prerequisites: ["Проверенные источники", "Проверенные аккаунты", "Малые лимиты"],
    outcome: "Малый тест реакций запущен с задержками и понятным отчетом.",
    screenshots: [screenshotLibrary.safety, screenshotLibrary.comments],
    sections: sectionSet("AI-реакции", [
      "Подготовьте каналы или группы.",
      "Проверьте аккаунты.",
      "Откройте AI реакции.",
      "Выберите источники.",
      "Выберите аккаунты.",
      "Выберите реакции.",
      "Настройте probability, delays и max reactions.",
      "Запустите малый тест.",
      "Проверьте отчет и cooldown.",
    ], "Реакции - attention-модуль, а не накрутка. Остановите запуск при ReactionTooMany или FloodWait."),
    checklist: ["Probability умеренная", "Delays включены", "Max reactions малый", "Cooldown понятен", "Ошибки Telegram не игнорируются"],
    related: ["safety", "sources", "warmup"],
  }),
  article({
    slug: "content-factory",
    title: "Как создать контент-завод",
    description:
      "Как настроить целевой канал, источники, базу знаний, изображения, черновики и публикацию.",
    moduleId: "content-factory",
    category: "content",
    difficulty: "basic",
    estimatedTimeMinutes: 15,
    prerequisites: ["Целевой канал", "Аккаунт-публикатор", "Источники", "База знаний или правила тона"],
    outcome: "Контент-завод создает черновики, которые можно проверить и опубликовать.",
    screenshots: [screenshotLibrary.contentFactory, screenshotLibrary.sources, screenshotLibrary.safety],
    sections: sectionSet("Контент-завод", [
      "Откройте Контент-завод.",
      "Создайте завод.",
      "Укажите целевой канал.",
      "Выберите аккаунт-публикатор.",
      "Добавьте источники.",
      "Добавьте базу знаний.",
      "Выберите формат постов.",
      "Включите или выключите изображения.",
      "Сгенерируйте черновик.",
      "Отредактируйте.",
      "Одобрите и опубликуйте.",
    ]),
    checklist: ["Целевой канал проверен", "Публикатор имеет доступ", "База знаний добавлена", "approval_required включен для старта", "Черновик просмотрен человеком"],
    related: ["sources", "safety", "billing"],
  }),
  article({
    slug: "safety",
    title: "Как читать безопасность и preflight",
    description:
      "Что значат риски аккаунтов, источников и запусков, почему limited/block нельзя игнорировать.",
    moduleId: "safety",
    category: "safety",
    difficulty: "basic",
    estimatedTimeMinutes: 8,
    prerequisites: ["Аккаунты", "Прокси", "Источники или кампания"],
    outcome: "Пользователь понимает, что исправить до запуска кампании.",
    screenshots: [screenshotLibrary.safety, screenshotLibrary.accounts, screenshotLibrary.sources],
    sections: sectionSet("Безопасность", [
      "Откройте Безопасность.",
      "Посмотрите аккаунты с рисками.",
      "Посмотрите источники с рисками.",
      "Исправьте прокси, аккаунт или источник.",
      "Повторите проверку.",
      "Только потом запускайте кампанию.",
    ], "Safety preflight не гарантирует 100% защиту, но помогает не запускать очевидно опасный сценарий."),
    checklist: ["Нет blocked/limited аккаунтов в запуске", "Источники доступны", "Прокси проверены", "Лимиты не завышены", "Preflight пройден"],
    related: ["accounts", "proxies", "sources", "warmup"],
  }),
  article({
    slug: "billing",
    title: "Как подключить тариф или модуль",
    description:
      "Как проверить доступы, выбрать тариф, подключить отдельный модуль и убедиться, что он разблокирован.",
    moduleId: "billing",
    category: "billing",
    difficulty: "start",
    estimatedTimeMinutes: 6,
    prerequisites: ["Кабинет", "Понимание нужного модуля", "Доступ к оплате"],
    outcome: "Нужный тариф или модуль подключен и доступен в кабинете.",
    screenshots: [screenshotLibrary.billing],
    sections: sectionSet("Тарифы и доступы", [
      "Откройте Тариф и доступы.",
      "Посмотрите текущий план.",
      "Выберите модуль или тариф.",
      "Перейдите к оплате.",
      "Вернитесь в кабинет.",
      "Проверьте, что модуль разблокирован.",
    ], "Если модуль locked, не пытайтесь обойти ограничение: подключите доступ или выберите другой сценарий."),
    checklist: ["Текущий план понятен", "Нужный модуль выбран", "Оплата прошла", "Доступ обновился", "Модуль открыт в кабинете"],
    related: ["getting-started", "content-factory", "ai-comments"],
  }),
];

export const helpArticleBySlug = Object.fromEntries(
  helpArticles.map((articleItem) => [articleItem.slug, articleItem]),
) as Record<string, HelpArticle>;

export const featuredHelpArticle = helpArticleBySlug["getting-started"];
