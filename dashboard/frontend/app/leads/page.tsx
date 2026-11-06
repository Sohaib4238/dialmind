"use client"

import { Ban, ChevronRight, FileUp, Phone, Search, ShieldOff, Upload, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useRouter } from "next/navigation"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { EmptyState, PageHeader, Panel, PanelHeader, SkeletonLine } from "@/components/common"
import { DialerPanel } from "@/components/dialer-panel"
import { SheetSyncPanel } from "@/components/sheet-sync-panel"
import { formatInZone } from "@/components/outcome"
import { LEAD_STATUS_LABEL, LEAD_STATUSES, LeadStatusTag, RARE_STATUSES } from "@/components/lead-status"
import { useIsAdmin } from "@/components/login-gate"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, ApiError } from "@/lib/api"
import { formatPhone, timeAgo } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { ImportPreview, ImportRow, Lead, LeadCounts, LeadStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

const ALL = "__all__"
const EASE = [0.22, 1, 0.36, 1] as const
const COLS = "grid grid-cols-[72px_minmax(180px,1.4fr)_150px_minmax(120px,1fr)_minmax(100px,0.8fr)_170px_64px_110px_84px] items-center gap-4"
const LIMIT = 2000
const SORTS = { sheet: "Sheet order", recent: "Recent activity" } as const
type Sort = keyof typeof SORTS

/** "(Sales) leads" -> "Sales"; a tab name without brackets, like "Tab A", stays as it is */
const tabLabel = (tab: string) => tab.match(/\(([^)]+)\)/)?.[1] ?? tab

const TILES: { key: string; label: string; status?: LeadStatus }[] = [
  { key: "total", label: "Total" },
  { key: "called", label: "Called" },
  { key: "interested", label: "Interested", status: "interested" },
  { key: "callback", label: "Callback", status: "callback" },
  { key: "needs_review", label: "Needs review", status: "needs_review" },
  { key: "do_not_contact", label: "Do not contact", status: "do_not_contact" },
]

export default function LeadsPage() {
  const router = useRouter()
  const admin = useIsAdmin()
  const [leads, setLeads] = useState<Lead[] | null>(null)
  const [counts, setCounts] = useState<LeadCounts | null>(null)
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState("")
  const [status, setStatus] = useState<string>(ALL)
  const [sort, setSort] = useState<Sort>("sheet")
  const [panel, setPanel] = useState<"none" | "import" | "dnc">("none")
  const [busyId, setBusyId] = useState<number | null>(null)
  const reqId = useRef(0)

  const load = useCallback(async () => {
    const id = ++reqId.current
    try {
      const d = await api.leads({ q: query.trim(), status: status === ALL ? "" : status, sort, limit: LIMIT })
      if (id !== reqId.current) return // a newer search is in flight
      setLeads(d.leads)
      setCounts(d.counts)
      setTotal(d.total)
    } catch (e) {
      if (id === reqId.current) {
        setLeads([])
        toast.error(e instanceof ApiError ? e.message : "Couldn't load leads")
      }
    }
  }, [query, status, sort])

  useEffect(() => {
    const t = setTimeout(load, query ? 200 : 0) // debounce typing
    return () => clearTimeout(t)
  }, [load, query])

  useLiveStream((msg) => {
    if (msg.type === "leads") load()
    if (msg.type === "lead") {
      setLeads((prev) => prev?.map((l) => (l.id === msg.lead.id ? msg.lead : l)) ?? prev)
      api.leadCounts().then(setCounts).catch(() => {})
    }
  })

  const callLead = async (lead: Lead) => {
    setBusyId(lead.id)
    try {
      await api.callLead(lead.id)
      toast.success(`Calling ${lead.name || formatPhone(lead.phone)}`, {
        action: { label: "Watch live", onClick: () => router.push("/") },
      })
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't start the call")
    } finally {
      setBusyId(null)
    }
  }

  const toggleDnc = async (lead: Lead) => {
    setBusyId(lead.id)
    try {
      const updated = await api.setDnc(lead.id, !lead.dnc, lead.dnc ? "" : "Marked from the leads list")
      setLeads((prev) => prev?.map((l) => (l.id === updated.id ? updated : l)) ?? prev)
      toast.success(updated.dnc ? `${lead.name || formatPhone(lead.phone)} won't be called or texted` : "Removed from Do not contact", {
        action: { label: "Undo", onClick: () => api.setDnc(lead.id, lead.dnc, lead.dnc_reason).then(load) },
      })
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't update the lead")
    } finally {
      setBusyId(null)
    }
  }

  const statusOptions = useMemo(
    () => LEAD_STATUSES.map((s) => ({ s, n: counts?.by_status[s] ?? 0 })).filter(({ s, n }) => n > 0 || !RARE_STATUSES.includes(s)),
    [counts],
  )
  const tileValue = (key: string) => (key === "total" ? counts?.total : key === "called" ? counts?.called : counts?.by_status[key as LeadStatus])

  return (
    <>
      <PageHeader
        title="Leads"
        description="Every lead and where it stands. Turn on the auto-dialer to call them one at a time."
        actions={admin && (
          <>
            <Button variant="ghost" size="sm" onClick={() => setPanel((p) => (p === "dnc" ? "none" : "dnc"))}>
              <Ban strokeWidth={1.5} />
              Add to Do not contact
            </Button>
            <Button size="sm" onClick={() => setPanel((p) => (p === "import" ? "none" : "import"))}>
              <Upload strokeWidth={1.75} />
              Import CSV
            </Button>
          </>
        )}
      />

      <DialerPanel />

      <SheetSyncPanel admin={admin} />

      {/* Counters */}
      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {TILES.map((t) => {
          const selected = !!t.status && status === t.status
          return (
            <button
              key={t.key}
              onClick={() => t.status && setStatus(selected ? ALL : t.status)}
              disabled={!t.status}
              className={cn(
                "rounded-lg border bg-surface p-3 text-left transition-colors duration-[120ms]",
                t.status ? "hover:border-line-strong active:scale-[0.99]" : "cursor-default",
                selected ? "border-line-strong bg-surface-2" : "border-border",
              )}
            >
              <div className="text-[12px] text-faint">{t.label}</div>
              {counts ? (
                <motion.div key={tileValue(t.key)} initial={{ opacity: 0.4, y: 2 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.18, ease: EASE }}
                  className={cn("mt-1 font-mono text-[22px] leading-7 font-medium tracking-[-0.02em] tnum", t.key === "do_not_contact" && (tileValue(t.key) ?? 0) > 0 && "text-danger")}>
                  {tileValue(t.key) ?? 0}
                </motion.div>
              ) : (
                <SkeletonLine className="mt-2 h-6 w-10" />
              )}
            </button>
          )
        })}
      </div>

      <AnimatePresence initial={false}>
        {panel === "import" && <ImportPanel key="import" onClose={() => setPanel("none")} onDone={() => { setPanel("none"); load() }} />}
        {panel === "dnc" && <DncPanel key="dnc" onClose={() => setPanel("none")} onDone={() => load()} />}
      </AnimatePresence>

      {/* Table */}
      <Panel>
        <div className="flex flex-wrap items-center gap-3 border-b border-border p-4">
          <div className="relative min-w-[220px] flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" strokeWidth={1.5} />
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search name, service, location, phone…" className="pl-9" aria-label="Search leads" />
          </div>
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="w-52" aria-label="Status">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All statuses</SelectItem>
              {statusOptions.map(({ s, n }) => (
                <SelectItem key={s} value={s}>
                  {LEAD_STATUS_LABEL[s]} <span className="text-faint tnum">{n}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={(x) => setSort(x as Sort)}>
            <SelectTrigger className="w-44" aria-label="Sort">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(SORTS) as Sort[]).map((k) => <SelectItem key={k} value={k}>{SORTS[k]}</SelectItem>)}
            </SelectContent>
          </Select>
          {(query || status !== ALL) && (
            <Button variant="ghost" size="sm" onClick={() => { setQuery(""); setStatus(ALL) }}>
              <X strokeWidth={1.5} />
              Clear
            </Button>
          )}
          <span className="ml-auto text-[12px] text-faint tnum">
            {leads ? (leads.length < total ? `Showing ${leads.length} of ${total} leads` : `${total} lead${total === 1 ? "" : "s"}`) : ""}
          </span>
        </div>

        <div className="overflow-x-auto">
          <div className="min-w-[1160px]">
            <div className={`${COLS} h-10 border-b border-border px-4 text-[12px] text-faint`}>
              <span title="The sales sheet's tab and row">Sheet</span>
              <span>Lead</span>
              <span>Phone</span>
              <span>Service</span>
              <span>Location</span>
              <span>Status</span>
              <span className="text-right">Tries</span>
              <span>Last call</span>
              <span />
            </div>
            {leads === null ? (
              <div className="divide-y divide-border">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className={`${COLS} h-12 px-4`}>
                    <SkeletonLine className="w-12" />
                    <SkeletonLine className="w-40" />
                    <SkeletonLine className="w-28" />
                    <SkeletonLine className="w-24" />
                    <SkeletonLine className="w-16" />
                    <SkeletonLine className="w-20" />
                    <SkeletonLine className="ml-auto w-6" />
                    <SkeletonLine className="w-16" />
                    <span />
                  </div>
                ))}
              </div>
            ) : leads.length === 0 ? (
              counts?.total ? (
                <EmptyState>No leads match these filters.</EmptyState>
              ) : (
                <EmptyState action={admin ? <Button size="sm" onClick={() => setPanel("import")}><Upload strokeWidth={1.75} />Import CSV</Button> : undefined}>
                  No leads yet. Import a CSV with a phone column (name, service required, business and job description, location are optional).
                </EmptyState>
              )
            ) : (
              <ul className="divide-y divide-border">
                <AnimatePresence initial={false}>
                  {leads.map((l, i) => (
                    <motion.li key={l.id} layout="position" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
                      transition={{ duration: 0.18, delay: Math.min(i, 12) * 0.012 }}
                      className={`${COLS} group h-12 cursor-pointer px-4 text-[13px] transition-colors duration-[120ms] hover:bg-surface-2`}
                      onClick={() => router.push(`/leads/${l.id}`)}>
                      {l.sheet_tab ? (
                        <span className="truncate text-[12px] text-muted-foreground" title={`${l.sheet_tab}${l.sheet_row ? `, row ${l.sheet_row}` : ""}`}>
                          {tabLabel(l.sheet_tab)} <span className="font-mono text-faint tnum">{l.sheet_row ?? ""}</span>
                        </span>
                      ) : (
                        <span className="text-[12px] text-faint">–</span>
                      )}
                      <div className="min-w-0">
                        <div className="truncate font-medium">{l.name || <span className="text-faint">No name</span>}</div>
                        <div className="truncate text-[12px] text-faint">{[l.business, l.lead_source].filter(Boolean).join(" · ") || "–"}</div>
                      </div>
                      <span className="font-mono text-[12px] tnum">{formatPhone(l.phone)}</span>
                      <span className="truncate text-muted-foreground" title={l.service_required || undefined}>{l.service_required || "–"}</span>
                      <span className="truncate text-muted-foreground" title={l.location || undefined}>{l.location || l.city || "–"}</span>
                      <span className="min-w-0">
                        <LeadStatusTag status={l.status} />
                        {l.status === "callback" && l.callback_at && (
                          <div className="mt-0.5 truncate text-[11px] text-faint" title={l.callback_note || undefined}>
                            {formatInZone(l.callback_at, l.timezone)}
                          </div>
                        )}
                      </span>
                      <span className="text-right font-mono text-muted-foreground tnum">{l.attempts}</span>
                      <span className="text-[12px] text-muted-foreground tnum">{l.last_call_at ? timeAgo(l.last_call_at) : "–"}</span>
                      <span className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
                        {admin && (
                          <>
                            <Button variant="ghost" size="icon-sm" title={l.dnc ? "On the Do-Not-Contact list" : "Call this lead"} aria-label="Call this lead"
                              disabled={l.dnc || busyId === l.id} onClick={() => callLead(l)}>
                              <Phone strokeWidth={1.5} />
                            </Button>
                            <Button variant="ghost" size="icon-sm" title={l.dnc ? "Remove from Do not contact" : "Mark Do not contact"}
                              aria-label={l.dnc ? "Remove from Do not contact" : "Mark Do not contact"} disabled={busyId === l.id}
                              className={cn(l.dnc && "text-danger hover:text-danger")} onClick={() => toggleDnc(l)}>
                              {l.dnc ? <ShieldOff strokeWidth={1.5} /> : <Ban strokeWidth={1.5} />}
                            </Button>
                          </>
                        )}
                        <ChevronRight className="size-4 text-faint transition-transform duration-[120ms] group-hover:translate-x-0.5" strokeWidth={1.5} />
                      </span>
                    </motion.li>
                  ))}
                </AnimatePresence>
              </ul>
            )}
          </div>
        </div>
      </Panel>
    </>
  )
}

function Collapsible({ children }: { children: React.ReactNode }) {
  return (
    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.24, ease: EASE }} className="overflow-hidden">
      <div className="pb-6">{children}</div>
    </motion.div>
  )
}

function ImportPanel({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<{ name: string; text: string } | null>(null)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [tab, setTab] = useState<"valid" | "invalid" | "duplicates">("valid")
  const [busy, setBusy] = useState(false)
  const [drag, setDrag] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const choose = async (f: File | undefined) => {
    if (!f) return
    if (f.size > 5_000_000) return toast.error("The file is too big (max 5 MB)")
    const text = await f.text()
    setBusy(true)
    try {
      const p = await api.previewImport(text, f.name)
      setFile({ name: f.name, text })
      setPreview(p)
      setTab(p.counts.valid ? "valid" : p.counts.invalid ? "invalid" : "duplicates")
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't read the file")
    } finally {
      setBusy(false)
    }
  }

  const doImport = async () => {
    if (!file || !preview) return
    setBusy(true)
    try {
      const r = await api.importLeads(file.text, file.name)
      toast.success(`Imported ${r.imported} lead${r.imported === 1 ? "" : "s"}`, {
        description: [
          r.updated ? `Filled empty fields on ${r.updated} existing lead${r.updated === 1 ? "" : "s"}.` : "",
          r.not_a_fit ? `${r.not_a_fit} marked Not a fit (service we don't offer).` : "",
        ].filter(Boolean).join(" ") || undefined,
      })
      onDone()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Import failed")
    } finally {
      setBusy(false)
    }
  }

  const rows: ImportRow[] = preview ? preview[tab] : []

  return (
    <Collapsible>
      <Panel>
        <PanelHeader title="Import leads from CSV" right={<Button variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}><X strokeWidth={1.5} /></Button>} />
        {!preview ? (
          <div className="p-4">
            <button
              onClick={() => input.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => { e.preventDefault(); setDrag(false); choose(e.dataTransfer.files[0]) }}
              className={cn(
                "flex w-full flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-10 text-center transition-colors duration-[120ms]",
                drag ? "border-signal bg-signal-soft" : "border-line-strong hover:bg-surface-2",
              )}
            >
              <FileUp className={cn("size-6", drag ? "text-signal" : "text-muted-foreground")} strokeWidth={1.5} />
              <span className="text-[13px] font-medium">{busy ? "Checking the file…" : "Drop a CSV here, or click to choose"}</span>
              <span className="max-w-[520px] text-[12px] text-faint">
                Columns: phone (required), name, service required, business description, job description, location (or city, state, zip),
                business, notes. Empty cells are fine. Other columns are kept in the notes. Numbers are cleaned to US format;
                you&apos;ll see what will be imported before anything is saved.
              </span>
            </button>
            <input ref={input} type="file" accept=".csv,text/csv" className="hidden" onChange={(e) => choose(e.target.files?.[0])} />
          </div>
        ) : (
          <div>
            <div className="flex flex-wrap items-center gap-3 border-b border-border p-4">
              <span className="text-[13px] font-medium">{preview.filename}</span>
              <span className="text-[12px] text-faint tnum">{preview.total_rows} rows</span>
              <div className="ml-auto flex gap-2">
                <Button variant="ghost" size="sm" onClick={() => { setPreview(null); setFile(null) }}>Choose another file</Button>
                <Button size="sm" disabled={busy || !(preview.counts.valid || preview.counts.updates)} onClick={doImport}>
                  {busy ? "Importing…" : preview.counts.valid || !preview.counts.updates
                    ? `Import ${preview.counts.valid} lead${preview.counts.valid === 1 ? "" : "s"}`
                    : `Fill fields on ${preview.counts.updates} lead${preview.counts.updates === 1 ? "" : "s"}`}
                </Button>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-3 p-4">
              {([["valid", "Valid", "text-signal"], ["invalid", "Invalid", "text-danger"], ["duplicates", "Duplicates", "text-warning"]] as const).map(([k, label, tone]) => (
                <button key={k} onClick={() => setTab(k)}
                  className={cn("relative rounded-lg border p-3 text-left transition-colors duration-[120ms]", tab === k ? "border-line-strong bg-surface-2" : "border-border hover:bg-surface-2/60")}>
                  <div className="text-[12px] text-faint">{label}</div>
                  <div className={cn("mt-1 font-mono text-[22px] leading-7 font-medium tnum", preview.counts[k] ? tone : "text-faint")}>{preview.counts[k]}</div>
                  {tab === k && <motion.span layoutId="import-tab" className="absolute inset-x-3 -bottom-px h-px bg-foreground" transition={{ type: "spring", stiffness: 420, damping: 34 }} />}
                </button>
              ))}
            </div>
            <div className="px-4 pb-2 text-[12px] text-faint">
              Matched columns: {Object.entries(preview.columns).map(([f, h]) => `${h} → ${f}`).join(" · ")}
              {preview.extra_columns.length > 0 && <> · Kept in notes: {preview.extra_columns.join(", ")}</>}
            </div>
            {rows.length === 0 ? (
              <EmptyState>{tab === "valid" ? "No valid rows." : tab === "invalid" ? "No invalid rows." : "No duplicates."}</EmptyState>
            ) : (
              <div className="max-h-[320px] overflow-y-auto border-t border-border">
                <table className="w-full text-[12px]">
                  <thead className="sticky top-0 bg-surface">
                    <tr className="border-b border-border text-left text-faint">
                      <th className="h-8 w-16 px-4 font-normal">Row</th>
                      <th className="h-8 px-4 font-normal">Phone</th>
                      <th className="h-8 px-4 font-normal">Name</th>
                      <th className="h-8 px-4 font-normal">{tab === "valid" ? "Service · Location" : "Why"}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {rows.slice(0, 200).map((r) => (
                      <tr key={`${tab}-${r.row}`}>
                        <td className="h-8 px-4 font-mono text-faint tnum">{r.row}</td>
                        <td className="h-8 px-4 font-mono tnum">{r.phone_display || r.phone || <span className="text-faint">(empty)</span>}</td>
                        <td className="h-8 px-4">{r.name || <span className="text-faint">–</span>}</td>
                        <td className={cn("h-8 px-4", tab === "invalid" ? "text-danger" : tab === "duplicates" ? "text-warning" : "text-muted-foreground")}>
                          {tab === "valid" ? [r.service_required, r.location].filter(Boolean).join(" · ") || "–" : r.reason}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </Panel>
    </Collapsible>
  )
}

function DncPanel({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [phone, setPhone] = useState("")
  const [reason, setReason] = useState("")
  const [busy, setBusy] = useState(false)

  const add = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!phone.trim()) return
    setBusy(true)
    try {
      const lead = await api.addDnc(phone, reason)
      toast.success(`${formatPhone(lead.phone)} is on the Do-Not-Contact list`, { description: "It will never be called or texted." })
      setPhone("")
      setReason("")
      onDone()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't add the number")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Collapsible>
      <Panel>
        <PanelHeader title="Add a number to Do not contact" right={<Button variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}><X strokeWidth={1.5} /></Button>} />
        <form onSubmit={add} className="flex flex-wrap items-end gap-3 p-4">
          <label className="space-y-1.5">
            <span className="text-[12px] font-medium text-muted-foreground">Phone number</span>
            <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="(305) 555-0123" className="w-52 font-mono tnum" autoFocus />
          </label>
          <label className="min-w-[240px] flex-1 space-y-1.5">
            <span className="text-[12px] font-medium text-muted-foreground">Reason (optional)</span>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Asked not to be contacted" />
          </label>
          <Button type="submit" variant="destructive" disabled={busy || !phone.trim()}>
            <Ban strokeWidth={1.5} />
            {busy ? "Adding…" : "Add to Do not contact"}
          </Button>
          <p className="w-full text-[12px] text-faint">Works for any US number, even if it isn&apos;t a lead yet. Calls to it are refused.</p>
        </form>
      </Panel>
    </Collapsible>
  )
}
