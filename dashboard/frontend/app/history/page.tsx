"use client"

import { ChevronRight } from "lucide-react"
import { motion } from "motion/react"
import Link from "next/link"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { EmptyState, PageHeader, Panel, ResultTag, SkeletonLine, Tag } from "@/components/common"
import { EmailStatusTag, TEMPLATE_LABEL } from "@/components/emails"
import { OutcomeTag } from "@/components/outcome"
import { buttonVariants } from "@/components/ui/button"
import { api, ApiError } from "@/lib/api"
import { formatDateTime, formatDuration, formatPhone, isActive } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { Call, Email } from "@/lib/types"

const COLS = "grid grid-cols-[minmax(140px,1.2fr)_minmax(150px,1.2fr)_120px_84px_90px_28px] items-center gap-4"

export default function HistoryPage() {
  const [calls, setCalls] = useState<Call[] | null>(null)
  const [emails, setEmails] = useState<Record<number, Email>>({})  // follow-up email per call (latest)

  useEffect(() => {
    api
      .emails({ limit: 1000 })
      .then((list) => setEmails(Object.fromEntries(list.filter((e) => e.call_id).reverse().map((e) => [e.call_id!, e]))))
      .catch(() => setEmails({}))
  }, [])

  useEffect(() => {
    api
      .calls()
      .then(setCalls)
      .catch((e) => {
        setCalls([])
        toast.error(e instanceof ApiError ? e.message : "Couldn't load call history")
      })
  }, [])

  // New calls appear at the top; in-progress calls update in place.
  useLiveStream((msg) => {
    if (msg.type === "email" && msg.email.call_id) setEmails((prev) => ({ ...prev, [msg.email.call_id!]: msg.email }))
    if (msg.type !== "call") return
    setCalls((prev) => {
      if (!prev) return prev
      const i = prev.findIndex((c) => c.id === msg.call.id)
      if (i === -1) return [msg.call, ...prev]
      const next = prev.slice()
      next[i] = msg.call
      return next
    })
  })

  return (
    <>
      <PageHeader title="History" description="Every call placed from here or from the terminal." />
      <Panel>
        <div className={`${COLS} h-10 border-b border-border px-4 text-[12px] text-faint`}>
          <span>Time</span>
          <span>Number</span>
          <span>Result</span>
          <span>Email</span>
          <span className="text-right">Duration</span>
          <span />
        </div>

        {calls === null ? (
          <div className="divide-y divide-border">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className={`${COLS} h-10 px-4`}>
                <SkeletonLine className="w-24" />
                <SkeletonLine className="w-32" />
                <SkeletonLine className="w-16" />
                <SkeletonLine className="w-12" />
                <SkeletonLine className="ml-auto w-10" />
                <span />
              </div>
            ))}
          </div>
        ) : calls.length === 0 ? (
          <EmptyState action={<Link href="/" className={buttonVariants({ variant: "secondary", size: "sm" })}>Place a call</Link>}>
            No calls yet. Calls you place show up here with their full transcript.
          </EmptyState>
        ) : (
          <ul className="divide-y divide-border">
            {calls.map((c, i) => (
              <motion.li
                key={c.id}
                layout="position"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.18, delay: Math.min(i, 10) * 0.015 }}
              >
                <Link
                  href={`/history/${c.id}`}
                  className={`${COLS} group h-10 px-4 text-[13px] transition-colors duration-[120ms] hover:bg-surface-2`}
                >
                  <span className="text-muted-foreground tnum">{formatDateTime(c.created_at)}</span>
                  <span className="flex items-center gap-2">
                    <span className="font-mono tnum">{formatPhone(c.number)}</span>
                    {c.simulated && <Tag>Sim</Tag>}
                    {c.source === "cli" && <Tag>Terminal</Tag>}
                  </span>
                  <span>
                    {!isActive(c.status) && c.outcome ? <OutcomeTag outcome={c.outcome} messageLeft={c.vm_message_left} /> : <ResultTag result={c.result} active={isActive(c.status)} />}
                  </span>
                  <span title={emails[c.id] ? `${TEMPLATE_LABEL[emails[c.id].template]} email: ${emails[c.id].detail || emails[c.id].to_email}` : undefined}>
                    {emails[c.id] ? <EmailStatusTag status={emails[c.id].status} /> : <span className="text-faint">–</span>}
                  </span>
                  <span className="text-right font-mono text-muted-foreground tnum">{formatDuration(c.duration_s)}</span>
                  <ChevronRight className="size-4 text-faint transition-transform duration-[120ms] group-hover:translate-x-0.5 group-hover:text-muted-foreground" strokeWidth={1.5} />
                </Link>
              </motion.li>
            ))}
          </ul>
        )}
      </Panel>
    </>
  )
}
