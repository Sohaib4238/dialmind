"use client"

import { Check, LoaderCircle, Play, RefreshCw, RotateCcw, Search, Square, Upload } from "lucide-react"
import { motion } from "motion/react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { EmptyState, PageHeader, Panel, PanelHeader, SkeletonLine, Tag } from "@/components/common"
import { useIsAdmin } from "@/components/login-gate"
import { SyncBadge, syncFromSave } from "@/components/sync-badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, ApiError } from "@/lib/api"
import type { SyncStatus, VoiceInfo, VoiceList, VoicePayload, VoiceSettings } from "@/lib/types"
import { cn } from "@/lib/utils"

const GENDERS = ["All", "Female", "Male", "Neutral"] as const
type Gender = (typeof GENDERS)[number]
const PAGE = 80
const ALL_ACCENTS = "__all__"

/** One shared <audio> for all previews: starting one stops the other. */
function usePreviewPlayer() {
  const audio = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState<string | null>(null)
  const [loading, setLoading] = useState<string | null>(null)

  const stop = useCallback(() => {
    audio.current?.pause()
    setPlaying(null)
  }, [])

  const play = useCallback(
    async (key: string, src: string | (() => Promise<string>), volume = 1) => {
      if (playing === key) return stop()
      audio.current?.pause()
      const a = audio.current ?? new Audio()
      audio.current = a
      setPlaying(null)
      setLoading(key)
      try {
        a.src = typeof src === "string" ? src : await src()
        a.volume = volume
        a.onended = () => setPlaying((p) => (p === key ? null : p))
        await a.play()
        setPlaying(key)
      } catch (e) {
        toast.error(e instanceof ApiError ? e.message : "Couldn't play this preview")
      } finally {
        setLoading(null)
      }
    },
    [playing, stop],
  )

  useEffect(() => () => audio.current?.pause(), [])
  return { playing, loading, play }
}

export default function VoicePage() {
  const [data, setData] = useState<VoicePayload | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [list, setList] = useState<VoiceList | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [listLoading, setListLoading] = useState(false)
  const [query, setQuery] = useState("")
  const [gender, setGender] = useState<Gender>("All")
  const [accent, setAccent] = useState(ALL_ACCENTS)
  const [previewOnly, setPreviewOnly] = useState(false)
  const [shown, setShown] = useState(PAGE)
  const [draft, setDraft] = useState<VoiceSettings | null>(null)
  const [savingId, setSavingId] = useState<string | null>(null)
  const [sync, setSync] = useState<SyncStatus | null>(null)
  const [confirmGen, setConfirmGen] = useState<string | null>(null)
  const [sampleChars, setSampleChars] = useState<number | null>(null)
  const [uploading, setUploading] = useState(false)
  const uploadInput = useRef<HTMLInputElement>(null)
  const isAdmin = useIsAdmin()
  const generated = useRef(new Map<string, string>())
  const player = usePreviewPlayer()

  const loadList = useCallback(async (refresh = false) => {
    setListLoading(true)
    try {
      const l = await api.voices(refresh)
      setList(l)
      setListError(null)
      if (l.stale) toast.warning("Showing the saved voice list", { description: `Cartesia didn't answer: ${l.stale}` })
      else if (refresh) toast.success(`${l.voices.length} Cartesia voices loaded`)
    } catch (e) {
      setListError(e instanceof ApiError ? e.message : "Couldn't load voices")
    } finally {
      setListLoading(false)
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const d = await api.voice()
      setData(d)
      setDraft(d.voice)
      setSync(d.sync)
      setLoadError(null)
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.message : "Couldn't load the voice settings")
    }
  }, [])

  useEffect(() => {
    load()
    loadList()
  }, [load, loadList])

  const active = data?.voice ?? null
  const activeInfo = useMemo(
    () => (active ? (list?.voices.find((v) => v.id === active.voice_id) ?? data?.voice_info ?? null) : null),
    [active, list, data],
  )

  const accents = useMemo(() => {
    const counts = new Map<string, number>()
    for (const v of list?.voices ?? []) for (const a of v.accent.split(", ").filter(Boolean)) counts.set(a, (counts.get(a) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [list])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (list?.voices ?? []).filter(
      (v) =>
        (gender === "All" || v.gender === gender) &&
        (accent === ALL_ACCENTS || v.accent.split(", ").includes(accent)) &&
        (!previewOnly || v.has_preview) &&
        (!q || [v.name, v.id, v.accent, v.tagline, v.description, v.locale].join(" ").toLowerCase().includes(q)),
    )
  }, [list, query, gender, accent, previewOnly])

  useEffect(() => setShown(PAGE), [query, gender, accent, previewOnly])

  const save = useCallback(
    async (next: VoiceSettings, label: string) => {
      if (!data) return
      setSavingId(next.voice_id)
      try {
        const res = await api.saveVoice(next, data.version)
        setData({ ...data, voice: next, version: res.version, voice_info: list?.voices.find((v) => v.id === next.voice_id) ?? data.voice_info })
        setDraft(next)
        const st = syncFromSave(res.cartesia)
        setSync(st)
        if (st.in_sync) toast.success(`${label} · Synced to Cartesia ✓`, { description: "Applies from the next call." })
        else toast.warning(`${label}, but not synced to Cartesia`, { description: st.detail, duration: 10000 })
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          toast.error(e.message)
          load()
        } else if (e instanceof ApiError && e.status === 422) {
          const errs = (e.detail as { errors?: { message: string }[] })?.errors ?? []
          toast.error(errs.map((x) => x.message).join("; ") || "Invalid voice settings")
        } else {
          toast.error(e instanceof ApiError ? e.message : "Couldn't save")
        }
      } finally {
        setSavingId(null)
      }
    },
    [data, list, load],
  )

  const uploadSound = async (file: File) => {
    setUploading(true)
    try {
      const { cartesia, ...sound } = await api.uploadSound(file)
      setData((d) => (d ? { ...d, sounds: [...d.sounds, sound] } : d))
      setDraft((d) => (d ? { ...d, ambience: { ...d.ambience, sound: sound.id } } : d))
      if (cartesia.ok) toast.success(`"${sound.label}" added`, { description: "Listen to it, then press Save sound to use it." })
      else toast.warning(`"${sound.label}" added`, { description: cartesia.detail, duration: 10000 })
    } catch (e) {
      toast.error(e instanceof ApiError ? (e.status === 413 ? "That file is too big: 20 MB at most" : e.message) : "Couldn't upload this sound")
    } finally {
      setUploading(false)
      if (uploadInput.current) uploadInput.current.value = ""
    }
  }

  const preview = (v: VoiceInfo) => {
    const key = v.id
    if (v.has_preview) return player.play(key, api.previewUrl(v.id))
    if (generated.current.has(key)) return player.play(key, generated.current.get(key)!)
    if (confirmGen !== key) {
      setConfirmGen(key)
      if (sampleChars == null) api.sampleText().then((t) => setSampleChars(t.characters)).catch(() => {})
      return
    }
    setConfirmGen(null)
    player.play(key, async () => {
      const url = await api.generateSample(v.id, draft?.speed ?? 1, draft?.volume ?? 1)
      generated.current.set(key, url)
      return url
    })
  }

  const draftDirty = !!draft && !!active && JSON.stringify(draft) !== JSON.stringify(active)

  return (
    <>
      <PageHeader
        title="Voice"
        description="The Cartesia voice your agent speaks with. Saving updates the Cartesia agent right away."
        actions={<SyncBadge sync={sync} />}
      />

      {loadError ? (
        <EmptyState action={<Button variant="secondary" size="sm" onClick={load}>Retry</Button>}>{loadError}</EmptyState>
      ) : data?.file_error ? (
        <Panel className="p-4 text-[13px] text-danger">agent_settings.yaml is invalid, fix it on the Instructions page first. {data.file_error}</Panel>
      ) : (
        <div className="space-y-6">
          {/* Active voice + tuning */}
          <Panel>
            <PanelHeader title="Active voice" right={<Tag tone="signal">Cartesia</Tag>} />
            {!active || !draft ? (
              <div className="space-y-2 p-4">
                <SkeletonLine className="h-6 w-48" />
                <SkeletonLine className="w-72" />
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-6 p-4 lg:grid-cols-[1fr_360px]">
                <div className="flex min-w-0 items-start gap-4">
                  <PreviewButton
                    big
                    playing={player.playing === active.voice_id}
                    loading={player.loading === active.voice_id}
                    disabled={!activeInfo}
                    onClick={() => activeInfo && preview(activeInfo)}
                    label={`Preview ${activeInfo?.name ?? "voice"}`}
                  />
                  <div className="min-w-0">
                    <div className="text-[22px] leading-7 font-semibold tracking-[-0.02em]">{activeInfo?.name ?? "Unknown voice"}</div>
                    <div className="mt-1 text-[13px] text-muted-foreground">{metaLine(activeInfo) || "–"}</div>
                    {activeInfo?.description && <p className="mt-2 max-w-[560px] text-[13px] leading-5 text-muted-foreground">{activeInfo.description}</p>}
                    <div className="mt-2 font-mono text-[11px] text-faint">{active.voice_id}</div>
                    {activeInfo && !activeInfo.has_preview && <PreviewNote voice={activeInfo} confirm={confirmGen} chars={sampleChars} onGo={preview} onCancel={() => setConfirmGen(null)} />}
                  </div>
                </div>

                <div className="space-y-4">
                  <Slider label="Speed" help={data?.help.speed} value={draft.speed} range={[0.6, 1.5]} step={0.05}
                    format={(v) => `${v.toFixed(2)}×`} onChange={(speed) => setDraft({ ...draft, speed })} />
                  <Slider label="Volume" help={data?.help.volume} value={draft.volume} range={[0.5, 2]} step={0.05}
                    format={(v) => `${v.toFixed(2)}×`} onChange={(volume) => setDraft({ ...draft, volume })} />
                  <div className="flex items-center justify-end gap-2">
                    <Button variant="ghost" size="sm" disabled={!draftDirty || !!savingId} onClick={() => setDraft(active)}>
                      <RotateCcw strokeWidth={1.5} />
                      Discard
                    </Button>
                    <Button size="sm" disabled={!draftDirty || !!savingId} onClick={() => save(draft, "Voice settings saved")}>
                      {savingId === draft.voice_id ? "Saving…" : "Save settings"}
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </Panel>

          {/* Background sound */}
          {active && draft && (
            <Panel>
              <PanelHeader title="Background sound" right={<Tag tone={active.ambience.enabled ? "signal" : undefined}>{active.ambience.enabled ? "On" : "Off"}</Tag>} />
              <div className="grid grid-cols-1 gap-6 p-4 lg:grid-cols-[1fr_360px]">
                <div className="space-y-3">
                  <p className="max-w-[560px] text-[13px] leading-5 text-muted-foreground">
                    Cartesia mixes this quietly under the voice, and the lead hears it during the whole call. It doesn&apos;t go into speech recognition, so the AI
                    doesn&apos;t hear it as someone talking. Applies from the next call.
                  </p>
                  <label className="flex items-center gap-3 text-[13px]">
                    <button
                      role="switch"
                      aria-checked={draft.ambience.enabled}
                      aria-label="Background sound"
                      onClick={() => setDraft({ ...draft, ambience: { ...draft.ambience, enabled: !draft.ambience.enabled } })}
                      className={cn(
                        "relative inline-flex h-6 w-11 items-center rounded-full border transition-colors duration-[120ms]",
                        draft.ambience.enabled ? "border-transparent bg-signal" : "border-line-strong bg-surface-2",
                      )}
                    >
                      <span className={cn("size-4 rounded-full transition-[margin] duration-[120ms]", draft.ambience.enabled ? "ml-[22px] bg-signal-fg" : "ml-[3px] bg-muted-foreground")} />
                    </button>
                    {draft.ambience.enabled ? "On" : "Off"}
                  </label>
                </div>
                <div className="space-y-4">
                  <div className="flex items-center gap-3">
                    <PreviewButton
                      playing={player.playing === `ambience:${draft.ambience.sound}`}
                      loading={player.loading === `ambience:${draft.ambience.sound}`}
                      onClick={() => player.play(`ambience:${draft.ambience.sound}`, api.ambienceUrl(draft.ambience.sound), draft.ambience.volume_percent / 100)}
                      label="Listen to this sound at this volume"
                    />
                    <Select value={draft.ambience.sound} onValueChange={(sound) => setDraft({ ...draft, ambience: { ...draft.ambience, sound } })}>
                      <SelectTrigger className="flex-1" aria-label="Sound type">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {data?.sounds.map((s) => (
                          <SelectItem key={s.id} value={s.id}>
                            {s.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <Slider label="Sound volume" help={data?.help["ambience.volume_percent"]} value={draft.ambience.volume_percent} range={[5, 100]} step={5}
                    format={(v) => `${v}%`} onChange={(volume_percent) => setDraft({ ...draft, ambience: { ...draft.ambience, volume_percent } })} />
                  <div className="flex items-center justify-end gap-2">
                    {isAdmin && (
                      <>
                        <input ref={uploadInput} type="file" accept=".mp3,.wav,audio/mpeg,audio/wav" className="hidden" aria-label="Sound file"
                          onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadSound(f) }} />
                        <Button variant="secondary" size="sm" disabled={uploading} onClick={() => uploadInput.current?.click()}>
                          {uploading ? <LoaderCircle className="animate-spin" strokeWidth={1.5} /> : <Upload strokeWidth={1.5} />}
                          {uploading ? "Processing…" : "Upload your own sound"}
                        </Button>
                      </>
                    )}
                    <Button size="sm" disabled={JSON.stringify(draft.ambience) === JSON.stringify(active.ambience) || !!savingId} onClick={() => save({ ...active, ambience: draft.ambience }, "Background sound saved")}>
                      {savingId && savingId === active.voice_id ? "Saving…" : "Save sound"}
                    </Button>
                  </div>
                </div>
              </div>
            </Panel>
          )}

          {/* Browse */}
          <Panel>
            <div className="flex flex-wrap items-center gap-3 border-b border-border p-4">
              <div className="relative min-w-[220px] flex-1">
                <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" strokeWidth={1.5} />
                <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search name, style, accent…" className="pl-9" aria-label="Search voices" />
              </div>
              <Segmented value={gender} onChange={setGender} />
              <Select value={accent} onValueChange={setAccent}>
                <SelectTrigger className="w-48" aria-label="Accent">
                  <SelectValue placeholder="All accents" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL_ACCENTS}>All accents</SelectItem>
                  {accents.map(([a, n]) => (
                    <SelectItem key={a} value={a}>
                      {a} <span className="text-faint tnum">{n}</span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <button
                role="switch"
                aria-checked={previewOnly}
                onClick={() => setPreviewOnly((x) => !x)}
                className={cn(
                  "inline-flex h-9 items-center gap-2 rounded-md border px-3 text-[13px] transition-colors duration-[120ms] active:scale-[0.98]",
                  previewOnly ? "border-transparent bg-signal-soft text-signal" : "border-border text-muted-foreground hover:bg-surface-2 hover:text-foreground",
                )}
              >
                <Play className="size-3.5 fill-current" strokeWidth={1.75} />
                Has preview
              </button>
              <Button variant="secondary" size="sm" onClick={() => loadList(true)} disabled={listLoading}>
                <RefreshCw className={cn(listLoading && "animate-spin")} strokeWidth={1.5} />
                Refresh
              </Button>
            </div>
            <div className="flex h-9 items-center justify-between px-4 text-[12px] text-faint">
              <span className="tnum">{list ? `${filtered.length} of ${list.voices.length} English voices` : " "}</span>
              {list && <span>{list.cached ? "Cached" : "Fetched"} {timeAgo(list.fetched_at)}</span>}
            </div>

            {listError && !list ? (
              <EmptyState action={<Button variant="secondary" size="sm" onClick={() => loadList()}>Retry</Button>}>{listError}</EmptyState>
            ) : !list ? (
              <ul className="divide-y divide-border border-t border-border">
                {Array.from({ length: 6 }).map((_, i) => (
                  <li key={i} className="flex items-center gap-3 px-4 py-3">
                    <SkeletonLine className="size-8 rounded-full" />
                    <div className="flex-1 space-y-2">
                      <SkeletonLine className="w-40" />
                      <SkeletonLine className="w-64" />
                    </div>
                  </li>
                ))}
              </ul>
            ) : filtered.length === 0 ? (
              <EmptyState action={<Button variant="ghost" size="sm" onClick={() => { setQuery(""); setGender("All"); setAccent(ALL_ACCENTS); setPreviewOnly(false) }}>Clear filters</Button>}>
                No voices match these filters.
              </EmptyState>
            ) : (
              <ul className="divide-y divide-border border-t border-border">
                {filtered.slice(0, shown).map((v) => {
                  const isActive = active?.voice_id === v.id
                  return (
                    <li key={v.id} className="flex items-center gap-3 px-4 py-3 transition-colors duration-[120ms] hover:bg-surface-2">
                      <PreviewButton playing={player.playing === v.id} loading={player.loading === v.id} onClick={() => preview(v)}
                        label={v.has_preview ? `Preview ${v.name}` : `Generate a sample of ${v.name}`} dim={!v.has_preview} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-[13px] font-medium">{v.name}</span>
                          {isActive && <Tag tone="signal">Active</Tag>}
                          {v.is_owner && <Tag>Yours</Tag>}
                        </div>
                        <div className="truncate text-[12px] text-faint">{metaLine(v) || v.id}</div>
                        {confirmGen === v.id ? (
                          <PreviewNote voice={v} confirm={confirmGen} chars={sampleChars} onGo={preview} onCancel={() => setConfirmGen(null)} />
                        ) : (
                          v.description && <p className="line-clamp-1 text-[12px] text-muted-foreground">{v.description}</p>
                        )}
                      </div>
                      {isActive ? (
                        <span className="flex h-8 shrink-0 items-center gap-1.5 px-2 text-[12px] text-signal">
                          <Check className="size-4" strokeWidth={1.75} /> In use
                        </span>
                      ) : (
                        <Button variant="secondary" size="sm" className="shrink-0" disabled={!!savingId || !draft}
                          onClick={() => draft && save({ ...draft, provider: "cartesia", voice_id: v.id }, `Voice set to ${v.name}`)}>
                          {savingId === v.id ? "Saving…" : "Use this voice"}
                        </Button>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
            {list && filtered.length > shown && (
              <div className="border-t border-border p-3 text-center">
                <Button variant="ghost" size="sm" onClick={() => setShown((n) => n + PAGE)}>
                  Show {Math.min(PAGE, filtered.length - shown)} more
                </Button>
              </div>
            )}
          </Panel>
        </div>
      )}
    </>
  )
}

function PreviewNote({ voice, confirm, chars, onGo, onCancel }: {
  voice: VoiceInfo
  confirm: string | null
  chars: number | null
  onGo: (v: VoiceInfo) => void
  onCancel: () => void
}) {
  if (confirm !== voice.id) return <p className="mt-1 text-[12px] text-faint">No preview file for this voice. Press play to generate a sample.</p>
  return (
    <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-warning">
      No free preview. A sample speaks the opening line and uses about {chars ?? "70"} Cartesia TTS credits.
      <Button size="xs" variant="secondary" onClick={() => onGo(voice)}>Generate</Button>
      <Button size="xs" variant="ghost" onClick={onCancel}>Cancel</Button>
    </div>
  )
}

function Segmented({ value, onChange }: { value: Gender; onChange: (v: Gender) => void }) {
  return (
    <div role="radiogroup" aria-label="Gender" className="inline-flex h-9 shrink-0 rounded-md border border-border bg-surface p-0.5">
      {GENDERS.map((g) => {
        const selected = value === g
        return (
          <button
            key={g}
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(g)}
            className={cn(
              "relative flex h-full items-center rounded-[5px] px-3 text-[13px] transition-colors duration-[120ms] active:scale-[0.98]",
              selected ? "text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {selected && (
              <motion.span layoutId="voice-gender" className="absolute inset-0 rounded-[5px] border border-line-strong bg-surface-2"
                transition={{ type: "spring", stiffness: 420, damping: 34 }} />
            )}
            <span className="relative">{g}</span>
          </button>
        )
      })}
    </div>
  )
}

function PreviewButton({ playing, loading, disabled, onClick, label, big, dim }: {
  playing: boolean
  loading: boolean
  disabled?: boolean
  onClick: () => void
  label: string
  big?: boolean
  dim?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={playing ? "Stop preview" : label}
      title={playing ? "Stop" : label}
      className={cn(
        "grid shrink-0 place-items-center rounded-full border transition-colors duration-[120ms] active:scale-[0.98] disabled:opacity-40",
        big ? "size-11" : "size-8",
        playing ? "border-transparent bg-signal-soft text-signal" : "border-line-strong text-muted-foreground hover:bg-surface-2 hover:text-foreground",
        dim && !playing && "border-dashed",
      )}
    >
      {loading ? (
        <LoaderCircle className="size-3.5 animate-spin" strokeWidth={1.75} />
      ) : playing ? (
        <Square className="size-3 fill-current" strokeWidth={1.75} />
      ) : (
        <Play className={cn("ml-px fill-current", big ? "size-4" : "size-3.5")} strokeWidth={1.75} />
      )}
    </button>
  )
}

function Slider({ label, help, value, range, step, format, onChange }: {
  label: string
  help?: string
  value: number
  range: [number, number]
  step: number
  format: (v: number) => string
  onChange: (v: number) => void
}) {
  return (
    <label className="block space-y-1.5">
      <div className="flex items-center justify-between text-[12px]">
        <span className="font-medium text-muted-foreground">{label}</span>
        <span className="font-mono text-foreground tnum">{format(value)}</span>
      </div>
      <input type="range" min={range[0]} max={range[1]} step={step} value={value}
        onChange={(e) => onChange(Math.round(Number(e.target.value) * 100) / 100)}
        className="h-1.5 w-full cursor-pointer accent-[var(--signal)]" />
      {help && <p className="text-[12px] text-faint">{help}</p>}
    </label>
  )
}

function metaLine(v: VoiceInfo | null): string {
  if (!v) return ""
  return [v.gender, v.accent, v.tagline].filter(Boolean).join(" · ")
}

function timeAgo(unix: number): string {
  const s = Math.max(0, Date.now() / 1000 - unix)
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return `${Math.floor(s / 86400)} d ago`
}
