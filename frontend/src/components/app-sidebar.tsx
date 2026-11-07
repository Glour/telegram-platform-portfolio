import type * as React from "react"
import {
  ActivityIcon,
  BotIcon,
  FactoryIcon,
  FlameKindlingIcon,
  LayoutDashboardIcon,
  LinkIcon,
  LifeBuoyIcon,
  LockIcon,
  LogOutIcon,
  MapIcon,
  MessageCircleMoreIcon,
  MessageSquareIcon,
  MessagesSquareIcon,
  NetworkIcon,
  PackageCheckIcon,
  RadioTowerIcon,
  SearchIcon,
  SendIcon,
  ShieldCheckIcon,
  ShieldIcon,
  SmilePlusIcon,
} from "lucide-react"

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { openAssistantSupport } from "@/lib/assistant"

export type AppSection =
  | "link_tracking"
  | "dashboard"
  | "accounts"
  | "account_archive"
  | "safety"
  | "proxies"
  | "account_market"
  | "sources"
  | "channel_map"
  | "neuro"
  | "neuro_chat"
  | "neuro_dialog"
  | "message_broadcast"
  | "mass_reactions"
  | "content_factory"
  | "parsing"
  | "warmup"
  | "operations"
  | "billing"

const navigation: {
  id: AppSection
  title: string
  description: string
  icon: React.ReactNode
}[] = [
  {
    id: "dashboard",
    title: "Обзор",
    description: "Статус системы",
    icon: <LayoutDashboardIcon />,
  },
  {
    id: "accounts",
    title: "Аккаунты",
    description: "Сессии и прокси",
    icon: <ShieldIcon />,
  },
  {
    id: "safety",
    title: "Безопасность",
    description: "Риски запусков",
    icon: <ShieldCheckIcon />,
  },
  {
    id: "proxies",
    title: "Прокси",
    description: "Сетевой контур",
    icon: <NetworkIcon />,
  },
  {
    id: "account_market",
    title: "Маркет аккаунтов",
    description: "Готовые сборки",
    icon: <PackageCheckIcon />,
  },
  { id: "link_tracking", title: "Ссылки и переходы", description: "Аналитика переходов", icon: <LinkIcon /> },
  {
    id: "sources",
    title: "Источники",
    description: "Каналы",
    icon: <RadioTowerIcon />,
  },
  {
    id: "message_broadcast",
    title: "Рассылки",
    description: "Сообщения в диалоги",
    icon: <SendIcon />,
  },
  {
    id: "neuro",
    title: "AI-комментарии",
    description: "Комментарии",
    icon: <BotIcon />,
  },
  {
    id: "neuro_chat",
    title: "AI-чат",
    description: "Ответы в группах",
    icon: <MessagesSquareIcon />,
  },
  {
    id: "neuro_dialog",
    title: "AI диалоги",
    description: "Личные диалоги",
    icon: <MessageCircleMoreIcon />,
  },
  {
    id: "mass_reactions",
    title: "AI реакции",
    description: "Реакции в каналах и чатах",
    icon: <SmilePlusIcon />,
  },
  {
    id: "content_factory",
    title: "Контент-завод",
    description: "Посты и черновики",
    icon: <FactoryIcon />,
  },
  {
    id: "channel_map",
    title: "База каналов",
    description: "База по тематикам",
    icon: <MapIcon />,
  },
  {
    id: "parsing",
    title: "Парсинг",
    description: "Диалоги и аудитории",
    icon: <SearchIcon />,
  },
  {
    id: "warmup",
    title: "Прогрев",
    description: "Фоновая активность",
    icon: <FlameKindlingIcon />,
  },
  {
    id: "operations",
    title: "Админка",
    description: "События и алерты",
    icon: <ActivityIcon />,
  },
  {
    id: "billing",
    title: "Тариф и доступы",
    description: "Модули",
    icon: <LockIcon />,
  },
]

export function AppSidebar({
  activeSection,
  onSectionChange,
  userEmail,
  workspaceName,
  lockedSections = [],
  showDeveloperTools = false,
  onLogout,
  ...props
}: React.ComponentProps<typeof Sidebar> & {
  activeSection: AppSection
  onSectionChange: (section: AppSection) => void
  userEmail?: string
  workspaceName?: string
  lockedSections?: AppSection[]
  showDeveloperTools?: boolean
  onLogout?: () => void
}) {
  const visibleWorkspaceName = brandWorkspaceName(workspaceName)
  void lockedSections
  const internalSidebarUser =
    showDeveloperTools || userEmail?.trim().toLowerCase() === "owner@example.invalid"
  const visibleNavigation = internalSidebarUser
    ? navigation
    : navigation.filter((item) => item.id !== "operations")

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" tooltip="demo-platform">
              <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                <MessageSquareIcon className="size-4" />
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold">
                  demo<span className="text-[#008f8b]">platform</span>
                </span>
                <span className="truncate text-xs text-muted-foreground">платформа</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Кабинет</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {visibleNavigation.map((item) => (
                <SidebarMenuItem key={item.id}>
                  <SidebarMenuButton
                    isActive={activeSection === item.id}
                    tooltip={item.title}
                    onClick={() => onSectionChange(item.id)}
                  >
                    {item.icon}
                    <span>{item.title}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton tooltip="Рабочий аккаунт">
              <div className="flex size-6 items-center justify-center rounded-full bg-muted text-xs font-medium">
                WA
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-medium">{visibleWorkspaceName}</span>
                <span className="truncate text-xs text-muted-foreground">{userEmail || "demo-platform"}</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip="Поддержка"
              onClick={() => openAssistantSupport()}
            >
              <LifeBuoyIcon />
              <span>Поддержка</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton tooltip="Выйти из профиля" onClick={onLogout}>
              <LogOutIcon />
              <span>Выйти</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  )
}

function brandWorkspaceName(value?: string) {
  const name = value?.trim()
  if (!name) return "demo-platform"
  return name
}
