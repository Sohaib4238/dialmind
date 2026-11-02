"use client"

import { AlertTriangle, FlaskConical } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { api } from "@/lib/api"
import { formatPhone } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import { cn } from "@/lib/utils"

/** On every page: where calls go right now. Test mode is enforced by the backend for every call
 * (dialer, retries, callbacks, "Call this lead", the Call page); this only makes it visible. */
export function TestModeBanner() {
  const [t, setT] = useState<{ on: boolean; number: string } | null>(null)

  const load = useCallback(() => {
    api.testMode().then(setT).catch(() => {})
  }, [])
  useEffect(() => {
    load()
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
  }, [load])
  useLiveStream((msg) => {
    if (msg.type === "dialer") setT({ on: msg.dialer.test_mode, number: msg.dialer.test_number })
  })

  if (!t) return null
  return (
    <div
      role="status"
      className={cn(
        "sticky top-0 z-30 flex items-center gap-2 border-b px-6 py-1.5 text-[12px] font-medium md:px-8",
        t.on ? "border-warning/20 bg-warning/10 text-warning" : "border-danger/20 bg-danger-soft text-danger",
      )}
    >
      {t.on ? <FlaskConical className="size-3.5" strokeWidth={1.75} /> : <AlertTriangle className="size-3.5" strokeWidth={1.75} />}
      {t.on
        ? `TEST MODE ON – calls go to ${formatPhone(t.number) || "TEST_CALL_NUMBER (not set: no call can be placed)"}`
        : "LIVE MODE – leads are called at their own numbers"}
    </div>
  )
}
