"use client"

import { AlertTriangle, Check } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"

import type { SyncStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

/** "Synced to Cartesia ✓" or why the agent isn't running the saved settings yet. */
export function SyncBadge({ sync, className }: { sync: SyncStatus | null; className?: string }) {
  return (
    <AnimatePresence mode="wait" initial={false}>
      {sync && (
        <motion.span
          key={sync.in_sync ? "ok" : sync.detail}
          initial={{ opacity: 0, y: 2 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
          title={sync.detail}
          className={cn(
            "inline-flex h-6 max-w-[360px] items-center gap-1.5 rounded-full px-2.5 text-[12px]",
            sync.in_sync ? "bg-signal-soft text-signal" : "bg-warning/10 text-warning",
            className,
          )}
        >
          {sync.in_sync ? <Check className="size-3.5 shrink-0" strokeWidth={2} /> : <AlertTriangle className="size-3.5 shrink-0" strokeWidth={1.75} />}
          <span className="truncate">{sync.in_sync ? "Synced to Cartesia" : sync.detail}</span>
        </motion.span>
      )}
    </AnimatePresence>
  )
}

/** Turn a save response's `cartesia` field into a SyncStatus. */
export function syncFromSave(r?: { ok: boolean; detail?: string }): SyncStatus {
  return r?.ok ? { in_sync: true, detail: "Synced to Cartesia" } : { in_sync: false, detail: r?.detail || "Not synced to Cartesia" }
}
