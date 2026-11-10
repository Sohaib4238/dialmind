export type CallStatus = "dialing" | "ringing" | "answered" | "ended" | "failed"
export type CallResult = "answered" | "voicemail" | "no_answer" | "busy" | "bad_line" | "failed" | "trunk_rejected" | null

export interface Call {
  id: number
  room: string
  number: string
  status: CallStatus
  result: CallResult
  reason: string
  source: "dashboard" | "cli" | "simulated" | "inbound"
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
  direction?: "outbound" | "inbound"
  ai_summary?: string
  transfer_offered?: string[]
  transfer_declined_by?: string[]
  transfer_to?: string
  transfer_to_name?: string
  provider?: VoiceProviderName
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
  | { type: "sheet_sync"; summary: SheetSyncSummary }
  | { type: "sheet_results"; results: SheetResults }

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
  inbound?: InboundStatus
}

/** Inbound calls (Instructions page): does Cartesia answer our number? */
export interface InboundStatus {
  enabled: boolean
  number: string
  state: "unknown" | "off" | "on" | "pending" | "error" | "standby"
  detail: string
  checked_at: number | null
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
  inbound?: InboundStatus
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
  | "handled_by_sales"

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
  lead_source: string
  budget: string
  additional_info: string
  alt_phones: string[]
  alt_emails: string[]
  links: string[]
  sheet_tab: string
  sheet_row: number | null
  sales_handled: boolean
  sales_handled_at: string | null
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
  /** Red warning: calls refused before they rang, or the Telnyx balance too low. Cleared when the dialer is turned on. */
  alert?: string
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
  /** New leads: sheet order (top to bottom, first tab first) or newest first. */
  order?: DialerOrder
  orders?: Record<DialerOrder, string>
  log: DialerLogEntry[]
}

export type DialerOrder = "sheet" | "newest"

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

/** One supervisor: own login (key), own transfer phone, own "available" switch, own working hours. */
export interface SupervisorPerson {
  key: string
  name: string
  phone: string
  available: boolean
  hours: SupervisorHours
  can_take_calls: boolean
  why_not: string
}

export interface SupervisorSettings {
  wait_seconds: number
  people: SupervisorPerson[]
  can_take_calls: boolean
  why_not: string
  hours_zones: Record<string, string>
  wait_min: number
  wait_max: number
  dashboard_url?: string | null
  login_configured?: boolean
  transfer_ready?: boolean
  me?: string | null
  is_admin?: boolean
  me_supervisor?: boolean
  logins?: string[]
  assignments?: boolean | null
  assignments_error?: string
}

export interface SheetSyncSummary {
  ok: boolean
  at?: string
  error?: string
  trigger?: string
  tabs?: Record<string, number>
  rows?: number
  new?: number
  duplicates?: number
  filled?: number
  /** Leads whose row moved in the sheet (rows inserted / deleted above them). */
  moved?: number
  /** "Re-check colours": only the colour rule on existing leads; nothing imported. */
  colors_only?: boolean
  not_imported?: number
  handled?: { new: number; existing: number; skipped: number }
  handled_colors?: string[]
  skipped_count?: number
  skipped?: { tab: string; row: number | null; name?: string; reason: string; group?: string }[]
  skipped_groups?: { group: string; reason: string; count: number }[]
  /** Every distinct background colour found: rows with it in any cell (hex "" = no fill). */
  colors?: { tab: string; hex: string; family: string; label: string; rows: number; handled: boolean }[]
  /** What each row was read as (one colour per row). */
  row_colors?: Record<string, { family: string; label: string; rows: number; handled: boolean }[]>
  /** Conditional-formatting rules / alternating colours per tab: their colours aren't seen. */
  color_rules?: Record<string, { conditional: number; banded: number }>
  duplicate_rows?: { tab: string; row: number; name: string; phone: string; reason: string; lead_id?: number }[]
  marked_handled?: { tab: string; row: number; name: string; lead_id: number; color: string }[]
  missing_tabs?: string[]
}

export interface SheetResults {
  ok: boolean
  at: string
  error?: string
  written?: number
  updated?: number
  added?: number
}

export interface SheetSettings {
  enabled: boolean
  sales_sheet: string
  tabs: string[]
  sync_minutes: number
  handled_colors: string[]
  handled_action: "import" | "skip"
  results_sheet: string
  results_tab: string
  results_timezone: string
  dialer_order: DialerOrder
}

export interface SheetsStatus {
  settings: SheetSettings
  key: { ok: boolean; client_email: string; detail: string }
  last_sync: SheetSyncSummary | null
  results: SheetResults | null
  next_sync_at: string | null
  running: boolean
  standby: boolean
  colors: string[]
  /** Never called whatever is saved: green, yellow, red. */
  always_handled: string[]
  result_zones: Record<string, string>
  dialer_orders: Record<DialerOrder, string>
}

/** What the supervisor's pop-up shows (one open request per call). */
export interface SupervisorRequest {
  call_id: number
  direction?: "outbound" | "inbound"
  lead_id: number | null
  status: TransferStatus
  label: string
  summary: string
  note: string
  requested_at: string | null
  deadline: string | null
  wait_seconds: number
  offered?: { key: string; name: string }[]
  declined_by?: string[]
  taken_by?: string
  taken_by_key?: string
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

export type VoiceProviderName = "cartesia" | "phonic"

export interface PhonicOptions {
  voice_id: string
  audio_speed: number
  background_noise: "" | "office" | "call-center" | "coffee-shop"
  background_noise_level: number
}

export interface ProviderFeatures {
  label: string
  voicemail_detection: boolean
  voice_speed: boolean
  background_sound: boolean
  transfer: string
  summary: boolean
  webhooks: boolean
}

export interface VoiceProviderState {
  provider: VoiceProviderName
  phonic: PhonicOptions
  features: Record<VoiceProviderName, ProviderFeatures>
  noises: string[]
  speed_range: [number, number]
  phonic_configured: boolean
  phonic_test_only: boolean
  phonic_test_number: string
}

export interface PhonicVoice {
  id: string
  name: string
  description: string
  preview_url: string
}
