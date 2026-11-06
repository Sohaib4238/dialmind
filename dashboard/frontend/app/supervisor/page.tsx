"use client"

import { Copy, Plus, Trash2, Volume2 } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { PageHeader, Panel, PanelHeader, SkeletonLine } from "@/components/common"
import { testChime } from "@/components/supervisor-popup"
import { Switch } from "@/components/switch"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, ApiError, IS_REMOTE } from "@/lib/api"
import { formatPhone } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { SupervisorHours, SupervisorPerson, SupervisorSettings } from "@/lib/types"
import { cn } from "@/lib/utils"

const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]] as const
const NEW_HOURS: SupervisorHours = { enabled: false, timezone: "America/New_York", start: "09:00", end: "17:00", days: ["mon", "tue", "wed", "thu", "fri"] }

function errorText(e: unknown, fallback: string) {
  return e instanceof ApiError ? e.message : fallback
}

export default function SupervisorPage() {
  const [s, setS] = useState<SupervisorSettings | null>(null)
  const [wait, setWait] = useState("20")
  const [editing, setEditing] = useState<string | null>(null) // a key, or "+" for a new one
  const admin = !!s?.is_admin

  const apply = useCallback((x: SupervisorSettings) => {
    setS((prev) => ({ ...prev, ...x }))
    setWait(String(x.wait_seconds))
  }, [])

  const load = useCallback(() => {
    api.supervisor().then(apply).catch((e) => toast.error(errorText(e, "Couldn't load the supervisor settings")))
  }, [apply])
  useEffect(load, [load])
  useLiveStream((msg) => {
    if (msg.type === "supervisor_settings") setS((prev) => (prev ? { ...prev, ...msg.settings } : prev))
  })

  const flip = async (p: SupervisorPerson, v: boolean) => {
    try {
      apply(await api.setAvailable(v, s?.me === p.key ? undefined : p.key))
    } catch (e) {
      toast.error(errorText(e, "Couldn't change availability"))
    }
  }

  const saveWait = async () => {
    try {
      apply(await api.saveSupervisorWait(Number(wait)))
      toast.success("Saved")
    } catch (e) {
      toast.error(errorText(e, "Couldn't save"))
    }
  }

  const remove = async (p: SupervisorPerson) => {
    if (!window.confirm(`Take ${p.name} off the supervisor list? Their login stays; they just won't get transfers.`)) return
    try {
      apply(await api.removeSupervisorPerson(p.key))
      toast.success(`${p.name} removed`)
    } catch (e) {
      toast.error(errorText(e, "Couldn't remove"))
    }
  }

  const zoneName = (z: string) => s?.hours_zones[z] ?? z
  const canFlip = (p: SupervisorPerson) => admin || s?.me === p.key

  return (
    <>
      <PageHeader
        title="Supervisor"
        description="When a lead is interested, the AI rings every available supervisor at once (pop-up with a sound). The first to accept gets the call on their own phone."
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="flex flex-col gap-6 lg:col-span-7">
          <Panel>
            <PanelHeader
              title="Supervisors"
              right={admin && editing !== "+" ? (
                <Button size="sm" variant="secondary" onClick={() => setEditing("+")}>
                  <Plus strokeWidth={1.5} />
                  Add
                </Button>
              ) : null}
            />
            {!s ? (
              <div className="space-y-3 p-4"><SkeletonLine className="w-1/2" /><SkeletonLine className="w-2/3" /></div>
            ) : (
              <div className="divide-y divide-border">
                {s.people.length === 0 && <p className="p-4 text-[13px] text-muted-foreground">No supervisors yet. Add one.</p>}
                {s.people.map((p) => editing === p.key ? (
                  <PersonForm key={p.key} s={s} person={p} onDone={(x) => { if (x) apply(x); setEditing(null) }} zoneName={zoneName} />
                ) : (
                  <div key={p.key} className="flex flex-wrap items-start justify-between gap-3 p-4 text-[13px]">
                    <div className="min-w-0 space-y-0.5">
                      <div className="font-medium">
                        {p.name}
                        <span className="ml-2 font-mono text-[12px] font-normal text-faint">{p.key}</span>
                        {s.me === p.key && <span className="ml-2 text-[11px] font-normal text-signal">you</span>}
                      </div>
                      <div className="font-mono text-[12px] text-muted-foreground tnum">{p.phone ? formatPhone(p.phone) : "No phone set"}</div>
                      <div className="text-[12px] text-faint">
                        {p.hours.enabled
                          ? `${p.hours.start}–${p.hours.end} ${zoneName(p.hours.timezone)}, ${p.hours.days.map((d) => d[0].toUpperCase() + d.slice(1)).join(" ")}`
                          : "Any time while switched on"}
                      </div>
                      <div className={cn("text-[12px]", p.can_take_calls ? "text-signal" : "text-warning")}>
                        {p.can_take_calls ? "Taking transfers now" : `Not taking transfers: ${p.why_not}`}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <Switch on={p.available} disabled={!canFlip(p)} onChange={(v) => flip(p, v)} label={`${p.name} available`} />
                      {admin && (
                        <>
                          <Button size="sm" variant="ghost" onClick={() => setEditing(p.key)}>Edit</Button>
                          <Button size="sm" variant="ghost" aria-label={`Remove ${p.name}`} onClick={() => remove(p)}>
                            <Trash2 strokeWidth={1.5} />
                          </Button>
                        </>
                      )}
                    </div>
                  </div>
                ))}
                {editing === "+" && <PersonForm s={s} onDone={(x) => { if (x) apply(x); setEditing(null) }} zoneName={zoneName} />}
              </div>
            )}
          </Panel>

          <Panel>
            <PanelHeader title="Transfers" right={!admin ? <span className="text-[12px] text-faint">Only an admin can change these</span> : null} />
            {!s ? (
              <div className="space-y-3 p-4"><SkeletonLine className="w-2/3" /></div>
            ) : (
              <div className="space-y-4 p-4 text-[13px]">
                <div className="flex flex-wrap items-end gap-3">
                  <label className="block space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">Wait for a click (seconds)</span>
                    <Input type="number" min={s.wait_min} max={s.wait_max} value={wait} disabled={!admin}
                      onChange={(e) => setWait(e.target.value)} className="w-28 font-mono tnum" />
                  </label>
                  {admin && <Button variant="secondary" onClick={saveWait}>Save</Button>}
                </div>
                <p className="text-[12px] text-faint">No Accept in time from anyone counts as busy: the AI books a callback.</p>
                <p className={cn("text-[12px]", s.can_take_calls ? "text-signal" : "text-warning")}>
                  {s.can_take_calls ? "At least one supervisor is taking transfers." : `Nobody is taking transfers: ${s.why_not}.`}
                </p>
                {s.transfer_ready === false && (
                  <p className="text-[12px] leading-5 text-warning">
                    The AI doesn&apos;t know about transfers yet. The backend sets up the request_supervisor tool in
                    Cartesia once the link is up (&quot;Cartesia link live&quot;).
                  </p>
                )}
                {s.assignments === false && (
                  <p className="text-[12px] leading-5 text-warning">
                    Cartesia refused to pass the accepting supervisor&apos;s number to the transfer
                    ({s.assignments_error}). Until that&apos;s fixed, transfers ring the first available supervisor on the list.
                  </p>
                )}
                <Button type="button" variant="secondary" size="sm" onClick={testChime}>
                  <Volume2 strokeWidth={1.5} />
                  Test sound
                </Button>
              </div>
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
                  Remote access is off. Create a login with{" "}
                  <code className="rounded bg-surface-2 px-1 font-mono text-[12px]">manage_users.py set NAME --role supervisor --phone +1XXXXXXXXXX</code>.
                </p>
              ) : s.dashboard_url ? (
                <>
                  <p className="text-muted-foreground">Send this link to the supervisors.{s.dashboard_url.includes("trycloudflare") && " It changes every time the dashboard restarts."}</p>
                  <div className="flex items-center gap-2">
                    <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-surface-2 px-2 py-1.5 font-mono text-[12px]">{s.dashboard_url}</code>
                    <Button size="sm" variant="secondary" onClick={() => navigator.clipboard.writeText(s.dashboard_url!).then(() => toast.success("Link copied"))}>
                      <Copy strokeWidth={1.5} />
                      Copy
                    </Button>
                  </div>
                </>
              ) : (
                <p className="text-muted-foreground">
                  {IS_REMOTE ? "Each supervisor signs in with their own login and switches themselves on." : "Login is set. Waiting for the dashboard tunnel (start_all.ps1 starts it)…"}
                </p>
              )}
              <p className="text-[12px] text-faint">
                Each supervisor signs in with their own login, clicks once anywhere (so the browser allows sound), keeps
                the tab open and switches themselves on.
              </p>
            </div>
          </Panel>
        </div>
      </div>
    </>
  )
}

function PersonForm({ s, person, onDone, zoneName }: {
  s: SupervisorSettings
  person?: SupervisorPerson
  onDone: (x: SupervisorSettings | null) => void
  zoneName: (z: string) => string
}) {
  const taken = new Set(s.people.map((p) => p.key))
  const free = (s.logins ?? []).filter((l) => !taken.has(l) || l === person?.key)
  const [key, setKey] = useState(person?.key ?? free[0] ?? "")
  const [name, setName] = useState(person?.name ?? "")
  const [phone, setPhone] = useState(person?.phone ?? "")
  const [hours, setHours] = useState<SupervisorHours>(person?.hours ?? NEW_HOURS)
  const [saving, setSaving] = useState(false)

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    try {
      onDone(await api.saveSupervisorPerson(person?.key ?? key, { key, name: name || key, phone, hours }))
      toast.success("Saved")
    } catch (err) {
      toast.error(errorText(err, "Couldn't save"))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={save} className="space-y-4 bg-surface-2 p-4 text-[13px]">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <label className="block space-y-1.5">
          <span className="text-[12px] font-medium text-muted-foreground">Login</span>
          {free.length > 0 ? (
            <Select value={key} onValueChange={setKey}>
              <SelectTrigger aria-label="Login"><SelectValue placeholder="Pick a login" /></SelectTrigger>
              <SelectContent>{free.map((l) => <SelectItem key={l} value={l}>{l}</SelectItem>)}</SelectContent>
            </Select>
          ) : (
            <Input value={key} onChange={(e) => setKey(e.target.value)} placeholder="login name" className="font-mono" />
          )}
        </label>
        <label className="block space-y-1.5">
          <span className="text-[12px] font-medium text-muted-foreground">Name (pop-up, History)</span>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Sara" />
        </label>
        <label className="block space-y-1.5">
          <span className="text-[12px] font-medium text-muted-foreground">Their phone (transfers ring it)</span>
          <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+1XXXXXXXXXX" className="font-mono tnum" />
        </label>
      </div>

      <div className="space-y-3">
        <label className="flex items-center justify-between gap-4">
          <span>
            <span className="block font-medium">Working hours</span>
            <span className="text-[12px] text-muted-foreground">Outside them they aren&apos;t rung, even if switched on.</span>
          </span>
          <Switch on={hours.enabled} onChange={(v) => setHours({ ...hours, enabled: v })} label="Use working hours" />
        </label>
        {hours.enabled && (
          <div className="space-y-3 rounded-md border border-border bg-surface p-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">Time zone</span>
                <Select value={hours.timezone} onValueChange={(v) => setHours({ ...hours, timezone: v as SupervisorHours["timezone"] })}>
                  <SelectTrigger aria-label="Time zone"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {Object.keys(s.hours_zones).map((z) => <SelectItem key={z} value={z}>{zoneName(z)}</SelectItem>)}
                  </SelectContent>
                </Select>
              </label>
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">From</span>
                <Input type="time" value={hours.start} onChange={(e) => setHours({ ...hours, start: e.target.value })} className="font-mono tnum" />
              </label>
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">To</span>
                <Input type="time" value={hours.end} onChange={(e) => setHours({ ...hours, end: e.target.value })} className="font-mono tnum" />
              </label>
            </div>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Working days">
              {DAYS.map(([d, label]) => {
                const on = hours.days.includes(d)
                return (
                  <button key={d} type="button" aria-pressed={on}
                    onClick={() => setHours({ ...hours, days: on ? hours.days.filter((x) => x !== d) : [...hours.days, d] })}
                    className={cn("h-7 rounded-md border px-2.5 text-[12px] transition-colors duration-[120ms]",
                      on ? "border-transparent bg-signal-soft text-signal" : "border-border text-muted-foreground hover:bg-surface")}>
                    {label}
                  </button>
                )
              })}
            </div>
            <p className="text-[11px] leading-4 text-faint">A shift can cross midnight (e.g. 18:00 to 03:00 UTC); the day is the day it starts.</p>
          </div>
        )}
      </div>
      <div className="flex gap-2">
        <Button type="submit" disabled={saving || !key}>{saving ? "Saving…" : "Save"}</Button>
        <Button type="button" variant="ghost" onClick={() => onDone(null)}>Cancel</Button>
      </div>
    </form>
  )
}
