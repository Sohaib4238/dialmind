"use client"

import { ChevronDown, Palette, RefreshCw, Settings2, Sheet } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { Panel, PanelHeader } from "@/components/common"
import { Switch } from "@/components/switch"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { api, ApiError } from "@/lib/api"
import { formatDateTime, timeAgo } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { SheetSettings, SheetsStatus, SheetSyncSummary } from "@/lib/types"
import { cn } from "@/lib/utils"

const SWATCH: Record<string, string> = {
  green: "#b7e1cd", yellow: "#ffe599", orange: "#f9cb9c", red: "#ea9999", pink: "#f4b6e6", purple: "#b4a7d6",
  blue: "#a4c2f4", cyan: "#a2d9e0", gray: "#cccccc",
}

/** The sales team's Google Sheet (read only): last sync, Sync now, what came in, and the settings (admin). */
export function SheetSyncPanel({ admin }: { admin: boolean }) {
  const [st, setSt] = useState<SheetsStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState<"none" | "details" | "settings">("none")

  const load = useCallback(() => {
    api.sheets().then(setSt).catch(() => {})
  }, [])
  useEffect(load, [load])
  useLiveStream((msg) => {
    if (msg.type === "sheet_sync") setSt((prev) => (prev ? { ...prev, last_sync: msg.summary, running: false } : prev))
    if (msg.type === "sheet_results") setSt((prev) => (prev ? { ...prev, results: msg.results } : prev))
  })

  const syncNow = async () => {
    setBusy(true)
    try {
      const r = await api.syncSheets()
      if (r.ok) toast.success(`Sheet synced: ${r.new} new, ${r.handled?.new ?? 0} handled by sales, ${r.duplicates} duplicates`)
      else toast.error(r.error ?? "Sync failed")
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't sync")
    } finally {
      setBusy(false)
    }
  }

  const recheck = async () => {
    setBusy(true)
    try {
      const r = await api.recheckColors()
      if (r.ok) {
        const n = r.handled?.existing ?? 0
        toast.success(`Colours checked: ${n} lead${n === 1 ? "" : "s"} now handled by sales`, { description: "Nothing was imported or changed otherwise." })
        setOpen("details")
      } else toast.error(r.error ?? "Colour check failed")
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't check the colours")
    } finally {
      setBusy(false)
    }
  }

  if (!st) return null
  const cfg = st.settings
  const last = st.last_sync
  const configured = !!cfg.sales_sheet
  if (!configured && !admin) return null

  return (
    <Panel className="mb-6">
      <PanelHeader
        title={<span className="flex items-center gap-2"><Sheet className="size-4 text-muted-foreground" strokeWidth={1.5} />Sales team&apos;s Google Sheet</span>}
        right={
          <div className="flex items-center gap-2">
            {admin && (
              <Button size="sm" variant="ghost" onClick={() => setOpen((o) => (o === "settings" ? "none" : "settings"))}>
                <Settings2 strokeWidth={1.5} />
                Settings
              </Button>
            )}
            {admin && configured && (
              <Button size="sm" variant="ghost" disabled={busy || st.running || st.standby} onClick={recheck}
                title="Mark leads we already have as Handled by sales if their row is green, yellow or red. Imports nothing.">
                <Palette strokeWidth={1.5} />
                Re-check colours
              </Button>
            )}
            {admin && configured && (
              <Button size="sm" variant="secondary" disabled={busy || st.running || st.standby} onClick={syncNow}>
                <RefreshCw strokeWidth={1.5} className={cn(busy && "animate-spin")} />
                Sync now
              </Button>
            )}
          </div>
        }
      />
      <div className="space-y-2 px-4 py-3 text-[13px]">
        {!configured ? (
          <p className="text-muted-foreground">Not set up yet. Open Settings and paste the sheet link.</p>
        ) : (
          <>
            <p className="text-muted-foreground">
              {last?.at ? <>Last {last.colors_only ? "colour check" : "sync"} {timeAgo(last.at)} ({formatDateTime(last.at)}{last.trigger === "auto" ? ", automatic" : ""}). </> : "Never synced yet. "}
              {cfg.enabled ? (st.next_sync_at ? `Next: every ${cfg.sync_minutes} min.` : `Every ${cfg.sync_minutes} min.`) : "Automatic sync is off."}
              {" "}Importing never starts the dialer.
            </p>
            {last && !last.ok && <p className="text-danger">Last sync failed: {last.error}</p>}
            {last?.ok && <Summary s={last} onDetails={() => setOpen((o) => (o === "details" ? "none" : "details"))} open={open === "details"} />}
            {open === "details" && last?.ok && <Details s={last} />}
            {st.results && (
              <p className={cn("text-[12px]", st.results.ok ? "text-faint" : "text-danger")}>
                Results sheet: {st.results.ok ? `last written ${timeAgo(st.results.at)} (${st.results.written} lead${st.results.written === 1 ? "" : "s"})` : st.results.error}
              </p>
            )}
            {st.standby && <p className="text-[12px] text-warning">Standby: this copy doesn&apos;t read or write Google Sheets.</p>}
          </>
        )}
        {!st.key.ok && admin && <p className="text-[12px] text-warning">{st.key.detail}</p>}
      </div>
      {open === "settings" && admin && <SheetSettingsForm st={st} onSaved={(x) => { setSt(x); setOpen("none") }} />}
    </Panel>
  )
}

function Summary({ s, onDetails, open }: { s: SheetSyncSummary; onDetails: () => void; open: boolean }) {
  const h = s.handled ?? { new: 0, existing: 0, skipped: 0 }
  const items: [string, number, string?][] = s.colors_only
    ? [
        ["existing leads now handled by sales", h.existing, "text-foreground"],
        ["rows not imported yet (Sync now brings them in)", s.not_imported ?? 0],
      ]
    : [
        ["new", s.new ?? 0, "text-signal"],
        ["duplicates", s.duplicates ?? 0],
        ["handled by sales", h.new + h.existing + h.skipped, "text-foreground"],
        ["skipped", s.skipped_count ?? 0, (s.skipped_count ?? 0) ? "text-warning" : undefined],
      ]
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      {items.map(([label, n, tone]) => (
        <span key={label} className="text-[12px] text-muted-foreground">
          <span className={cn("font-mono text-[13px] tnum", tone)}>{n}</span> {label}
        </span>
      ))}
      {!!s.missing_tabs?.length && <span className="text-[12px] text-warning">Tab not found: {s.missing_tabs.join(", ")}</span>}
      <button type="button" onClick={onDetails} className="flex items-center gap-1 text-[12px] text-muted-foreground hover:text-foreground">
        Details <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} strokeWidth={1.5} />
      </button>
    </div>
  )
}

function Details({ s }: { s: SheetSyncSummary }) {
  const h = s.handled ?? { new: 0, existing: 0, skipped: 0 }
  const where = (tab: string, row: number | null) => `${tab}${row ? `, row ${row}` : ""}`
  return (
    <div className="space-y-3 rounded-md border border-border bg-surface-2 p-3 text-[12px]">
      <p className="text-muted-foreground">
        Rows read: {Object.entries(s.tabs ?? {}).map(([t, n]) => `${t} ${n}`).join(" · ")}. Handled by sales: {h.new} new (never called),
        {" "}{h.existing} existing lead{h.existing === 1 ? "" : "s"} stopped, {h.skipped} not imported.
        {!!s.filled && ` Filled empty fields on ${s.filled} existing lead${s.filled === 1 ? "" : "s"}.`}
        {!!s.moved && ` ${s.moved} lead${s.moved === 1 ? "" : "s"} moved to a new row number.`}
      </p>
      {!!s.colors?.length && <ColorTable s={s} />}
      {Object.entries(s.color_rules ?? {}).map(([tab, r]) => (
        <p key={tab} className="text-warning">
          {tab}: {r.conditional ? `${r.conditional} conditional-formatting rule${r.conditional === 1 ? "" : "s"}` : ""}
          {r.conditional && r.banded ? " and " : ""}{r.banded ? `${r.banded} alternating-colour range${r.banded === 1 ? "" : "s"}` : ""}.
          {" "}Colours from those aren&apos;t seen; paint the row (fill colour) to mark it.
        </p>
      ))}
      {!!s.skipped_groups?.length && (
        <div>
          <div className="font-medium text-muted-foreground">Why rows were skipped</div>
          <ul className="mt-1 space-y-0.5">
            {s.skipped_groups.map((g) => (
              <li key={g.group} className="flex gap-2"><span className="w-10 shrink-0 text-right font-mono text-faint tnum">{g.count}</span><span>{g.reason}</span></li>
            ))}
          </ul>
        </div>
      )}
      {!!s.marked_handled?.length && (
        <List title="Existing leads now handled by sales" rows={s.marked_handled.map((x) => [where(x.tab, x.row), `${x.name || "lead"} (${x.color})`])} />
      )}
      {!!s.skipped?.length && <List title="Skipped" rows={s.skipped.map((x) => [where(x.tab, x.row), `${x.name ? `${x.name}: ` : ""}${x.reason}`])} />}
      {!!s.duplicate_rows?.length && <List title="Duplicates" rows={s.duplicate_rows.map((x) => [where(x.tab, x.row), `${x.name || x.phone}: ${x.reason}`])} />}
    </div>
  )
}

function ColorTable({ s }: { s: SheetSyncSummary }) {
  const tabs = [...new Set((s.colors ?? []).map((c) => c.tab))]
  return (
    <div className="space-y-2">
      <div className="font-medium text-muted-foreground">Row colours found</div>
      {tabs.map((tab) => (
        <div key={tab} className="overflow-x-auto">
          <div className="mb-1 text-faint">
            {tab}: {(s.row_colors?.[tab] ?? []).map((r) => `${r.label} ${r.rows}`).join(" · ")}
          </div>
          <table className="w-full min-w-[420px] text-left">
            <thead>
              <tr className="border-b border-border text-faint">
                <th className="h-7 pr-3 font-normal">Colour</th>
                <th className="h-7 pr-3 text-right font-normal">Rows</th>
                <th className="h-7 pr-3 font-normal">Read as</th>
                <th className="h-7 font-normal">Called?</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(s.colors ?? []).filter((c) => c.tab === tab).map((c) => (
                <tr key={c.hex || "none"}>
                  <td className="h-7 pr-3">
                    <span className="flex items-center gap-2">
                      <span className="size-3 shrink-0 rounded-sm border border-black/15" style={{ background: c.hex || "transparent" }} />
                      <span className="font-mono">{c.hex || "no fill"}</span>
                    </span>
                  </td>
                  <td className="h-7 pr-3 text-right font-mono tnum">{c.rows}</td>
                  <td className="h-7 pr-3">{c.label}</td>
                  <td className={cn("h-7", c.handled ? "text-foreground" : "text-faint")}>{c.handled ? "Never (handled by sales)" : "Yes"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      <p className="text-faint">Rows with this colour in any cell, so a row can count under two colours (for example white A–B and red after).</p>
    </div>
  )
}

function List({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div>
      <div className="font-medium text-muted-foreground">{title} ({rows.length})</div>
      <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto">
        {rows.map(([a, b], i) => (
          <li key={i} className="flex gap-2"><span className="w-56 shrink-0 truncate text-faint">{a}</span><span className="min-w-0 break-words">{b}</span></li>
        ))}
      </ul>
    </div>
  )
}

function SheetSettingsForm({ st, onSaved }: { st: SheetsStatus; onSaved: (x: SheetsStatus) => void }) {
  const [v, setV] = useState<SheetSettings>(st.settings)
  const [tabs, setTabs] = useState(st.settings.tabs.join("\n"))
  const [saving, setSaving] = useState(false)
  const set = (k: Partial<SheetSettings>) => setV((x) => ({ ...x, ...k }))

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    try {
      onSaved(await api.saveSheets({ ...v, tabs: tabs.split("\n").map((t) => t.trim()).filter(Boolean) }))
      toast.success("Saved")
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't save")
    } finally {
      setSaving(false)
    }
  }
  const rewrite = async () => {
    try {
      const r = await api.rewriteResults()
      toast.success(`Writing ${r.queued} lead${r.queued === 1 ? "" : "s"} to the results sheet`)
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't start")
    }
  }

  const field = "text-[12px] font-medium text-muted-foreground"
  return (
    <form onSubmit={save} className="space-y-5 border-t border-border p-4 text-[13px]">
      <div className="rounded-md border border-border bg-surface-2 p-3 text-[12px] leading-5 text-muted-foreground">
        {st.key.ok ? (
          <>Our Google account: <code className="font-mono text-foreground">{st.key.client_email}</code>. Share the sales sheet with it as
            {" "}<b>Viewer</b>, and your results sheet as <b>Editor</b>.</>
        ) : (
          <>No Google key yet: {st.key.detail}</>
        )}
      </div>

      <div className="space-y-3">
        <div className="font-medium">Sales team&apos;s sheet (read only)</div>
        <label className="block space-y-1.5">
          <span className={field}>Sheet link</span>
          <Input value={v.sales_sheet} onChange={(e) => set({ sales_sheet: e.target.value })} placeholder="https://docs.google.com/spreadsheets/d/…" />
        </label>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <label className="block space-y-1.5">
            <span className={field}>Tabs to read (one per line)</span>
            <Textarea rows={3} value={tabs} onChange={(e) => setTabs(e.target.value)} className="font-mono text-[12px]" />
          </label>
          <div className="space-y-3">
            <label className="flex items-center justify-between gap-3">
              <span className="text-[13px]">Sync automatically</span>
              <Switch on={v.enabled} onChange={(x) => set({ enabled: x })} label="Sync automatically" />
            </label>
            <label className="block space-y-1.5">
              <span className={field}>Every (minutes)</span>
              <Input type="number" min={5} max={1440} value={v.sync_minutes} onChange={(e) => set({ sync_minutes: Number(e.target.value) })} className="w-28 font-mono tnum" />
            </label>
          </div>
        </div>
        <div className="space-y-1.5">
          <span className={field}>Row colours that mean &quot;handled by sales&quot; (never called; any shade)</span>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Handled colours">
            {st.colors.map((c) => {
              const always = st.always_handled.includes(c)
              const on = always || v.handled_colors.includes(c)
              return (
                <button key={c} type="button" aria-pressed={on} disabled={always}
                  title={always ? "Always: green, yellow and red rows are never called" : undefined}
                  onClick={() => set({ handled_colors: on ? v.handled_colors.filter((x) => x !== c) : [...v.handled_colors, c] })}
                  className={cn("flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-[12px] capitalize transition-colors duration-[120ms]",
                    on ? "border-line-strong bg-surface text-foreground" : "border-border text-faint hover:bg-surface",
                    always && "cursor-default")}>
                  <span className="size-3 rounded-sm border border-black/10" style={{ background: SWATCH[c] }} />
                  {c}
                  {always && <span className="text-[11px] normal-case text-faint">always</span>}
                </button>
              )
            })}
          </div>
          <p className="text-[12px] text-faint">Green, yellow and red rows (red = wrong number etc.) are always handled by sales. Add other colours if needed.</p>
        </div>
        <label className="block space-y-1.5">
          <span className={field}>New rows in those colours</span>
          <Select value={v.handled_action} onValueChange={(x) => set({ handled_action: x as SheetSettings["handled_action"] })}>
            <SelectTrigger className="w-72" aria-label="New handled rows"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="import">Import as &quot;Handled by sales&quot; (never called)</SelectItem>
              <SelectItem value="skip">Don&apos;t import them</SelectItem>
            </SelectContent>
          </Select>
        </label>
      </div>

      <div className="space-y-3">
        <div className="font-medium">Results sheet (ours, for the sales team)</div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <label className="block space-y-1.5 sm:col-span-3">
            <span className={field}>Sheet link (a different sheet you own)</span>
            <Input value={v.results_sheet} onChange={(e) => set({ results_sheet: e.target.value })} placeholder="https://docs.google.com/spreadsheets/d/…" />
          </label>
          <label className="block space-y-1.5">
            <span className={field}>Tab</span>
            <Input value={v.results_tab} onChange={(e) => set({ results_tab: e.target.value })} />
          </label>
          <label className="block space-y-1.5 sm:col-span-2">
            <span className={field}>Times in</span>
            <Select value={v.results_timezone} onValueChange={(x) => set({ results_timezone: x })}>
              <SelectTrigger aria-label="Time zone"><SelectValue /></SelectTrigger>
              <SelectContent>
                {Object.entries(st.result_zones).map(([z, n]) => <SelectItem key={z} value={z}>{n}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        {st.settings.results_sheet && <Button type="button" variant="secondary" onClick={rewrite}>Write all results again</Button>}
      </div>
    </form>
  )
}
