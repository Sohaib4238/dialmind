import type { PhonicOptions, PhonicVoice, VoiceProviderName, VoiceProviderState, AppConfig, AuthMe, BackgroundSound, Call, CallEvent, DialerOrder, DialerState, Email, EmailState, Health, ImportPreview, Lead, LeadCounts, LeadDetail, LeadsList, SaveResult, SettingsPayload, SheetSettings, SheetsStatus, SheetSyncSummary, Sms, SmsState, SupervisorHours, SupervisorRequest, SupervisorSettings, VoiceList, VoicePayload, VoiceSettings } from "./types"

/** True when the dashboard is opened from another computer (through the dashboard tunnel). */
export const IS_REMOTE =
  typeof window !== "undefined" && !["127.0.0.1", "localhost"].includes(window.location.hostname)

// On this computer the browser talks to the backend directly; remotely it uses /api on the same
// origin, which Next.js passes to the backend (next.config.ts) and the backend asks for a login.
export const API_URL = IS_REMOTE ? "" : (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "")

/** Remote use: a 401 means "log in"; the login gate listens for this. */
export const LOGIN_REQUIRED_EVENT = "dialer:login-required"

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, message: string, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      cache: "no-store",
    })
  } catch {
    throw new ApiError(0, "Can't reach the dashboard backend. Is it running on port 8000?", null)
  }
  const body = await res.json().catch(() => null)
  if (res.status === 401 && IS_REMOTE && !path.startsWith("/api/auth/")) {
    window.dispatchEvent(new Event(LOGIN_REQUIRED_EVENT))
  }
  if (!res.ok) {
    const detail = body?.detail
    const message = typeof detail === "string" ? detail : `Request failed (${res.status})`
    throw new ApiError(res.status, message, detail)
  }
  return body as T
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  testMode: () => request<{ on: boolean; number: string }>("/api/test-mode"),
  calls: () => request<Call[]>("/api/calls"),
  call: (id: number) => request<{ call: Call; events: CallEvent[] }>(`/api/calls/${id}`),
  activeCall: () => request<{ call: Call | null }>("/api/calls/active"),
  startCall: (number: string) =>
    request<Call>("/api/calls", { method: "POST", body: JSON.stringify({ number }) }),
  hangUp: (id: number, force = false) =>
    request<{ ok: boolean }>(`/api/calls/${id}/hangup${force ? "?force=true" : ""}`, { method: "POST" }),
  me: () => request<AuthMe>("/api/auth/me"),
  login: (username: string, password: string) =>
    request<{ ok: boolean; user: string }>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  supervisor: () => request<SupervisorSettings>("/api/supervisor"),
  saveSupervisorWait: (wait_seconds: number) =>
    request<SupervisorSettings>("/api/supervisor", { method: "PUT", body: JSON.stringify({ wait_seconds }) }),
  saveSupervisorPerson: (key: string, values: { key?: string; name?: string; phone?: string; available?: boolean; hours?: SupervisorHours }) =>
    request<SupervisorSettings>(`/api/supervisor/people/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify(values) }),
  removeSupervisorPerson: (key: string) =>
    request<SupervisorSettings>(`/api/supervisor/people/${encodeURIComponent(key)}`, { method: "DELETE" }),
  /** Your own switch; an admin (or this computer) may pass whose. */
  setAvailable: (available: boolean, key?: string) =>
    request<SupervisorSettings>("/api/supervisor/availability", { method: "POST", body: JSON.stringify({ available, key }) }),
  supervisorRequests: () => request<SupervisorRequest[]>("/api/supervisor/requests"),
  answerRequest: (callId: number, accept: boolean, supervisor?: string) =>
    request<SupervisorRequest>(`/api/supervisor/requests/${callId}/answer`, { method: "POST", body: JSON.stringify({ accept, supervisor }) }),
  sheets: () => request<SheetsStatus>("/api/sheets"),
  saveSheets: (values: Partial<SheetSettings>) =>
    request<SheetsStatus>("/api/sheets", { method: "PUT", body: JSON.stringify(values) }),
  syncSheets: () => request<SheetSyncSummary>("/api/sheets/sync", { method: "POST" }),
  recheckColors: () => request<SheetSyncSummary>("/api/sheets/recheck-colors", { method: "POST" }),
  setDialerOrder: (order: DialerOrder) =>
    request<DialerState>("/api/dialer/order", { method: "POST", body: JSON.stringify({ order }) }),
  rewriteResults: () => request<{ ok: boolean; queued: number }>("/api/sheets/results/rewrite", { method: "POST" }),
  setSalesHandled: (id: number, handled: boolean) =>
    request<Lead>(`/api/leads/${id}/sales-handled`, { method: "POST", body: JSON.stringify({ handled }) }),
  simulate: (number: string) =>
    request<Call>("/api/dev/simulate", { method: "POST", body: JSON.stringify({ number }) }),
  health: () => request<Health>("/api/health"),
  runChecks: () => request<Health>("/api/health/check", { method: "POST" }),
  settings: () => request<SettingsPayload>("/api/settings"),
  voiceProvider: () => request<VoiceProviderState>("/api/voice-provider"),
  saveVoiceProvider: (provider: VoiceProviderName, phonic?: Partial<PhonicOptions>) =>
    request<VoiceProviderState>("/api/voice-provider", { method: "PUT", body: JSON.stringify({ provider, phonic }) }),
  phonicVoices: () => request<{ voices: PhonicVoice[] }>("/api/phonic/voices"),
  saveSettings: (values: Record<string, unknown>, version: string) =>
    request<SaveResult>("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ values, version }),
    }),
  leads: (params: { q?: string; status?: string; sort?: "sheet" | "recent"; limit?: number } = {}) => {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v).map(([k, v]) => [k, String(v)])).toString()
    return request<LeadsList>(`/api/leads${qs ? `?${qs}` : ""}`)
  },
  lead: (id: number) => request<LeadDetail>(`/api/leads/${id}`),
  leadCounts: () => request<LeadCounts>("/api/leads/counts"),
  dialer: () => request<DialerState>("/api/dialer"),
  dialerOn: () => request<DialerState>("/api/dialer/on", { method: "POST" }),
  dialerOff: () => request<DialerState>("/api/dialer/off", { method: "POST" }),
  setTestMode: (enabled: boolean, confirm = false) =>
    request<DialerState>("/api/dialer/test-mode", { method: "POST", body: JSON.stringify({ enabled, confirm }) }),
  previewImport: (csv: string, filename: string) =>
    request<ImportPreview>("/api/leads/import/preview", { method: "POST", body: JSON.stringify({ csv, filename }) }),
  importLeads: (csv: string, filename: string) =>
    request<{ imported: number; updated: number; not_a_fit: number }>("/api/leads/import", { method: "POST", body: JSON.stringify({ csv, filename }) }),
  setDnc: (id: number, dnc: boolean, reason = "") =>
    request<Lead>(`/api/leads/${id}/dnc`, { method: "POST", body: JSON.stringify({ dnc, reason }) }),
  addDnc: (phone: string, reason = "") => request<Lead>("/api/dnc", { method: "POST", body: JSON.stringify({ phone, reason }) }),
  setOutcome: (id: number, outcome: string, note: string, callback_time: string) =>
    request<Lead>(`/api/leads/${id}/outcome`, { method: "POST", body: JSON.stringify({ outcome, note, callback_time }) }),
  callAnyway: (id: number) => request<Lead>(`/api/leads/${id}/call-anyway`, { method: "POST" }),
  callLead: (id: number) => request<Call>(`/api/leads/${id}/call`, { method: "POST" }),
  emails: (params: { lead_id?: number; call_id?: number; limit?: number } = {}) => {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)])).toString()
    return request<Email[]>(`/api/emails${qs ? `?${qs}` : ""}`)
  },
  emailStatus: () => request<EmailState>("/api/emails/status"),
  retryEmail: (id: number) => request<Email>(`/api/emails/${id}/retry`, { method: "POST" }),
  checkUnsubscribes: () => request<EmailState["unsubscribe"]>("/api/emails/check-unsubscribes", { method: "POST" }),
  sendTestEmail: () => request<{ ok: boolean; detail: string }>("/api/emails/test", { method: "POST" }),
  texts: (params: { lead_id?: number; call_id?: number; limit?: number } = {}) => {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)])).toString()
    return request<Sms[]>(`/api/sms${qs ? `?${qs}` : ""}`)
  },
  smsStatus: () => request<SmsState>("/api/sms/status"),
  retrySms: (id: number) => request<Sms>(`/api/sms/${id}/retry`, { method: "POST" }),
  sendTestSms: () => request<{ ok: boolean; detail: string }>("/api/sms/test", { method: "POST" }),
  voice: () => request<VoicePayload>("/api/voice"),
  voices: (refresh = false) => request<VoiceList>(`/api/voice/voices${refresh ? "?refresh=true" : ""}`),
  sampleText: () => request<{ text: string; characters: number }>("/api/voice/sample-text"),
  saveVoice: (voice: VoiceSettings, version: string) =>
    request<SaveResult>("/api/voice", { method: "PUT", body: JSON.stringify({ voice, version }) }),
  /** URL of the voice's own preview file (free; proxied by the backend). */
  previewUrl: (voiceId: string) => `${API_URL}/api/voice/preview/${encodeURIComponent(voiceId)}`,
  /** URL of a background sound file (free; served by the backend). */
  ambienceUrl: (sound: string) => `${API_URL}/api/voice/ambience/${encodeURIComponent(sound)}`,
  /** Your own background sound: processed on the server, can take a little while. */
  uploadSound: (file: File) =>
    request<BackgroundSound & { cartesia: { ok: boolean; detail?: string } }>(`/api/voice/sounds?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    }),
  /** Generates a sample for a voice without a preview (uses Cartesia TTS credits). Returns a blob URL. */
  generateSample: async (voice_id: string, speed: number, volume: number): Promise<string> => {
    let res: Response
    try {
      res = await fetch(`${API_URL}/api/voice/sample`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice_id, speed, volume }),
      })
    } catch {
      throw new ApiError(0, "Can't reach the dashboard backend. Is it running on port 8000?", null)
    }
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      throw new ApiError(res.status, typeof body?.detail === "string" ? body.detail : `Sample failed (${res.status})`, body?.detail)
    }
    return URL.createObjectURL(await res.blob())
  },
}
