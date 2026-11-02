"use client"

import { Plus, RotateCcw, Trash2 } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { Kbd, PageHeader, Panel, SkeletonLine, Tag } from "@/components/common"
import { SyncBadge, syncFromSave } from "@/components/sync-badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, ApiError } from "@/lib/api"
import type { FieldSpec, SettingsPayload, SyncStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

type Values = Record<string, unknown>
type Errors = Record<string, string>

const MULTILINE_KEYS = new Set(["speaking_style", "main_script", "ai_receptionist_script", "message", "body", "offered", "not_offered", "scheduling_instructions", "rules"])

/** Textarea that grows with its content (CSS field-sizing isn't supported everywhere). */
function AutoTextarea(props: React.ComponentProps<typeof Textarea>) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = "auto"
    el.style.height = `${el.scrollHeight + 2}px`
  }, [props.value])
  return <Textarea ref={ref} {...props} />
}

function getIn(obj: unknown, path: (string | number)[]): unknown {
  return path.reduce<unknown>((o, k) => (o == null ? o : (o as Record<string | number, unknown>)[k]), obj)
}

function setIn<T>(obj: T, path: (string | number)[], value: unknown): T {
  if (!path.length) return value as T
  const [head, ...rest] = path
  const copy: any = Array.isArray(obj) ? [...obj] : { ...(obj as object) }
  copy[head] = setIn(copy[head], rest, value)
  return copy
}

export default function SettingsPage() {
  const [data, setData] = useState<SettingsPayload | null>(null)
  const [values, setValues] = useState<Values>({})
  const [errors, setErrors] = useState<Errors>({})
  const [saving, setSaving] = useState(false)
  const [sync, setSync] = useState<SyncStatus | null>(null)

  const load = useCallback(async () => {
    try {
      const d = await api.settings()
      setData(d)
      setValues(d.values)
      setErrors({})
      setSync(d.sync ?? null)
      if (d.file_error) toast.error("The settings file on disk is invalid", { description: d.file_error })
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't load settings")
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const dirty = useMemo(() => !!data && JSON.stringify(values) !== JSON.stringify(data.values), [data, values])

  const save = useCallback(async () => {
    if (!data || !dirty || saving) return
    setSaving(true)
    try {
      const res = await api.saveSettings(values, data.version)
      setData({ ...data, values, version: res.version })
      setErrors({})
      const st = syncFromSave(res.cartesia)
      setSync(st)
      if (st.in_sync) toast.success("Saved · Synced to Cartesia ✓", { description: `Applies to the next call. Backup: ${res.backup}` })
      else toast.warning("Saved, but not synced to Cartesia", { description: st.detail, duration: 10000 })
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const list = ((e.detail as { errors?: { path: string; message: string }[] })?.errors) ?? []
        setErrors(Object.fromEntries(list.map((x) => [x.path, x.message])))
        toast.error(`Not saved: ${list.length} problem${list.length === 1 ? "" : "s"} to fix`, {
          description: "The file on disk was not changed.",
        })
        const first = list[0]?.path
        if (first) document.getElementById(`field-${first}`)?.scrollIntoView({ behavior: "smooth", block: "center" })
      } else if (e instanceof ApiError && e.status === 409) {
        toast.error("Settings changed on disk", {
          description: e.message,
          action: { label: "Reload", onClick: () => load() },
        })
      } else {
        toast.error(e instanceof ApiError ? e.message : "Couldn't save")
      }
    } finally {
      setSaving(false)
    }
  }, [data, dirty, saving, values, load])

  // Ctrl/Cmd+S saves.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault()
        save()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [save])

  // Warn before leaving with unsaved edits.
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener("beforeunload", onBeforeUnload)
    return () => window.removeEventListener("beforeunload", onBeforeUnload)
  }, [dirty])

  const update = (path: (string | number)[], v: unknown) => {
    setValues((prev) => setIn(prev, path, v))
    const key = path.join(".")
    if (errors[key]) setErrors(({ [key]: _, ...rest }) => rest)
  }

  return (
    <>
      <PageHeader
        title="Instructions"
        description="What the agent says and how it behaves. Saved to config/agent_settings.yaml and used from the next call."
        actions={
          <>
            <SyncBadge sync={sync} className="mr-1" />
            <AnimatePresence>
              {dirty && (
                <motion.span initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="mr-1 text-[12px] text-muted-foreground">
                  Unsaved changes
                </motion.span>
              )}
            </AnimatePresence>
            <Button variant="ghost" size="sm" onClick={() => data && (setValues(data.values), setErrors({}))} disabled={!dirty || saving}>
              <RotateCcw strokeWidth={1.5} />
              Discard
            </Button>
            <Button size="sm" onClick={save} disabled={!dirty || saving}>
              {saving ? "Saving…" : "Save"}
              <span className="flex gap-0.5 opacity-70">
                <Kbd>Ctrl</Kbd>
                <Kbd>S</Kbd>
              </span>
            </Button>
          </>
        }
      />

      {!data ? (
        <div className="space-y-6">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="space-y-2">
              <SkeletonLine className="w-32" />
              <SkeletonLine className="h-20 w-full rounded-md" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-8 lg:grid-cols-[180px_1fr]">
          <nav className="sticky top-8 hidden h-fit flex-col gap-0.5 lg:flex">
            {data.fields.map((f) => (
              <a
                key={f.path}
                href={`#section-${f.path}`}
                className={cn(
                  "flex h-7 items-center justify-between rounded-md px-2 text-[12px] transition-colors duration-[120ms] hover:bg-surface-2 hover:text-foreground",
                  Object.keys(errors).some((k) => k === f.path || k.startsWith(f.path + ".")) ? "text-danger" : "text-muted-foreground",
                )}
              >
                {f.label}
                {f.coming_soon && <span className="size-1 rounded-full bg-faint" title="Coming soon" />}
              </a>
            ))}
          </nav>

          <div className="space-y-4">
            {data.fields.map((f) => (
              <Panel key={f.path} id={`section-${f.path}`} className="scroll-mt-8 p-4">
                <Field spec={f} path={[f.key]} values={values} errors={errors} onChange={update} top />
              </Panel>
            ))}
            <p className="pt-2 text-[12px] text-faint">
              Placeholders you can use in any text: {["caller_name", "company_name", "callback_number", "company_website", "company_address"].map((p) => (
                <code key={p} className="mr-1 font-mono text-muted-foreground">{`{${p}}`}</code>
              ))}
              . Lead details, different on every call and sometimes empty:{" "}
              {["lead_name", "business_name", "service_required", "business_description", "job_description", "location"].map((p) => (
                <code key={p} className="mr-1 font-mono text-muted-foreground">{`{${p}}`}</code>
              ))}
              . <code className="mr-1 font-mono text-muted-foreground">{"{specialist_title}"}</code> is who we hand
              the lead to, from their service (like &quot;our senior web designer&quot;, see Who the lead is handed to).
              Every save keeps a backup in config/backups.
            </p>
          </div>
        </div>
      )}
    </>
  )
}

function FieldLabel({ spec, top }: { spec: FieldSpec; top?: boolean }) {
  return (
    <div className="mb-2 space-y-0.5">
      <div className="flex items-center gap-2">
        <span className={cn(top ? "text-[14px] font-medium" : "text-[12px] font-medium text-muted-foreground")}>{spec.label}</span>
        {spec.coming_soon && top && <Tag>Coming soon</Tag>}
      </div>
      {spec.help && <p className="text-[12px] leading-[18px] text-faint">{spec.help}</p>}
    </div>
  )
}

function ErrorText({ message }: { message?: string }) {
  return (
    <AnimatePresence>
      {message && (
        <motion.p initial={{ opacity: 0, y: -2 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-1.5 text-[12px] text-danger">
          {message}
        </motion.p>
      )}
    </AnimatePresence>
  )
}

function Field({
  spec,
  path,
  values,
  errors,
  onChange,
  top,
}: {
  spec: FieldSpec
  path: (string | number)[]
  values: Values
  errors: Errors
  onChange: (path: (string | number)[], v: unknown) => void
  top?: boolean
}) {
  const key = path.join(".")
  const value = getIn(values, path)
  const error = errors[key]
  const domId = `field-${key}`

  if (spec.kind === "group") {
    return (
      <div id={top ? undefined : domId}>
        <FieldLabel spec={spec} top={top} />
        <div className="mt-3 space-y-4 border-l border-border pl-4">
          {spec.fields!.map((f) => (
            <div key={f.key} className="relative">
              {f.coming_soon && !spec.coming_soon && <div className="absolute top-0 right-0"><Tag>Coming soon</Tag></div>}
              <Field spec={f} path={[...path, f.key]} values={values} errors={errors} onChange={onChange} />
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (spec.kind === "list") {
    const items = (Array.isArray(value) ? value : []) as Record<string, unknown>[]
    return (
      <div>
        <FieldLabel spec={spec} top={top} />
        <ul className="mt-3 space-y-3">
          <AnimatePresence initial={false}>
            {items.map((item, i) => (
              <motion.li
                key={i}
                layout
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, height: 0 }}
                className="grid grid-cols-[1fr_auto] gap-3 rounded-md border border-border bg-background/40 p-3"
              >
                <div className="space-y-3">
                  {spec.item_fields!.map((f) => (
                    <Field key={f.key} spec={f} path={[...path, i, f.key]} values={values} errors={errors} onChange={onChange} />
                  ))}
                </div>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Remove"
                  onClick={() => onChange(path, items.filter((_, j) => j !== i))}
                >
                  <Trash2 strokeWidth={1.5} />
                </Button>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
        <Button
          variant="secondary"
          size="sm"
          className="mt-3"
          onClick={() => onChange(path, [...items, Object.fromEntries(spec.item_fields!.map((f) => [f.key, ""]))])}
        >
          <Plus strokeWidth={1.5} />
          Add
        </Button>
        <ErrorText message={error} />
      </div>
    )
  }

  let control: React.ReactNode
  if (spec.kind === "select") {
    control = (
      <Select value={String(value ?? "")} onValueChange={(v) => onChange(path, v)}>
        <SelectTrigger id={domId} className="w-56" aria-invalid={!!error}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {spec.options!.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  } else if (spec.kind === "multi") {
    control = (
      <ToggleGroup
        id={domId}
        type="multiple"
        variant="outline"
        value={(value as string[]) ?? []}
        onValueChange={(v) => onChange(path, spec.options!.map((o) => o.value).filter((o) => v.includes(o)))}
      >
        {spec.options!.map((o) => (
          <ToggleGroupItem
            key={o.value}
            value={o.value}
            className="h-8 px-3 text-[12px] data-[state=on]:border-line-strong data-[state=on]:bg-surface-2 data-[state=on]:text-foreground"
          >
            {o.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    )
  } else if (spec.kind === "switch") {
    const on = value === true
    control = (
      <button
        id={domId}
        role="switch"
        aria-checked={on}
        onClick={() => onChange(path, !on)}
        className={cn(
          "relative inline-flex h-6 w-11 items-center rounded-full border transition-colors duration-[120ms]",
          on ? "border-transparent bg-signal" : "border-line-strong bg-surface-2",
        )}
      >
        <motion.span
          layout
          transition={{ type: "spring", stiffness: 420, damping: 34 }}
          className={cn("size-4 rounded-full", on ? "ml-[22px] bg-signal-fg" : "ml-[3px] bg-muted-foreground")}
        />
      </button>
    )
  } else if (spec.kind === "number") {
    control = (
      <Input
        id={domId}
        type="number"
        className="w-28 font-mono tnum"
        min={spec.min ?? undefined}
        max={spec.max ?? undefined}
        value={value == null ? "" : String(value)}
        aria-invalid={!!error}
        onChange={(e) => onChange(path, e.target.value === "" ? "" : Number(e.target.value))}
      />
    )
  } else if (spec.kind === "time") {
    control = (
      <Input
        id={domId}
        className="w-28 font-mono tnum"
        placeholder="09:00"
        value={String(value ?? "")}
        aria-invalid={!!error}
        onChange={(e) => onChange(path, e.target.value)}
      />
    )
  } else {
    const text = String(value ?? "")
    const multiline = MULTILINE_KEYS.has(spec.key) || text.includes("\n") || text.length > 90
    control = multiline ? (
      <AutoTextarea
        id={domId}
        value={text}
        aria-invalid={!!error}
        spellCheck
        className="min-h-24 resize-y leading-[20px]"
        onChange={(e) => onChange(path, e.target.value)}
      />
    ) : (
      <Input id={domId} value={text} aria-invalid={!!error} onChange={(e) => onChange(path, e.target.value)} />
    )
  }

  return (
    <div>
      <FieldLabel spec={spec} top={top} />
      {control}
      <ErrorText message={error} />
    </div>
  )
}
