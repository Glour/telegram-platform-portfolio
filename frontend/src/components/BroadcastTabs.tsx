import type { ReactNode } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export function BroadcastTabs({ ordinary, promotional }: { ordinary: ReactNode; promotional: ReactNode }) {
  return <Tabs defaultValue="ordinary" className="gap-4">
    <TabsList aria-label="Вид рассылки" className="h-auto max-w-full flex-wrap justify-start">
      <TabsTrigger value="ordinary" className="px-4 py-2">Обычная рассылка</TabsTrigger>
      <TabsTrigger value="promotional" className="px-4 py-2">Промо-рассылка по аккаунтам</TabsTrigger>
    </TabsList>
    <TabsContent value="ordinary">{ordinary}</TabsContent>
    <TabsContent value="promotional" forceMount className="data-[state=inactive]:hidden">{promotional}</TabsContent>
  </Tabs>;
}
