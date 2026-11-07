import * as React from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import {
  ArrowRightIcon,
  BookOpenIcon,
  CheckIcon,
  ClockIcon,
  ExternalLinkIcon,
  FilterIcon,
  ImageIcon,
  ListChecksIcon,
  SearchIcon,
  ShieldAlertIcon,
  XIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  featuredHelpArticle,
  helpArticleBySlug,
  helpArticles,
  helpCategoryLabels,
  type HelpArticle,
  type HelpCategory,
  type HelpScreenshot,
  type WarmupPreparationStage,
} from "@/content/help";
import { cn } from "@/lib/utils";
import { MarketingLayout, PageMeta } from "@/pages/marketing";

const siteUrl = "https://demo.invalid";

const categoryOrder: HelpCategory[] = [
  "start",
  "accounts",
  "campaigns",
  "safety",
  "content",
  "billing",
];

const categoryGroups: { title: string; text: string; categories: HelpCategory[] }[] = [
  {
    title: "Подготовка инфраструктуры",
    text: "Аккаунты, прокси, источники и базовая безопасность перед первым запуском.",
    categories: ["start", "accounts", "safety"],
  },
  {
    title: "Рабочие модули",
    text: "AI-комментарии, рассылки, чат, диалоги, реакции и контент-завод.",
    categories: ["campaigns", "content"],
  },
  {
    title: "Данные и доступы",
    text: "Парсинг, база каналов, тарифы и подключение отдельных модулей.",
    categories: ["billing"],
  },
];

function articleUrl(slug: string) {
  return `/help/${slug}`;
}

function articleSchema(article: HelpArticle) {
  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "BreadcrumbList",
        itemListElement: [
          {
            "@type": "ListItem",
            position: 1,
            name: "Инструкции",
            item: `${siteUrl}/help`,
          },
          {
            "@type": "ListItem",
            position: 2,
            name: article.title,
            item: `${siteUrl}${articleUrl(article.slug)}`,
          },
        ],
      },
      {
        "@type": "Article",
        headline: article.title,
        description: article.description,
        inLanguage: "ru-RU",
        image: article.screenshots[0]?.src
          ? `${siteUrl}${article.screenshots[0].src}`
          : `${siteUrl}/og-demo-platform.svg`,
        mainEntityOfPage: `${siteUrl}${articleUrl(article.slug)}`,
        author: {
          "@type": "Organization",
          name: "demo-platform",
        },
        publisher: {
          "@type": "Organization",
          name: "demo-platform",
          logo: {
            "@type": "ImageObject",
            url: `${siteUrl}/favicon.svg`,
          },
        },
      },
      ...(article.faq
        ? [
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
          ]
        : []),
    ],
  };
}

function helpHubSchema() {
  return {
    "@context": "https://schema.org",
    "@type": "CollectionPage",
    name: "Инструкции по demo-platform",
    url: `${siteUrl}/help`,
    description:
      "Пошаговые инструкции по аккаунтам, прокси, источникам, AI-комментариям, прогреву, парсингу и тарифам demo-platform.",
    hasPart: helpArticles.map((article) => ({
      "@type": "Article",
      name: article.title,
      url: `${siteUrl}${articleUrl(article.slug)}`,
    })),
  };
}

function difficultyLabel(article: HelpArticle) {
  if (article.difficulty === "start") return "Старт";
  if (article.difficulty === "advanced") return "Продвинутый";
  return "Базовый";
}

function HelpCard({ article, featured = false }: { article: HelpArticle; featured?: boolean }) {
  return (
    <Link
      to={articleUrl(article.slug)}
      className={cn(
        "group flex h-full flex-col rounded-lg border bg-[#fffdf7] p-5 transition hover:-translate-y-0.5 hover:border-[#171915]/28 hover:shadow-[0_18px_50px_rgba(23,25,21,0.10)]",
        featured
          ? "border-[#171915] bg-[#171915] text-[#f6f4ed]"
          : "border-[#171915]/10 text-[#171915]",
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <Badge
          className={cn(
            "rounded-md",
            featured
              ? "bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]"
              : "bg-[#171915] text-[#f6f4ed] hover:bg-[#171915]",
          )}
        >
          {helpCategoryLabels[article.category]}
        </Badge>
        <span className={cn("text-xs", featured ? "text-[#f6f4ed]/62" : "text-[#171915]/50")}>
          {article.estimatedTimeMinutes} мин
        </span>
      </div>
      <h3 className="mt-5 text-2xl font-semibold leading-tight">{article.title}</h3>
      <p className={cn("mt-3 flex-1 leading-7", featured ? "text-[#f6f4ed]/72" : "text-[#171915]/66")}>
        {article.description}
      </p>
      <div className="mt-6 flex items-center gap-2 text-sm font-semibold">
        Читать инструкцию
        <ArrowRightIcon className="size-4 transition group-hover:translate-x-1" />
      </div>
    </Link>
  );
}

export function HelpCenterPage() {
  const [query, setQuery] = React.useState("");
  const [category, setCategory] = React.useState<HelpCategory | "all">("all");

  const normalizedQuery = query.trim().toLowerCase();
  const filteredArticles = helpArticles.filter((article) => {
    const matchesCategory = category === "all" || article.category === category;
    const haystack = `${article.title} ${article.description} ${article.prerequisites.join(" ")}`.toLowerCase();
    return matchesCategory && (!normalizedQuery || haystack.includes(normalizedQuery));
  });

  return (
    <MarketingLayout>
      <PageMeta
        title="Как пользоваться demo-platform — инструкции и база знаний"
        description="Пошаговые инструкции по demo-platform: быстрый старт, аккаунты, прокси, источники, AI-комментарии, прогрев, парсинг, контент-завод и тарифы."
        canonicalPath="/help/"
        structuredData={helpHubSchema()}
      />
      <main className="w-full max-w-full overflow-x-hidden bg-[#f6f4ed]">
        <section className="relative overflow-hidden bg-[#171915] py-20 text-[#f6f4ed]">
          <div className="absolute inset-x-0 bottom-0 h-2 bg-[linear-gradient(90deg,#d7ff46,#00a6a6,#ff6f59)]" />
          <div className="mx-auto grid max-w-7xl gap-10 px-4 sm:px-6 lg:grid-cols-[1.05fr_0.95fr] lg:px-8">
            <div>
              <Badge className="mb-5 bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]">
                Как пользоваться
              </Badge>
              <h1 className="max-w-4xl text-4xl font-semibold leading-tight text-balance sm:text-6xl">
                Инструкции по demo-platform
              </h1>
              <p className="mt-5 max-w-2xl text-lg leading-8 text-[#f6f4ed]/72">
                Короткие рабочие сценарии: что нажать, что проверить, где смотреть
                результат и какие лимиты поставить для безопасного старта.
              </p>
              <div className="mt-8 flex flex-col gap-3 sm:flex-row">
                <Button
                  asChild
                  className="h-11 !bg-[#d7ff46] !text-[#171915] hover:!bg-[#c5ef31]"
                >
                  <Link to={articleUrl(featuredHelpArticle.slug)}>
                    Начать с первого запуска
                    <ArrowRightIcon />
                  </Link>
                </Button>
                <Button
                  asChild
                  variant="outline"
                  className="h-11 border-[#f6f4ed]/24 bg-transparent text-[#f6f4ed] hover:bg-[#f6f4ed]/10 hover:text-[#f6f4ed]"
                >
                  <Link to="/signup">Открыть кабинет</Link>
                </Button>
              </div>
            </div>
            <div className="rounded-lg border border-[#f6f4ed]/12 bg-white/5 p-5">
              <div className="flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.14em] text-[#d7ff46]">
                <ListChecksIcon className="size-4" />
                Первый безопасный путь
              </div>
              <ol className="mt-5 grid gap-3 text-sm text-[#f6f4ed]/78">
                {["Прокси", "Аккаунт", "Проверка", "Источник", "Черновик", "Отчет"].map(
                  (item, index) => (
                    <li key={item} className="flex items-center gap-3">
                      <span className="grid size-7 place-items-center rounded-md bg-[#d7ff46] text-xs font-semibold text-[#171915]">
                        {index + 1}
                      </span>
                      {item}
                    </li>
                  ),
                )}
              </ol>
            </div>
          </div>
        </section>

        <section className="border-b border-[#171915]/10 bg-[#fffdf7] py-6">
          <div className="mx-auto grid max-w-7xl gap-3 px-4 sm:px-6 lg:grid-cols-[1fr_auto] lg:px-8">
            <label className="relative block">
              <SearchIcon className="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-[#171915]/45" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Найти инструкцию: аккаунт, прокси, комментарии..."
                className="h-12 w-full rounded-lg border border-[#171915]/12 bg-[#f6f4ed] pl-11 pr-4 text-sm outline-none transition focus:border-[#008f8b]"
              />
            </label>
            <div className="flex gap-2 overflow-x-auto pb-1">
              <Button
                type="button"
                variant={category === "all" ? "default" : "outline"}
                className="shrink-0"
                onClick={() => setCategory("all")}
              >
                <FilterIcon />
                Все
              </Button>
              {categoryOrder.map((categoryItem) => (
                <Button
                  key={categoryItem}
                  type="button"
                  variant={category === categoryItem ? "default" : "outline"}
                  className="shrink-0"
                  onClick={() => setCategory(categoryItem)}
                >
                  {helpCategoryLabels[categoryItem]}
                </Button>
              ))}
            </div>
          </div>
        </section>

        <section className="py-16">
          <div className="mx-auto max-w-7xl min-w-0 px-4 sm:px-6 lg:px-8">
            <div className="grid gap-5 lg:grid-cols-[1.1fr_0.9fr]">
              <HelpCard article={featuredHelpArticle} featured />
              <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-1">
                {helpArticles.slice(1, 3).map((article) => (
                  <HelpCard key={article.slug} article={article} />
                ))}
              </div>
            </div>

            <div className="mt-14 grid gap-10">
              {categoryGroups.map((group) => {
                const groupArticles = filteredArticles.filter((article) =>
                  group.categories.includes(article.category),
                );
                if (!groupArticles.length) return null;
                return (
                  <section key={group.title}>
                    <div className="mb-5">
                      <h2 className="text-2xl font-semibold">{group.title}</h2>
                      <p className="mt-2 max-w-2xl text-[#171915]/62">{group.text}</p>
                    </div>
                    <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
                      {groupArticles.map((article) => (
                        <HelpCard key={article.slug} article={article} />
                      ))}
                    </div>
                  </section>
                );
              })}
            </div>
          </div>
        </section>
      </main>
    </MarketingLayout>
  );
}

function HelpScreenshotView({
  screenshot,
  onOpen,
}: {
  screenshot: HelpScreenshot;
  onOpen: (screenshot: HelpScreenshot) => void;
}) {
  return (
    <figure className="overflow-hidden rounded-lg border border-[#171915]/10 bg-[#fffdf7]">
      <button
        type="button"
        className="group block w-full text-left"
        onClick={() => onOpen(screenshot)}
      >
        <img
          src={screenshot.src}
          alt={screenshot.alt}
          loading="lazy"
          className="aspect-video w-full bg-[#171915]/5 object-cover transition group-hover:scale-[1.01]"
        />
      </button>
      <figcaption className="flex items-start gap-2 border-t border-[#171915]/10 px-4 py-3 text-sm text-[#171915]/64">
        <ImageIcon className="mt-0.5 size-4 shrink-0 text-[#008f8b]" />
        {screenshot.caption}
      </figcaption>
    </figure>
  );
}

function HelpArticleToc({ article }: { article: HelpArticle }) {
  return (
    <nav className="grid gap-2 text-sm">
      {article.preparationFlow && (
        <a
          href="#warmup-map"
          className="rounded-md px-3 py-2 text-[#171915]/66 transition hover:bg-[#171915]/6 hover:text-[#171915]"
        >
          Карта подготовки
        </a>
      )}
      {article.sections.map((section) => (
        <a
          key={section.id}
          href={`#${section.id}`}
          className="rounded-md px-3 py-2 text-[#171915]/66 transition hover:bg-[#171915]/6 hover:text-[#171915]"
        >
          {section.title}
        </a>
      ))}
    </nav>
  );
}

function WarmupPreparationFlow({ stages }: { stages: WarmupPreparationStage[] }) {
  return (
    <section
      id="warmup-map"
      className="mt-10 scroll-mt-24 overflow-hidden rounded-lg border border-[#171915]/10 bg-[#171915] text-[#f6f4ed]"
    >
      <div className="border-b border-[#f6f4ed]/12 p-5 sm:p-6">
        <p className="text-sm font-semibold uppercase tracking-[0.14em] text-[#d7ff46]">
          Карта подготовки
        </p>
        <h2 className="mt-3 max-w-2xl text-3xl font-semibold leading-tight">
          Подход к прогреву: от тихой посадки до рабочего запуска
        </h2>
        <p className="mt-4 max-w-3xl leading-8 text-[#f6f4ed]/70">
          Прогрев работает лучше, когда аккаунт не дергают рывками. Ниже - мягкая
          схема, которая помогает проверить связку аккаунт-прокси-источники и
          постепенно подвести аккаунты к реальным модулям.
        </p>
      </div>
      <div className="grid gap-3 p-4 sm:grid-cols-2 sm:p-5">
        {stages.map((stage) => (
          <article
            key={stage.id}
            className="rounded-lg border border-[#f6f4ed]/12 bg-[#fffdf7]/[0.06] p-4"
          >
            <div className="flex items-start gap-3">
              <span className="grid size-11 shrink-0 place-items-center rounded-md bg-[#d7ff46] text-sm font-semibold text-[#171915]">
                {stage.label}
              </span>
              <div className="min-w-0">
                <h3 className="text-lg font-semibold leading-tight">{stage.title}</h3>
                <p className="mt-2 leading-7 text-[#f6f4ed]/72">{stage.description}</p>
              </div>
            </div>
            <ul className="mt-4 grid gap-2 text-sm leading-6 text-[#f6f4ed]/68">
              {stage.details.map((detail) => (
                <li key={detail} className="flex gap-2">
                  <CheckIcon className="mt-1 size-4 shrink-0 text-[#d7ff46]" />
                  <span>{detail}</span>
                </li>
              ))}
            </ul>
            {stage.riskNote && (
              <p className="mt-4 rounded-md border border-[#ff6f59]/35 bg-[#ff6f59]/10 px-3 py-2 text-sm leading-6 text-[#f6f4ed]/78">
                {stage.riskNote}
              </p>
            )}
          </article>
        ))}
      </div>
      <div className="border-t border-[#f6f4ed]/12 px-5 py-4 text-sm leading-6 text-[#f6f4ed]/64">
        Прогрев не отменяет ограничения Telegram. Его задача - убрать резкие
        скачки поведения и раньше показать слабые места: прокси, источник,
        лимиты или сам аккаунт.
      </div>
    </section>
  );
}

export function HelpArticlePage() {
  const { slug } = useParams();
  const article = slug ? helpArticleBySlug[slug] : null;
  const [openedScreenshot, setOpenedScreenshot] =
    React.useState<HelpScreenshot | null>(null);

  if (!article) {
    return <Navigate to="/help" replace />;
  }

  const relatedArticles = article.related
    .map((relatedSlug) => helpArticleBySlug[relatedSlug])
    .filter(Boolean);

  return (
    <MarketingLayout>
      <PageMeta
        title={`${article.title} — инструкция demo-platform`}
        description={article.description}
        canonicalPath={articleUrl(article.slug)}
        structuredData={articleSchema(article)}
      />
      <main className="w-full max-w-full overflow-x-hidden bg-[#f6f4ed]">
        <section className="w-full max-w-full overflow-x-hidden border-b border-[#171915]/10 bg-[#171915] py-16 text-[#f6f4ed]">
          <div className="mx-auto w-full max-w-7xl min-w-0 px-4 sm:px-6 lg:px-8">
            <div className="flex flex-wrap items-center gap-2 text-sm text-[#f6f4ed]/66">
              <Link to="/help" className="hover:text-[#d7ff46]">
                Инструкции
              </Link>
              <span>/</span>
              <span>{article.title}</span>
            </div>
            <Badge className="mt-6 bg-[#d7ff46] text-[#171915] hover:bg-[#d7ff46]">
              {helpCategoryLabels[article.category]}
            </Badge>
            <h1 className="mt-5 max-w-[calc(100vw-2rem)] break-words text-4xl font-semibold leading-tight text-balance sm:max-w-4xl sm:text-6xl">
              {article.title}
            </h1>
            <p className="mt-5 max-w-[calc(100vw-2rem)] break-words text-lg leading-8 text-[#f6f4ed]/72 [overflow-wrap:anywhere] sm:max-w-3xl">
              {article.description}
            </p>
            <div className="mt-7 flex flex-wrap gap-3 text-sm">
              <span className="inline-flex items-center gap-2 rounded-md border border-[#f6f4ed]/14 px-3 py-2">
                <ClockIcon className="size-4 text-[#d7ff46]" />
                {article.estimatedTimeMinutes} минут
              </span>
              <span className="inline-flex items-center gap-2 rounded-md border border-[#f6f4ed]/14 px-3 py-2">
                <BookOpenIcon className="size-4 text-[#d7ff46]" />
                {difficultyLabel(article)}
              </span>
            </div>
          </div>
        </section>

        <section className="py-12">
          <div className="mx-auto grid max-w-7xl min-w-0 gap-8 px-4 sm:px-6 lg:grid-cols-[260px_minmax(0,860px)_260px] lg:px-8">
            <aside className="hidden lg:block">
              <div className="sticky top-24 rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-3">
                <div className="px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-[#171915]/50">
                  Оглавление
                </div>
                <HelpArticleToc article={article} />
              </div>
            </aside>

            <article className="min-w-0">
              <details className="mb-6 rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-4 lg:hidden">
                <summary className="cursor-pointer font-semibold">Оглавление</summary>
                <div className="mt-3">
                  <HelpArticleToc article={article} />
                </div>
              </details>

              <div className="grid gap-5 rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-5 sm:grid-cols-2">
                <div>
                  <h2 className="text-sm font-semibold uppercase tracking-[0.14em] text-[#008f8b]">
                    Что подготовить
                  </h2>
                  <ul className="mt-4 grid gap-2 text-sm text-[#171915]/70">
                    {article.prerequisites.map((item) => (
                      <li key={item} className="flex gap-2">
                        <CheckIcon className="mt-0.5 size-4 shrink-0 text-[#008f8b]" />
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <h2 className="text-sm font-semibold uppercase tracking-[0.14em] text-[#008f8b]">
                    Результат
                  </h2>
                  <p className="mt-4 leading-7 text-[#171915]/70">{article.outcome}</p>
                </div>
              </div>

              <div className="mt-8 grid gap-8">
                {article.screenshots.map((screenshot) => (
                  <HelpScreenshotView
                    key={screenshot.id}
                    screenshot={screenshot}
                    onOpen={setOpenedScreenshot}
                  />
                ))}
              </div>

              {article.preparationFlow && (
                <WarmupPreparationFlow stages={article.preparationFlow} />
              )}

              <div className="mt-10 grid gap-10">
                {article.sections.map((section) => (
                  <section key={section.id} id={section.id} className="scroll-mt-24">
                    <h2 className="text-3xl font-semibold leading-tight">{section.title}</h2>
                    <div className="mt-4 grid gap-4 leading-8 text-[#171915]/72">
                      {section.body.map((paragraph) => (
                        <p key={paragraph}>{paragraph}</p>
                      ))}
                    </div>
                    {section.steps && (
                      <ol className="mt-6 grid gap-3">
                        {section.steps.map((step, index) => (
                          <li
                            key={step}
                            className="grid grid-cols-[2.5rem_1fr] gap-3 rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-4"
                          >
                            <span className="grid size-9 place-items-center rounded-md bg-[#171915] text-sm font-semibold text-[#f6f4ed]">
                              {index + 1}
                            </span>
                            <span className="pt-1 leading-7">{step}</span>
                          </li>
                        ))}
                      </ol>
                    )}
                    {section.warning && (
                      <div className="mt-6 rounded-lg border border-[#ff6f59]/25 bg-[#fff1ee] p-5 text-[#171915]">
                        <div className="flex items-start gap-3">
                          <ShieldAlertIcon className="mt-0.5 size-5 shrink-0 text-[#c9412c]" />
                          <p className="leading-7">{section.warning}</p>
                        </div>
                      </div>
                    )}
                  </section>
                ))}
              </div>

              <section className="mt-12 rounded-lg border border-[#171915]/10 bg-[#171915] p-6 text-[#f6f4ed]">
                <h2 className="text-2xl font-semibold">Чеклист готовности</h2>
                <div className="mt-5 grid gap-3 sm:grid-cols-2">
                  {article.checklist.map((item) => (
                    <div key={item} className="flex items-start gap-2 text-sm text-[#f6f4ed]/78">
                      <CheckIcon className="mt-0.5 size-4 shrink-0 text-[#d7ff46]" />
                      {item}
                    </div>
                  ))}
                </div>
              </section>

              {article.faq && (
                <section className="mt-12">
                  <h2 className="text-2xl font-semibold">Частые вопросы</h2>
                  <div className="mt-5 grid gap-3">
                    {article.faq.map((item) => (
                      <details
                        key={item.question}
                        className="rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-5"
                      >
                        <summary className="cursor-pointer font-semibold">
                          {item.question}
                        </summary>
                        <p className="mt-3 leading-7 text-[#171915]/68">{item.answer}</p>
                      </details>
                    ))}
                  </div>
                </section>
              )}

              <section className="mt-12 rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-6">
                <h2 className="text-2xl font-semibold">Дальше</h2>
                <p className="mt-3 leading-7 text-[#171915]/68">
                  Откройте кабинет и повторите шаги на малом объеме. Если видите
                  ошибки доступа, FloodWait или limited, остановите запуск и
                  вернитесь к безопасности.
                </p>
                <div className="mt-6 flex flex-col gap-3 sm:flex-row">
                  <Button asChild>
                    <Link to="/signup">
                      Открыть кабинет
                      <ExternalLinkIcon />
                    </Link>
                  </Button>
                  <Button asChild variant="outline">
                    <Link to="/pricing#modules">Модули по отдельности</Link>
                  </Button>
                </div>
              </section>
            </article>

            <aside className="min-w-0">
              <div className="sticky top-24 rounded-lg border border-[#171915]/10 bg-[#fffdf7] p-4">
                <h2 className="font-semibold">Связанные инструкции</h2>
                <div className="mt-4 grid gap-2">
                  {relatedArticles.map((related) => (
                    <Link
                      key={related.slug}
                      to={articleUrl(related.slug)}
                      className="rounded-md border border-[#171915]/10 p-3 text-sm transition hover:border-[#008f8b]/40 hover:bg-[#008f8b]/5"
                    >
                      {related.title}
                    </Link>
                  ))}
                </div>
              </div>
            </aside>
          </div>
        </section>
      </main>

      {openedScreenshot && (
        <div
          className="fixed inset-0 z-[100] grid place-items-center bg-[#171915]/82 p-4"
          role="dialog"
          aria-modal="true"
        >
          <button
            type="button"
            aria-label="Закрыть скриншот"
            className="absolute right-4 top-4 grid size-10 place-items-center rounded-md bg-[#f6f4ed] text-[#171915]"
            onClick={() => setOpenedScreenshot(null)}
          >
            <XIcon className="size-5" />
          </button>
          <img
            src={openedScreenshot.src}
            alt={openedScreenshot.alt}
            className="max-h-[90svh] max-w-[96vw] rounded-lg border border-white/12 object-contain shadow-2xl"
          />
        </div>
      )}
    </MarketingLayout>
  );
}
