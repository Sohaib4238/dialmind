"use client"

import { motion, useReducedMotion } from "motion/react"
import { useEffect, useState } from "react"

import { cn } from "@/lib/utils"

const BARS = 24

/**
 * Live activity bars. Driven by speaking-state events from the agent (who is talking),
 * not by raw audio. Accent while the agent speaks, muted while the callee speaks,
 * flat when nobody is talking or the call isn't live.
 */
export function Waveform({ live, speaker }: { live: boolean; speaker: "agent" | "callee" | null }) {
  const reduce = useReducedMotion()
  const active = live && speaker !== null
  const [heights, setHeights] = useState<number[]>(() => Array(BARS).fill(2))

  useEffect(() => {
    if (!active || reduce) {
      setHeights(Array(BARS).fill(active ? 8 : 2))
      return
    }
    const tick = () =>
      setHeights(
        Array.from({ length: BARS }, (_, i) => {
          // Taller in the middle, random per bar, so it reads as speech not noise.
          const envelope = 0.45 + 0.55 * Math.sin((Math.PI * (i + 0.5)) / BARS)
          return 3 + Math.round(Math.random() * 17 * envelope)
        }),
      )
    tick()
    const id = setInterval(tick, 140)
    return () => clearInterval(id)
  }, [active, reduce])

  return (
    <div className="flex h-6 items-center gap-[3px]" aria-hidden>
      {heights.map((h, i) => (
        <motion.span
          key={i}
          className={cn(
            "w-[2px] rounded-full",
            !live ? "bg-line-strong" : speaker === "agent" ? "bg-signal" : speaker === "callee" ? "bg-muted-foreground" : "bg-faint",
          )}
          animate={{ height: h }}
          transition={{ type: "spring", stiffness: 520, damping: 30, mass: 0.4 }}
        />
      ))}
    </div>
  )
}
