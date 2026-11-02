"use client"

import { Copy, Volume2 } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { PageHeader, Panel, PanelHeader, SkeletonLine } from "@/components/common"
import { testChime } from "@/components/supervisor-popup"
import { Switch } from "@/components/switch"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useIsAdmin } from "@/components/login-gate"
import { api, ApiError } from "@/lib/api"
import { useLiveStream } from "@/lib/live"
import type { SupervisorHours, SupervisorSettings } from "@/lib/types"
import { cn } from "@/lib/utils"

const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]] as const

export default function SupervisorPage() {
  const [s, setS] = useState<SupervisorSettings | null>(null)
  const [phone, setPhone] = useState("")
  const [wait, setWait] = useState("20")
  const [hours, setHours] = useState<SupervisorHours | null>(null)
  const [saving, setSaving] = useState(false)
  const admin = useIsAdmin()

  const apply = useCallback((x: SupervisorSettings) => {
    setS(x)
    setPhone(x.phone)
    setWait(String(x.wait_seconds))
    setHours(x.hours)
  }, [])

  useEffect(() => {
    api.supervisor().then(apply).catch((e) => toast.error(e instanceof ApiError ? e.message : "Couldn't load the supervisor settings"))
  }, [apply])
  useLiveStream((msg) => {
    if (msg.type === "supervisor_settings") setS((prev) => (prev ? { ...prev, ...msg.settings } : prev))
  })

  const flip = async (v: boolean) => {
    try {
      setS(await api.setAvailable(v).then((x) => ({ ...s!, ...x })))
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't change availability")
    }
  }

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!hours) return
    setSaving(true)
    try {
      const x = await api.saveSupervisor({ phone, wait_seconds: Number(wait), hours })
      apply({ ...s!, ...x })
      toast.success("Saved")
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't save")
    } finally {
      setSaving(false)
    }
  }

  const zoneName = (z: string) => s?.hours_zones[z] ?? z

  return (
    <>
      <PageHeader
        title="Supervisor"
        description="When a lead is interested, the AI asks you first (pop-up with a sound on every open dashboard), then transfers the call to your phone."
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="flex flex-col gap-6 lg:col-span-7">
          <Panel>
            <PanelHeader title="Availability" />
            {!s ? (
              <div className="space-y-3 p-4"><SkeletonLine className="w-1/2" /></div>
            ) : (
              <div className="space-y-3 p-4 text-[13px]">
                <label className="flex items-center justify-between gap-4">
                  <span>
                    <span className="block font-medium">Available for transfers</span>
                    <span className="text-[12px] text-muted-foreground">Off: the AI doesn&apos;t ask; it books a callback instead.</span>
                  </span>
                  <Switch on={s.available} onChange={flip} label="Available for transfers" />
                </label>
                <p className={cn("text-[12px]", s.can_take_calls ? "text-signal" : "text-warning")}>
                  {s.can_take_calls ? "Taking transfers now." : `Not taking transfers: ${s.why_not}.`}
                </p>
                {s.transfer_ready === false && (
                  <p className="text-[12px] leading-5 text-warning">
                    The AI doesn&apos;t know about transfers yet. The backend sets up the request_supervisor tool in
                    Cartesia once the tunnel is up (&quot;Cartesia link live&quot;).
                  </p>
                )}
                <Button type="button" variant="secondary" size="sm" onClick={testChime}>
                  <Volume2 strokeWidth={1.5} />
                  Test sound
                </Button>
              </div>
            )}
          </Panel>

          <Panel>
            <PanelHeader title="Transfer settings" right={!admin ? <span className="text-[12px] text-faint">Only an admin can change these</span> : null} />
            {!s || !hours ? (
              <div className="space-y-3 p-4"><SkeletonLine className="w-2/3" /><SkeletonLine className="w-1/2" /></div>
            ) : (
              <form onSubmit={save} className="space-y-5 p-4 text-[13px]">
                <fieldset disabled={!admin} className="space-y-5 disabled:opacity-70">
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <label className="block space-y-1.5">
                      <span className="text-[12px] font-medium text-muted-foreground">Your phone (calls are transferred here)</span>
                      <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+12125550100" className="font-mono tnum" />
                    </label>
                    <label className="block space-y-1.5">
                      <span className="text-[12px] font-medium text-muted-foreground">Wait for your click (seconds)</span>
                      <Input type="number" min={s.wait_min} max={s.wait_max} value={wait} onChange={(e) => setWait(e.target.value)} className="font-mono tnum" />
                      <span className="block text-[11px] text-faint">No click in time counts as busy.</span>
                    </label>
                  </div>

                  <div className="space-y-3">
                    <label className="flex items-center justify-between gap-4">
                      <span>
                        <span className="block font-medium">Working hours</span>
                        <span className="text-[12px] text-muted-foreground">Outside them the AI books a callback, even if you&apos;re switched on.</span>
                      </span>
                      <Switch on={hours.enabled} onChange={(v) => setHours({ ...hours, enabled: v })} label="Use working hours" />
                    </label>
                    {hours.enabled && (
                      <div className="space-y-3 rounded-md border border-border bg-surface-2 p-3">
                        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                          <label className="block space-y-1.5 sm:col-span-1">
                            <span className="text-[12px] font-medium text-muted-foreground">Time zone</span>
                            <Select value={hours.timezone} onValueChange={(v) => setHours({ ...hours, timezone: v as SupervisorHours["timezone"] })}>
                              <SelectTrigger aria-label="Time zone"><SelectValue /></SelectTrigger>
                              <SelectContent>
                                {Object.keys(s.hours_zones).map((z) => <SelectItem key={z} value={z}>{zoneName(z)}</SelectItem>)}
                              </SelectContent>
                            </Select>
                          </label>
                          <label className="block space-y-1.5">
                            <span className="text-[12px] font-medium text-muted-foreground">From ({zoneName(hours.timezone)})</span>
                            <Input type="time" value={hours.start} onChange={(e) => setHours({ ...hours, start: e.target.value })} className="font-mono tnum" />
                          </label>
                          <label className="block space-y-1.5">
                            <span className="text-[12px] font-medium text-muted-foreground">To ({zoneName(hours.timezone)})</span>
                            <Input type="time" value={hours.end} onChange={(e) => setHours({ ...hours, end: e.target.value })} className="font-mono tnum" />
                          </label>
                        </div>
                        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Working days">
                          {DAYS.map(([d, label]) => {
                            const on = hours.days.includes(d)
                            return (
                              <button
                                key={d}
                                type="button"
                                aria-pressed={on}
                                onClick={() => setHours({ ...hours, days: on ? hours.days.filter((x) => x !== d) : [...hours.days, d] })}
                                className={cn("h-7 rounded-md border px-2.5 text-[12px] transition-colors duration-[120ms]",
                                  on ? "border-transparent bg-signal-soft text-signal" : "border-border text-muted-foreground hover:bg-surface")}
                              >
                                {label}
                              </button>
                            )
                          })}
                        </div>
                        <p className="text-[11px] leading-4 text-faint">
                          A shift can cross midnight (e.g. 18:00 to 03:00 UTC); the day is the day it starts.
                        </p>
                      </div>
                    )}
                  </div>
                  {admin && (
                    <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
                  )}
                </fieldset>
              </form>
            )}
          </Panel>
        </div>

        <div className="flex flex-col gap-6 lg:col-span-5">
          <Panel>
            <PanelHeader title="Use it from another laptop" />
            <div className="space-y-3 p-4 text-[13px] leading-5">
              {!s ? (
                <SkeletonLine className="w-2/3" />
              ) : !s.login_configured ? (
                <p className="text-muted-foreground">
                  Remote access is off. On this computer run{" "}
                  <code className="rounded bg-surface-2 px-1 font-mono text-[12px]">python dashboard\backend\manage_users.py set NAME --role supervisor</code>,
                  then restart with <code className="rounded bg-surface-2 px-1 font-mono text-[12px]">.\start_all.ps1</code>.
                </p>
              ) : s.dashboard_url ? (
                <>
                  <p className="text-muted-foreground">Send this link to the supervisor.{s.dashboard_url.includes("trycloudflare") && " It changes every time the dashboard restarts."}</p>
                  <div className="flex items-center gap-2">
                    <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-surface-2 px-2 py-1.5 font-mono text-[12px]">{s.dashboard_url}</code>
                    <Button size="sm" variant="secondary" onClick={() => navigator.clipboard.writeText(s.dashboard_url!).then(() => toast.success("Link copied"))}>
                      <Copy strokeWidth={1.5} />
                      Copy
                    </Button>
                  </div>
                  <p className="text-[12px] text-faint">They sign in with the login made for them (manage_users.py).</p>
                </>
              ) : (
                <p className="text-muted-foreground">Login is set. Waiting for the dashboard tunnel (start_all.ps1 starts it)…</p>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </>
  )
}
