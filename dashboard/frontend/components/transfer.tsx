import type { Call, TransferStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

export const TRANSFER_LABEL: Record<Exclude<TransferStatus, "">, string> = {
  unavailable: "No supervisor was available",
  waiting: "Waiting for a supervisor",
  accepted: "Supervisor accepted",
  declined: "Supervisors were busy",
  timeout: "No supervisor answered",
  transferring: "Transferring",
  transferred: "Transferred",
  failed: "Transfer didn't go through",
}

const TONE: Partial<Record<TransferStatus, string>> = {
  transferred: "text-signal",
  transferring: "text-signal",
  accepted: "text-signal",
  waiting: "text-warning",
  declined: "text-warning",
  timeout: "text-warning",
  failed: "text-danger",
}

/** The hand-off to the supervisor on one call: what happened and the summary he saw in the pop-up. */
export function TransferInfo({ call, className }: { call: Call; className?: string }) {
  const st = call.transfer_status
  // "unavailable" alone (set when dialing) only matters if the lead turned out interested
  if (!st || (st === "unavailable" && !call.transfer_summary && call.outcome !== "transfer_declined")) return null
  return (
    <div className={cn("space-y-1.5 rounded-md border border-border bg-surface-2 p-3 text-[13px]", className)}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className={cn("font-medium", TONE[st] ?? "text-foreground")}>
          {TRANSFER_LABEL[st] ?? st}
          {call.transfer_to_name && ["accepted", "transferring", "transferred", "failed"].includes(st) && ` · taken by ${call.transfer_to_name}`}
        </span>
        {call.transfer_note && <span className="text-[12px] text-faint">{call.transfer_note}</span>}
      </div>
      {call.transfer_summary && (
        <div>
          <div className="text-[12px] text-faint">Summary the supervisor saw</div>
          <p className="mt-0.5 leading-5 text-muted-foreground">{call.transfer_summary}</p>
        </div>
      )}
    </div>
  )
}
