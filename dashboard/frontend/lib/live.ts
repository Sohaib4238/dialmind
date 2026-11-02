"use client"

import { useEffect, useRef, useSyncExternalStore } from "react"
import { API_URL, IS_REMOTE } from "./api"
import type { StreamMessage } from "./types"

// One shared live connection for the whole app (all pages subscribe to it): SSE on this computer,
// a WebSocket from another laptop (through the dashboard tunnel).
type Listener = (msg: StreamMessage) => void

const listeners = new Set<Listener>()
const statusListeners = new Set<() => void>()
let source: EventSource | WebSocket | null = null
let connected = false
let retryTimer: ReturnType<typeof setTimeout> | null = null

/** After this long without the live stream, pages switch to polling (the supervisor pop-up must still appear). */
export const FALLBACK_AFTER_MS = 5000
const WS_SILENT_MAX_MS = 45_000
let downSince = Date.now()
let fallback = false
let fallbackTimer: ReturnType<typeof setTimeout> | null = null

function setConnected(v: boolean) {
  if (connected !== v) {
    connected = v
    if (v) {
      fallback = false
      if (fallbackTimer) clearTimeout(fallbackTimer)
      fallbackTimer = null
    } else {
      downSince = Date.now()
      armFallback()
    }
    statusListeners.forEach((l) => l())
  }
}

function armFallback() {
  if (fallbackTimer || connected || typeof window === "undefined") return
  fallbackTimer = setTimeout(() => {
    fallbackTimer = null
    if (!connected && Date.now() - downSince >= FALLBACK_AFTER_MS - 50) {
      fallback = true
      statusListeners.forEach((l) => l())
    }
  }, Math.max(0, FALLBACK_AFTER_MS - (Date.now() - downSince)))
}

function deliver(data: string) {
  try {
    const msg = JSON.parse(data) as StreamMessage | { type: "hello" | "ping" }
    if (msg.type === "hello") setConnected(true)
    else if (msg.type !== "ping") listeners.forEach((l) => l(msg as StreamMessage))
  } catch {
    /* ignore malformed */
  }
}

function lost() {
  setConnected(false)
  source?.close()
  source = null
  if (!retryTimer && (listeners.size || statusListeners.size)) {
    retryTimer = setTimeout(() => {
      retryTimer = null
      open()
    }, 2000)
  }
}

function open() {
  if (source || typeof window === "undefined") return
  armFallback() // never connected yet counts as "down" too
  if (IS_REMOTE) {
    // Cloudflare quick tunnels don't support SSE, so the remote dashboard uses a WebSocket
    // (same origin; Next.js passes it to the backend, the login cookie goes along).
    const ws = new WebSocket(`${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/api/live`)
    source = ws
    let last = Date.now()
    const watchdog = setInterval(() => {
      if (Date.now() - last > WS_SILENT_MAX_MS) ws.close() // the backend pings every 20 s; silence = dead link
    }, 5000)
    ws.onmessage = (e) => {
      last = Date.now()
      deliver(String(e.data))
    }
    ws.onclose = () => {
      clearInterval(watchdog)
      if (source === ws) lost()
    }
    return
  }
  const es = new EventSource(`${API_URL}/api/stream`)
  source = es
  es.addEventListener("hello", () => setConnected(true))
  es.addEventListener("message", (e) => deliver((e as MessageEvent).data))
  es.onerror = () => {
    if (source === es) lost()
  }
}

/** Subscribe to live messages from the backend. */
export function useLiveStream(onMessage: Listener) {
  const ref = useRef(onMessage)
  ref.current = onMessage
  useEffect(() => {
    const l: Listener = (m) => ref.current(m)
    listeners.add(l)
    open()
    return () => {
      listeners.delete(l)
    }
  }, [])
}

export type LiveStatus = "connected" | "fallback" | "connecting"

/** connected; "fallback" = down for 5+ s, so pages poll instead; "connecting" = down for less than that. */
export function useLiveStatus(): LiveStatus {
  return useSyncExternalStore(
    (cb) => {
      statusListeners.add(cb)
      open()
      return () => statusListeners.delete(cb)
    },
    () => (connected ? "connected" : fallback ? "fallback" : "connecting"),
    () => "connecting",
  )
}

/** Whether the live stream is connected (for the sidebar indicator). */
export function useLiveConnected(): boolean {
  return useSyncExternalStore(
    (cb) => {
      statusListeners.add(cb)
      open()
      return () => statusListeners.delete(cb)
    },
    () => connected,
    () => false,
  )
}
