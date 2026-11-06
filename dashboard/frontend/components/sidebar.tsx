"use client"

import { Activity, AudioLines, History, LogOut, Phone, SlidersHorizontal, UserRoundCheck, Users } from "lucide-react"
import { motion } from "motion/react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { useIsAdmin } from "@/components/login-gate"
import { Switch } from "@/components/switch"
import { api, ApiError, IS_REMOTE } from "@/lib/api"
import { useLiveStatus, useLiveStream } from "@/lib/live"
import type { SupervisorSettings } from "@/lib/types"
import { cn } from "@/lib/utils"

const NAV = [
  { href: "/leads", label: "Leads", icon: Users },
  { href: "/", label: "Call", icon: Phone, adminOnly: true },
  { href: "/history", label: "History", icon: History },
  { href: "/supervisor", label: "Supervisor", icon: UserRoundCheck },
  { href: "/settings", label: "Instructions", icon: SlidersHorizontal, adminOnly: true },
  { href: "/voice", label: "Voice", icon: AudioLines, adminOnly: true },
  { href: "/health", label: "Health", icon: Activity },
]

/** Cartesia can reach us: tunnel up + webhook registered. "standby": a server that leaves the link to the laptop. */
function useLinkOnline() {
  const [online, setOnline] = useState<boolean | "standby" | null>(null)
  useEffect(() => {
    let alive = true
    const tick = () =>
      api
        .health()
        .then((h) => alive && setOnline(h.link.standby ? "standby" : h.link.online))
        .catch(() => alive && setOnline(false))
    tick()
    const id = setInterval(tick, 10_000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])
  return online
}

export function Sidebar() {
  const pathname = usePathname()
  const live = useLiveStatus()
  const link = useLinkOnline()
  const admin = useIsAdmin()
  const [remote, setRemote] = useState(false) // after mount: the server render can't know the hostname
  useEffect(() => setRemote(IS_REMOTE), [])

  return (
    <aside className="sticky top-0 hidden h-dvh w-[232px] shrink-0 flex-col border-r border-border bg-surface md:flex">
      <div className="flex h-14 items-center gap-2.5 px-5">
        <div className="grid size-6 place-items-center rounded-md border border-line-strong bg-surface-2">
          <Phone className="size-3.5 text-signal" strokeWidth={1.75} />
        </div>
        <div className="leading-tight">
          <div className="text-[13px] font-semibold tracking-[-0.01em]">DialMind</div>
          <div className="text-[11px] text-faint">Testing dashboard</div>
        </div>
      </div>

      <div className="px-5 pt-4 pb-2 text-[11px] font-medium tracking-[0.04em] text-faint uppercase">Workspace</div>
      <nav className="flex flex-col gap-0.5 px-3">
        {NAV.filter((n) => admin || !n.adminOnly).map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href)
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-[13px] transition-colors duration-[120ms]",
                active ? "text-foreground" : "text-muted-foreground hover:bg-surface-2/60 hover:text-foreground",
              )}
            >
              {active && (
                <motion.span
                  layoutId="nav-active"
                  className="absolute inset-0 rounded-md bg-surface-2"
                  transition={{ type: "spring", stiffness: 420, damping: 34 }}
                />
              )}
              <Icon className="relative size-4" strokeWidth={1.5} />
              <span className="relative">{label}</span>
            </Link>
          )
        })}
      </nav>

      <div className="mt-auto border-t border-border px-5 py-3">
        <AvailabilitySwitch />
      </div>
      <div className="space-y-2 border-t border-border px-5 py-4 text-[12px]">
        <StatusLine
          ok={link === "standby" ? "warn" : link}
          label={link == null ? "Checking Cartesia link…" : link === "standby" ? "Standby (laptop is live)" : link ? "Cartesia link live" : "Cartesia link down (polling)"}
        />
        <StatusLine
          ok={live === "connected" ? true : live === "fallback" ? "warn" : false}
          label={live === "connected" ? "Live updates connected" : live === "fallback" ? "Live (fallback polling)" : "Live updates reconnecting…"}
        />
        {remote && (
          <button
            onClick={() => api.logout().finally(() => window.location.reload())}
            className="flex items-center gap-2 text-muted-foreground transition-colors duration-[120ms] hover:text-foreground"
          >
            <LogOut className="size-3.5" strokeWidth={1.5} />
            Sign out
          </button>
        )}
      </div>
    </aside>
  )
}

/** Narrow windows: the sidebar collapses into a top bar. */
export function MobileNav() {
  const pathname = usePathname()
  const admin = useIsAdmin()
  return (
    <nav className="sticky top-0 z-10 flex h-12 items-center gap-1 overflow-x-auto border-b border-border bg-surface px-3 md:hidden">
      {NAV.filter((n) => admin || !n.adminOnly).map(({ href, label, icon: Icon }) => {
        const active = href === "/" ? pathname === "/" : pathname.startsWith(href)
        return (
          <Link
            key={href}
            href={href}
            className={cn(
              "flex h-8 shrink-0 items-center gap-2 rounded-md px-2.5 text-[13px]",
              active ? "bg-surface-2 text-foreground" : "text-muted-foreground",
            )}
          >
            <Icon className="size-4" strokeWidth={1.5} />
            {label}
          </Link>
        )
      })}
    </nav>
  )
}

function StatusLine({ ok, label }: { ok: boolean | null | "warn"; label: string }) {
  return (
    <div className="flex items-center gap-2 text-muted-foreground">
      <span
        className={cn(
          "size-1.5 rounded-full",
          ok == null ? "bg-faint" : ok === "warn" ? "bg-warning" : ok ? "bg-signal" : "bg-danger",
        )}
      />
      {label}
    </div>
  )
}

/** "Available for transfers": each supervisor flips their own, from any screen (also remotely). On this computer
 * (no login) with one supervisor, it's theirs. Hidden for anyone else (an admin uses the Supervisor page). */
function AvailabilitySwitch() {
  const [s, setS] = useState<SupervisorSettings | null>(null)
  useEffect(() => {
    api.supervisor().then(setS).catch(() => {})
  }, [])
  useLiveStream((msg) => {
    if (msg.type === "supervisor_settings") setS((prev) => (prev ? { ...prev, ...msg.settings } : prev))
  })
  const key = s?.me_supervisor ? s.me : !IS_REMOTE && s?.people.length === 1 ? s.people[0].key : null
  const p = s?.people.find((x) => x.key === key)
  if (!s || !p) return null
  const flip = async (v: boolean) => {
    setS({ ...s, people: s.people.map((x) => (x.key === p.key ? { ...x, available: v } : x)) })
    try {
      setS({ ...s, ...(await api.setAvailable(v, s.me_supervisor ? undefined : p.key)) })
    } catch (e) {
      setS(s)
      toast.error(e instanceof ApiError ? e.message : "Couldn't change availability")
    }
  }
  return (
    <div className="space-y-1">
      <label className="flex items-center justify-between gap-3 text-[12px] text-muted-foreground">
        {s.me_supervisor ? "I'm available" : `${p.name} available`}
        <Switch small on={p.available} onChange={flip} label="Available for transfers" />
      </label>
      {p.available && !p.can_take_calls && p.why_not && <p className="text-[11px] leading-4 text-warning">{p.why_not}</p>}
    </div>
  )
}
