export type CallStatus = "dialing" | "ringing" | "answered" | "ended" | "failed"
export type CallResult = "answered" | "voicemail" | "no_answer" | "busy" | "bad_line" | "failed" | null

export interface Call {
  id: number
  room: string
  number: string
  status: CallStatus
  result: CallResult
  reason: string
  source: "dashboard" | "cli" | "simulated"
  simulated: boolean
  created_at: string
  answered_at: string | null
  ended_at: string | null
  duration_s: number | null
  lead_id?: number | null
  outcome?: Outcome | null
  outcome_source?: "ai" | "transcript" | "call" | "manual" | "transfer" | ""
  outcome_note?: string
  callback_at?: string | null
  transfer_status?: TransferStatus
  transfer_summary?: string
  transfer_note?: string
  transfer_requested_at?: string | null
  transfer_answered_at?: string | null
  vm_message_left?: boolean
  email_collected?: string
  email_reason?: "details" | "interested" | "not_now" | "other" | ""
}

export type TransferStatus =
  | "" | "unavailable" | "waiting" | "accepted" | "declined" | "timeout" | "transferring" | "transferred" | "failed"

export type EventKind = "status" | "transcript" | "timing" | "state" | "log" | "agent" | "debug"
export type Role = "agent" | "callee" | "system" | null

export interface CallEvent {
  id: number
  call_id: number
  ts: string
  kind: EventKind
  role: Role
  text: string
  data: Record<string, unknown>
}

export type StreamMessage =
  | { type: "call"; call: Call }
  | { type: "event"; event: CallEvent }
  | { type: "lead"; lead: Lead }
  | { type: "leads"; action: string; count: number }
  | { type: "dialer"; dialer: DialerState }
  | { type: "supervisor_request"; request: SupervisorRequest }
  | { type: "supervisor_settings"; settings: SupervisorSettings }
  | { type: "email"; email: Email }
  | { type: "sms"; sms: Sms }

export interface AppConfig {
  dev: boolean
  default_number: string
  engine: string
  from_number: string
}

export interface HealthCheck {
  name: string
  ok: boolean
  detail: string
  ms: number
  warn?: boolean
}

export interface Health {
  /** Can Cartesia reach the backend (tunnel up, webhook registered)? */
  link: {
    online: boolean
    /** A standby server (DIALER_STANDBY): Cartesia + Telnyx still point at the laptop, on purpose. */
    standby?: boolean
    public_url: string | null
    webhook_url: string | null
    detail: string
  }
  checks: HealthCheck[] | null
  checked_at: string | null
}

export type FieldKind = "text" | "select" | "number" | "time" | "multi" | "list" | "group" | "switch"

export interface FieldSpec {
  key: string
  path: string
  label: string
  help: string
  coming_soon: boolean
  kind: FieldKind
  options?: { value: string; label: string }[]
  min?: number | null
  max?: number | null
  fields?: FieldSpec[]
  item_fields?: FieldSpec[]
}

export interface SettingsPayload {
  fields: FieldSpec[]
  values: Record<string, unknown>
  version: string
  file_error: string | null
  path: string
  sync?: SyncStatus
}

export interface SyncStatus {
  in_sync: boolean
  detail: string
  synced_at?: number | null
}

export interface SaveResult {
  version: string
  backup: string
  cartesia?: { ok: boolean; action?: string; detail?: string }
}

export interface Ambience {
  enabled: boolean
  sound: string
  volume_percent: number
}

export interface BackgroundSound {
  id: string
  label: string
  builtin: boolean
}

export interface VoiceSettings {
  provider: "cartesia"
  voice_id: string
  speed: number
  volume: number
  ambience: Ambience
}

export interface VoiceInfo {
  id: string
  name: string
  gender: string
  accent: string
  locale: string
  tagline: string
  description: string
  is_owner: boolean
  has_preview: boolean
}

export interface VoicePayload {
  voice: VoiceSettings | null
  voice_info: VoiceInfo | null
  version: string
  file_error: string | null
  help: Record<string, string>
  sounds: BackgroundSound[]
  sync: SyncStatus
}

export interface VoiceList {
  voices: VoiceInfo[]
  fetched_at: number
  cached: boolean
  stale?: string
}

export type LeadStatus =
  | "new" | "calling" | "interested" | "callback" | "not_interested" | "needs_review" | "voicemail" | "no_answer"
  | "bad_line" | "bad_number" | "do_not_contact" | "not_a_fit" | "failed" | "answered" | "transferred" | "texted"

export type Outcome =
  | "interested" | "not_now" | "gatekeeper" | "not_interested" | "do_not_contact" | "wrong_number"
  | "voicemail" | "bad_line" | "no_answer" | "failed" | "needs_review"
  | "transferred" | "transfer_declined" | "transfer_failed"

export interface Lead {
  id: number
  phone: string
  name: string
  business: string
  city: string
  location: string
  service_required: string
  business_description: string
  job_description: string
  email: string
  timezone: string | null
  fit_override: boolean
  callback_at: string | null
  callback_note: string
  callback_count: number
  vm_count: number
  tz_source: "zip" | "state" | "area_code" | "unknown"
  notes: string
  extra: Record<string, string>
  status: LeadStatus
  dnc: boolean
  dnc_reason: string
  dnc_at: string | null
  attempts: number
  last_call_at: string | null
  last_call_id: number | null
  source: string
  created_at: string | null
}

export interface LeadCounts {
  total: number
  called: number
  by_status: Record<LeadStatus, number>
}

export interface LeadsList {
  leads: Lead[]
  total: number
  counts: LeadCounts
}

export interface LeadEvent {
  id: number
  lead_id: number
  ts: string
  kind: string
  text: string
  data: Record<string, unknown>
}

export interface LeadDetail {
  lead: Lead
  calls: Call[]
  events: LeadEvent[]
  emails: Email[]
  texts: Sms[]
}

export type EmailStatus = "sent" | "failed" | "not_sent"

export interface Sms {
  id: number
  lead_id: number
  call_id: number | null
  template: "not_answered" | "not_interested" | "not_now" | "interested"
  lead_phone: string
  to_phone: string
  test_mode: boolean
  body: string
  status: "queued" | "sent" | "delivered" | "failed" | "not_sent"
  detail: string
  send_after: string | null
  created_at: string | null
  sent_at: string | null
  delivered_at: string | null
}

export interface SmsState {
  configured: boolean
  detail: string
  enabled: boolean
  test_mode: boolean
  test_number: string
  disabled: boolean
  sent_24h: number
  daily_limit: number
  queued: number
  from_number: string
  profile_id: string
  webhook: string
  webhook_ok: boolean
  signature: string
}

/** A follow-up email (sent, failed, or not sent and why). */
export interface Email {
  id: number
  lead_id: number
  call_id: number | null
  template: "interested" | "callback" | "details" | "voicemail_followup"
  lead_email: string
  to_email: string
  test_mode: boolean
  subject: string
  body: string
  status: EmailStatus
  detail: string
  created_at: string | null
  sent_at: string | null
}

export interface EmailState {
  configured: boolean
  detail: string
  enabled: boolean
  test_mode: boolean
  test_address: string
  disabled: boolean
  sent_24h: number
  daily_limit: number
  from_address: string
  unsubscribe: { enabled: boolean; detail: string; last_check: number | null; unsubscribed: number }
}

export interface ImportRow {
  row: number
  phone: string
  phone_display?: string
  name: string
  business?: string
  city?: string
  location?: string
  service_required?: string
  notes?: string
  reason?: string
}

export interface ImportPreview {
  filename: string
  columns: Record<string, string>
  extra_columns: string[]
  total_rows: number
  counts: { valid: number; invalid: number; duplicates: number; updates: number }
  valid: ImportRow[]
  invalid: ImportRow[]
  duplicates: ImportRow[]
}

export interface DialerLogEntry {
  ts: number
  text: string
  level: "info" | "warn" | "error"
}

export interface DialerState {
  state: "off" | "on" | "paused"
  reason: string
  test_mode: boolean
  test_number: string
  session_calls: number
  failures_in_row: number
  current: { lead_id: number; name: string; phone: string; dialed: string; call_id: number; attempt: number; local_time?: string } | null
  next: { lead_id: number; name: string; phone: string; status: LeadStatus; attempts: number; local_time?: string } | null
  queue: {
    new?: number; retries_due?: number; retries_waiting?: number; callbacks_due?: number; callbacks_waiting?: number
    outside_hours?: number; unknown_tz?: number
  }
  wait_detail: string
  minutes_left: number
  limits: { max_calls?: number; max_retries?: number; retry_gap_hours?: number; pause_after_failures?: number; min_free_minutes?: number; hours?: string; days?: string[]; error?: string }
  log: DialerLogEntry[]
}

export interface AuthMe {
  remote: boolean
  configured: boolean
  user: string | null
  role: "admin" | "supervisor" | null
}

export interface SupervisorHours {
  enabled: boolean
  timezone: "America/New_York" | "UTC"
  start: string
  end: string
  days: string[]
}

export interface SupervisorSettings {
  phone: string
  available: boolean
  wait_seconds: number
  hours: SupervisorHours
  can_take_calls: boolean
  why_not: string
  hours_zones: Record<string, string>
  wait_min: number
  wait_max: number
  dashboard_url?: string | null
  login_configured?: boolean
  transfer_ready?: boolean
}

/** What the supervisor's pop-up shows (one open request per call). */
export interface SupervisorRequest {
  call_id: number
  lead_id: number | null
  status: TransferStatus
  label: string
  summary: string
  note: string
  requested_at: string | null
  deadline: string | null
  wait_seconds: number
  lead: {
    lead_name: string
    business_name: string
    service_required: string
    business_description: string
    job_description: string
    location: string
    phone: string
    lead_local_time: string
    lead_timezone: string
  }
  transcript: { role: Role; text: string }[]
}
