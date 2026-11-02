"use client"

import { Check, RefreshCw, X } from "lucide-react"
import { motion } from "motion/react"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { EmptyState, PageHeader, Panel, PanelHeader, SkeletonLine } from "@/components/common"
import { EmailHealthPanel } from "@/components/emails"
import { SmsHealthPanel } from "@/components/texts"
import { Button } from "@/components/ui/button"
import { api, ApiError } from "@/lib/api"
import { formatClock } from "@/lib/format"
import type { Health } from "@/lib/types"
import { cn } from "@/lib/utils"

export default function HealthPage() {
  const [health, setHealth] = useState<Health | null>(null)
  const [checking, setChecking] = useState(false)

  const check = useCallback(async () => {
    setChecking(true)
    try {
      const h = await api.runChecks()
      setHealth(h)
      const failed = h.checks?.filter((c) => !c.ok).length ?? 0
      if (failed) toast.error(`${failed} check${failed === 1 ? "" : "s"} failed`)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't run checks")
    } finally {
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    api
      .health()
      .then((h) => {
        setHealth(h)
        if (!h.checks) check() // first visit: run once
      })
      .catch((e) => toast.error(e instanceof ApiError ? e.message : "Couldn't load health"))
    const id = setInterval(() => api.health().then((h) => setHealth((prev) => ({ ...h, checks: h.checks ?? prev?.checks ?? null }))).catch(() => {}), 10_000)
    return () => clearInterval(id)
  }, [check])

  const link = health?.link

  return (
    <>
      <PageHeader
        title="Health"
        description="Is everything a Cartesia call needs reachable right now? No call is placed."
        actions={
          <Button variant="secondary" size="sm" onClick={check} disabled={checking}>
            <motion.span animate={checking ? { rotate: 360 } : { rotate: 0 }} transition={checking ? { repeat: Infinity, duration: 0.9, ease: "linear" } : { duration: 0 }} className="inline-flex">
              <RefreshCw strokeWidth={1.5} />
            </motion.span>
            {checking ? "Checking…" : "Check now"}
          </Button>
        }
      />

      <div className="space-y-6">
        <Panel>
          <PanelHeader title="Cartesia link" />
          <div className="flex items-center justify-between gap-4 p-4">
            {link ? (
              <>
                <div className="flex min-w-0 items-center gap-3">
                  <span className={cn("relative size-2 shrink-0 rounded-full", link.online ? "dot-pulse bg-signal text-signal" : "bg-warning")} />
                  <div className="min-w-0">
                    <div className="text-[13px] font-medium">{link.standby ? "Standby: Cartesia sends call events to the laptop" : link.online ? "Live: Cartesia sends call events here" : "Not live: call status comes from polling"}</div>
                    <div className="truncate text-[12px] text-muted-foreground">{link.detail}</div>
                  </div>
                </div>
                {link.webhook_url && <span className="hidden truncate font-mono text-[11px] text-faint lg:block">{link.webhook_url}</span>}
              </>
            ) : (
              <SkeletonLine className="w-64" />
            )}
          </div>
        </Panel>

        <Panel>
          <PanelHeader
            title="Services"
            right={health?.checked_at ? <span className="font-mono text-[12px] text-faint tnum">checked {formatClock(health.checked_at)}</span> : null}
          />
          {!health?.checks ? (
            checking || !health ? (
              <div className="divide-y divide-border">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className="grid h-11 grid-cols-[160px_1fr_64px] items-center gap-4 px-4">
                    <SkeletonLine className="w-24" />
                    <SkeletonLine className="w-48" />
                    <SkeletonLine className="ml-auto w-10" />
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState>Not checked yet. Run a check to test Cartesia, the tunnel and Telnyx.</EmptyState>
            )
          ) : (
            <ul className="divide-y divide-border">
              {health.checks.map((c, i) => (
                <motion.li
                  key={c.name}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ delay: i * 0.03 }}
                  className="grid min-h-11 grid-cols-[160px_1fr_64px] items-center gap-4 px-4 py-2 text-[13px]"
                >
                  <span className="flex items-center gap-2 font-medium">
                    {!c.ok ? (
                      <X className="size-4 text-danger" strokeWidth={1.75} />
                    ) : (
                      <Check className={cn("size-4", c.warn ? "text-warning" : "text-signal")} strokeWidth={1.75} />
                    )}
                    {c.name}
                  </span>
                  <span className={cn("break-words", !c.ok ? "text-danger" : c.warn ? "text-warning" : "text-muted-foreground")}>{c.detail}</span>
                  <span className="text-right font-mono text-[12px] text-faint tnum">{c.ms}ms</span>
                </motion.li>
              ))}
            </ul>
          )}
        </Panel>

        <EmailHealthPanel />
        <SmsHealthPanel />
      </div>
    </>
  )
}
