"use client"

import { motion } from "motion/react"

import { cn } from "@/lib/utils"

export function Switch({ on, onChange, disabled, label, id, small }: {
  on: boolean
  onChange: (on: boolean) => void
  disabled?: boolean
  label?: string
  id?: string
  small?: boolean
}) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className={cn(
        "relative inline-flex shrink-0 items-center rounded-full border transition-colors duration-[120ms] disabled:opacity-50",
        small ? "h-5 w-9" : "h-6 w-11",
        on ? "border-transparent bg-signal" : "border-line-strong bg-surface-2",
      )}
    >
      <motion.span
        layout
        transition={{ type: "spring", stiffness: 420, damping: 34 }}
        className={cn("rounded-full", small ? "size-3.5" : "size-4",
          on ? (small ? "ml-[18px] bg-signal-fg" : "ml-[22px] bg-signal-fg") : "ml-[3px] bg-muted-foreground")}
      />
    </button>
  )
}
