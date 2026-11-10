"use client"

import { Check } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { toast } from "sonner"

import { Panel, Tag } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, ApiError } from "@/lib/api"
import { formatPhone } from "@/lib/format"
import type { PhonicOptions, PhonicVoice, ProviderFeatures, VoiceProviderName, VoiceProviderState } from "@/lib/types"

const NOISE_LABELS: Record<string, string> = { "": "None", office: "Office", "call-center": "Call center", "coffee-shop": "Coffee shop" }
const ITEM = "h-8 px-3 text-[12px] data-[state=on]:border-line-strong data-[state=on]:bg-surface-2 data-[state=on]:text-foreground"
/** The provider choice: the selected one is filled and has a check mark. */
const PROVIDER_ITEM =
  "h-9 gap-1.5 px-4 text-[13px] data-[state=on]:border-transparent data-[state=on]:bg-signal data-[state=on]:font-medium data-[state=on]:text-signal-fg data-[state=on]:hover:bg-signal"

/** Voice provider switch (Cartesia / Phonic) with Phonic's own options. Saved to data/voice_provider.json.
 *  onProviderChange gets the selected (not yet saved) provider, so the Voice page can show that provider's settings. */
export function VoiceProviderPanel({ onProviderChange }: { onProviderChange?: (p: VoiceProviderName) => void }) {
  const [state, setState] = useState<VoiceProviderState | null>(null)
  const [provider, setProvider] = useState<VoiceProviderName>("cartesia")
  const [opts, setOpts] = useState<PhonicOptions | null>(null)
  const [voices, setVoices] = useState<PhonicVoice[] | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const s = await api.voiceProvider()
      setState(s)
      setProvider(s.provider)
      setOpts(s.phonic)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't load the voice provider")
      onProviderChange?.("cartesia")
    }
  }, [onProviderChange])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (state) onProviderChange?.(provider)
  }, [state, provider, onProviderChange])

  useEffect(() => {
    if (provider !== "phonic" || voices || !state?.phonic_configured) return
    api.phonicVoices().then((r) => setVoices(r.voices)).catch((e) => {
      setVoices([])
      toast.error("Couldn't load Phonic voices", { description: e instanceof ApiError ? e.message : undefined })
    })
  }, [provider, voices, state?.phonic_configured])

  const dirty = useMemo(
    () => !!state && (provider !== state.provider || JSON.stringify(opts) !== JSON.stringify(state.phonic)),
    [state, provider, opts],
  )

  const save = async () => {
    if (!opts || saving) return
    setSaving(true)
    try {
      const s = await api.saveVoiceProvider(provider, opts)
      setState(s)
      setProvider(s.provider)
      setOpts(s.phonic)
      toast.success(`Voice provider: ${s.features[s.provider].label}`, { description: "Used from the next call." })
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't save")
    } finally {
      setSaving(false)
    }
  }

  if (!state || !opts) return null
  const set = (patch: Partial<PhonicOptions>) => setOpts({ ...opts, ...patch })
  const voiceList = voices && !voices.some((v) => v.id === opts.voice_id) ? [{ id: opts.voice_id, name: opts.voice_id, description: "", preview_url: "" }, ...voices] : voices

  return (
    <Panel id="section-voice-provider" className="scroll-mt-8 space-y-4 p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-[13px] font-medium">Voice provider</div>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            Who runs the agent on the next call. Both use the same instructions (Instructions page). Each call remembers which one it used (History).
          </p>
        </div>
        <Button size="sm" onClick={save} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save provider"}
        </Button>
      </div>

      <ToggleGroup type="single" variant="outline" value={provider} onValueChange={(v) => v && setProvider(v as VoiceProviderName)}>
        {(["cartesia", "phonic"] as const).map((p) => (
          <ToggleGroupItem key={p} value={p} className={PROVIDER_ITEM} disabled={p === "phonic" && !state.phonic_configured} aria-label={state.features[p]?.label ?? p}>
            {provider === p && <Check className="size-4" strokeWidth={2} />}
            {p === "cartesia" ? "Cartesia (default)" : "Phonic"}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {!state.phonic_configured && <p className="text-[12px] text-faint">Phonic needs PHONIC_API_KEY in .env.</p>}
      {provider !== state.provider && (
        <p className="text-[12px] text-warning">
          Not saved yet: calls still use {state.features[state.provider]?.label ?? state.provider}. Press Save provider to switch.
        </p>
      )}

      {provider === "phonic" && (
        <div className="space-y-4 border-t border-border pt-4">
          {state.phonic_test_only && (
            <p className="text-[12px] text-signal">
              Test only: Phonic can call {formatPhone(state.phonic_test_number)} and nothing else (PHONIC_TEST_ONLY).
            </p>
          )}
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="space-y-1.5">
              <span className="text-[12px] text-muted-foreground">Voice</span>
              <Select value={opts.voice_id} onValueChange={(v) => set({ voice_id: v })}>
                <SelectTrigger className="h-8 w-full text-[13px]">
                  <SelectValue placeholder={voices === null ? "Loading…" : "Pick a voice"} />
                </SelectTrigger>
                <SelectContent>
                  {(voiceList ?? [{ id: opts.voice_id, name: opts.voice_id, description: "", preview_url: "" }]).map((v) => (
                    <SelectItem key={v.id} value={v.id}>
                      {v.name}
                      {v.description ? <span className="ml-1 text-faint">· {v.description}</span> : null}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            <label className="space-y-1.5">
              <span className="text-[12px] text-muted-foreground">
                Speed ({state.speed_range[0]}–{state.speed_range[1]}, 1 = normal)
              </span>
              <Input
                type="number"
                step={0.05}
                min={state.speed_range[0]}
                max={state.speed_range[1]}
                value={opts.audio_speed}
                onChange={(e) => set({ audio_speed: Number(e.target.value) })}
                className="h-8 text-[13px]"
              />
            </label>
          </div>
          <div className="space-y-1.5">
            <span className="text-[12px] text-muted-foreground">Background sound (Phonic&apos;s own; our own recordings are Cartesia only)</span>
            <div className="flex flex-wrap items-center gap-3">
              <ToggleGroup
                type="single"
                variant="outline"
                value={opts.background_noise || "none"}
                onValueChange={(v) => v && set({ background_noise: (v === "none" ? "" : v) as PhonicOptions["background_noise"], background_noise_level: v === "none" ? 0 : opts.background_noise_level || 0.1 })}
              >
                {["none", ...state.noises].map((n) => (
                  <ToggleGroupItem key={n} value={n} className={ITEM}>
                    {NOISE_LABELS[n === "none" ? "" : n] ?? n}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
              {opts.background_noise && (
                <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
                  Level (0–1)
                  <Input
                    type="number"
                    step={0.05}
                    min={0}
                    max={1}
                    value={opts.background_noise_level}
                    onChange={(e) => set({ background_noise_level: Number(e.target.value) })}
                    className="h-8 w-20 text-[13px]"
                  />
                </label>
              )}
            </div>
          </div>
          <FeatureList f={state.features.phonic} />
        </div>
      )}
    </Panel>
  )
}

function FeatureList({ f }: { f: ProviderFeatures }) {
  const row = (label: string, ok: boolean, note: string) => (
    <li className="flex items-baseline gap-2">
      <Tag tone={ok ? "signal" : "danger"}>{ok ? "Yes" : "No"}</Tag>
      <span className="text-foreground">{label}</span>
      <span className="text-faint">{note}</span>
    </li>
  )
  return (
    <ul className="space-y-1.5 text-[12px]">
      {row("Voicemail detection", f.voicemail_detection, f.voicemail_detection ? "" : "Not offered by Phonic. The agent is told to hang up on a recording, and our own transcript check closes the line.")}
      {row("Voice speed", f.voice_speed, "")}
      {row("Background sound", f.background_sound, "Office, call center or coffee shop.")}
      {row("Transfer", true, f.transfer)}
      {row("Call summary", f.summary, f.summary ? "" : "Phonic gives none: the summary stays empty on Phonic calls.")}
    </ul>
  )
}
