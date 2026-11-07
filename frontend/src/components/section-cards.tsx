import { BotIcon, FlameKindlingIcon, MessageCircleIcon, ShieldCheckIcon, TrendingUpIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardAction,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"

export type SectionCardStats = {
  accountsTotal: number
  accountsReady: number
  sourcesTotal: number
  campaignsTotal: number
  campaignsActive: number
  warmupPlansTotal: number
}

export type SectionCardTarget = "accounts" | "sources" | "neuro" | "warmup"

export function SectionCards({
  stats,
  onOpenTarget,
}: {
  stats: SectionCardStats
  onOpenTarget?: (target: SectionCardTarget) => void
}) {
  const cards = [
    {
      label: "Аккаунты",
      target: "accounts" as const,
      value: String(stats.accountsTotal),
      badge: `${stats.accountsReady} готовы`,
      footer: "Сессии и проверки Telegram аккаунтов",
      icon: <ShieldCheckIcon />,
    },
    {
      label: "Источники",
      target: "sources" as const,
      value: String(stats.sourcesTotal),
      badge: "в работе",
      footer: "Каналы и ссылки в рабочем контуре",
      icon: <MessageCircleIcon />,
    },
    {
      label: "Кампании",
      target: "neuro" as const,
      value: String(stats.campaignsTotal),
      badge: `${stats.campaignsActive} активны`,
      footer: "AI-комментарии и черновики",
      icon: <BotIcon />,
    },
    {
      label: "Прогрев",
      target: "warmup" as const,
      value: String(stats.warmupPlansTotal),
      badge: "планы",
      footer: "Планы ручных чеклистов и отдыха",
      icon: <FlameKindlingIcon />,
    },
  ]

  return (
    <div className="grid grid-cols-1 gap-4 *:data-[slot=card]:bg-gradient-to-t *:data-[slot=card]:from-primary/5 *:data-[slot=card]:to-card *:data-[slot=card]:shadow-xs sm:grid-cols-2 xl:grid-cols-4 dark:*:data-[slot=card]:bg-card">
      {cards.map((card) => (
        <Card
          key={card.label}
          className={cn(
            "@container/card transition hover:-translate-y-0.5 hover:ring-foreground/20 hover:shadow-sm",
            onOpenTarget &&
              "cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
          )}
          role={onOpenTarget ? "button" : undefined}
          tabIndex={onOpenTarget ? 0 : undefined}
          onClick={() => onOpenTarget?.(card.target)}
          onKeyDown={(event) => {
            if (!onOpenTarget) return
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault()
              onOpenTarget(card.target)
            }
          }}
        >
          <CardHeader>
            <CardDescription>{card.label}</CardDescription>
            <CardTitle className="text-2xl font-semibold tabular-nums @[250px]/card:text-3xl">
              {card.value}
            </CardTitle>
            <CardAction>
              <Badge variant="outline">
                {card.icon}
                {card.badge}
              </Badge>
            </CardAction>
          </CardHeader>
          <CardFooter className="flex-col items-start gap-1.5 text-sm">
            <div className="line-clamp-1 flex gap-2 font-medium">
              {card.footer}
              <TrendingUpIcon className="size-4" />
            </div>
            <div className="text-muted-foreground">Обновляется после синхронизации API</div>
          </CardFooter>
        </Card>
      ))}
    </div>
  )
}
