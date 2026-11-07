import { LifeBuoyIcon, LogOutIcon, RefreshCwIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { openAssistantSupport } from "@/lib/assistant"

export function SiteHeader({
  title,
  onRefresh,
  onLogout,
}: {
  title: string
  onRefresh: () => void
  onLogout: () => void
}) {
  return (
    <header className="sticky top-0 z-30 flex min-h-14 shrink-0 items-center gap-2 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="flex w-full min-w-0 items-center gap-1 px-3 sm:gap-2 sm:px-4 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator orientation="vertical" className="mx-1 hidden data-[orientation=vertical]:h-4 sm:block" />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-base font-medium">{title}</h1>
        </div>
        <Button variant="outline" size="sm" className="h-9 shrink-0 px-2 sm:px-3" onClick={onRefresh}>
          <RefreshCwIcon />
          <span className="hidden sm:inline">Обновить</span>
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-9 shrink-0 px-2 sm:px-3"
          onClick={() => openAssistantSupport()}
        >
          <LifeBuoyIcon />
          <span className="hidden sm:inline">Поддержка</span>
        </Button>
        <Button variant="ghost" size="sm" className="h-9 shrink-0 px-2 sm:px-3" onClick={onLogout}>
          <LogOutIcon />
          <span className="hidden sm:inline">Выйти</span>
        </Button>
      </div>
    </header>
  )
}
