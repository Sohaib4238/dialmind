"use client"

import { Lock, Phone } from "lucide-react"
import { createContext, useContext, useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api, ApiError, IS_REMOTE, LOGIN_REQUIRED_EVENT } from "@/lib/api"
import type { AuthMe } from "@/lib/types"

const RoleContext = createContext<AuthMe["role"]>("admin")

/** Show admin controls? True on this computer. It only hides buttons: the backend enforces the role (auth.py). */
export function useIsAdmin() {
  return useContext(RoleContext) === "admin"
}

/** On this computer: nothing. From another computer: a login form until signed in. */
export function LoginGate({ children }: { children: React.ReactNode }) {
  const [role, setRole] = useState<AuthMe["role"]>("admin")
  // Starts "checking" on both server and client (the server can't know the hostname). The pages must not mount before
  // the login is known: their first requests would fail with "Please log in", and a supervisor would see admin buttons.
  const [state, setState] = useState<"checking" | "in" | "out" | "off">("checking")

  useEffect(() => {
    if (!IS_REMOTE) {
      setState("in")
      return
    }
    api.me()
      .then((m) => { setRole(m.role); setState(m.user ? "in" : "out") })
      .catch((e) => setState(e instanceof ApiError && e.status === 404 ? "off" : "out"))
    const onLogout = () => setState("out")
    window.addEventListener(LOGIN_REQUIRED_EVENT, onLogout)
    return () => window.removeEventListener(LOGIN_REQUIRED_EVENT, onLogout)
  }, [])

  if (state === "in") return <RoleContext.Provider value={role}>{children}</RoleContext.Provider>
  if (state === "checking") return <div className="min-h-dvh bg-background" />
  return <LoginForm off={state === "off"} onDone={(r) => { setRole(r); setState("in") }} />
}

function LoginForm({ off, onDone }: { off: boolean; onDone: (role: AuthMe["role"]) => void }) {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError("")
    try {
      await api.login(username, password)
      onDone((await api.me()).role) // no reload: keeps the sound unlocked by this click; the live stream opens with the new cookie
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't reach the dashboard.")
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-dvh place-items-center bg-background px-4">
      <form onSubmit={submit} className="w-full max-w-[340px] space-y-4 rounded-lg border border-border bg-surface p-6">
        <div className="flex items-center gap-2.5">
          <div className="grid size-7 place-items-center rounded-md border border-line-strong bg-surface-2">
            <Phone className="size-3.5 text-signal" strokeWidth={1.75} />
          </div>
          <div className="leading-tight">
            <div className="text-[14px] font-semibold tracking-[-0.01em]">DialMind</div>
            <div className="text-[12px] text-faint">Sign in</div>
          </div>
        </div>
        {off ? (
          <p className="text-[13px] leading-5 text-muted-foreground">
            No login has been set up yet.
          </p>
        ) : (
          <>
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">Username</span>
              <Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus />
            </label>
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">Password</span>
              <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </label>
            {error && <p className="text-[12px] text-danger">{error}</p>}
            <Button type="submit" className="w-full" disabled={busy || !username || !password}>
              <Lock strokeWidth={1.5} />
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </>
        )}
      </form>
    </div>
  )
}
