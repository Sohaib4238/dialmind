"use client"

import { AnimatePresence, motion } from "motion/react"

import { STATUS_LABEL } from "@/lib/format"
import type { CallStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

const TONE: Record<CallStatus | "idle", { pill: string; dot: string; pulse: boolean }> = {
  idle: { pill: "border-border text-faint", dot: "text-faint", pulse: false },
  dialing: { pill: "border-border text-muted-foreground", dot: "text-faint", pulse: true },
  ringing: { pill: "border-border text-foreground", dot: "text-warning", pulse: true },
  answered: { pill: "border-transparent bg-signal-soft text-signal", dot: "text-signal", pulse: true },
  ended: { pill: "border-border text-muted-foreground", dot: "text-faint", pulse: false },
  failed: { pill: "border-transparent bg-danger-soft text-danger", dot: "text-danger", pulse: false },
}

/** One pill that morphs between call states (layout animation + cross-faded label). */
export function StatusPill({ status }: { status: CallStatus | "idle" }) {
  const tone = TONE[status]
  const label = status === "idle" ? "Idle" : STATUS_LABEL[status]
  return (
    <motion.div
      layout
      transition={{ type: "spring", stiffness: 420, damping: 34 }}
      className={cn(
        "inline-flex h-6 items-center gap-2 overflow-hidden rounded-full border px-2.5 text-[12px] font-medium transition-colors duration-200",
        tone.pill,
      )}
    >
      <span className={cn("relative size-1.5 rounded-full bg-current", tone.dot, tone.pulse && "dot-pulse")} />
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={label}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.16 }}
        >
          {label}
        </motion.span>
      </AnimatePresence>
    </motion.div>
  )
}
