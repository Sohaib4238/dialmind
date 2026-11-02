"use client"

import { CircleCheckIcon, InfoIcon, Loader2Icon, OctagonXIcon, TriangleAlertIcon } from "lucide-react"
import { Toaster as Sonner, type ToasterProps } from "sonner"

// Dark only (v1). Small, bordered, bottom-right; success uses a check icon, not a green fill.
const Toaster = (props: ToasterProps) => (
  <Sonner
    theme="dark"
    position="bottom-right"
    className="toaster group"
    icons={{
      success: <CircleCheckIcon className="size-4 text-signal" />,
      info: <InfoIcon className="size-4 text-muted-foreground" />,
      warning: <TriangleAlertIcon className="size-4 text-warning" />,
      error: <OctagonXIcon className="size-4 text-danger" />,
      loading: <Loader2Icon className="size-4 animate-spin text-muted-foreground" />,
    }}
    toastOptions={{
      classNames: {
        toast: "!rounded-lg !border-border !bg-surface !text-[13px] !shadow-[0_8px_24px_rgba(0,0,0,0.4)]",
        description: "!text-muted-foreground",
        actionButton: "!bg-surface-2 !text-foreground",
      },
    }}
    style={
      {
        "--normal-bg": "var(--surface)",
        "--normal-text": "var(--text)",
        "--normal-border": "var(--line)",
        "--border-radius": "8px",
      } as React.CSSProperties
    }
    {...props}
  />
)

export { Toaster }
