import * as React from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

function MagicButton({
  className,
  children,
  ...props
}: React.ComponentProps<typeof Button>) {
  return (
    <Button
      data-slot="magic-button"
      className={cn(
        "relative isolate overflow-hidden border-violet-500/20 bg-violet-600 text-white shadow-[0_10px_28px_rgba(124,58,237,0.28)] hover:bg-violet-500 hover:shadow-[0_14px_34px_rgba(124,58,237,0.36)] focus-visible:ring-violet-400/40",
        "before:absolute before:inset-y-[-120%] before:left-[-40%] before:z-[-1] before:w-1/3 before:rotate-12 before:bg-white/30 before:blur-md before:transition-transform before:duration-700 hover:before:translate-x-[360%]",
        "after:absolute after:inset-0 after:z-[-2] after:bg-[radial-gradient(circle_at_20%_10%,rgba(255,255,255,0.35),transparent_30%),linear-gradient(135deg,#7c3aed,#a855f7_48%,#4f46e5)]",
        className,
      )}
      {...props}
    >
      {children}
    </Button>
  )
}

export { MagicButton }
