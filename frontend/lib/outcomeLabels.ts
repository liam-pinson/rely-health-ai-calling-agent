import type { CallStatus } from "@/lib/types";

// Plain-English versions of our own status enum values.
const STATUS_LABELS: Record<CallStatus, string> = {
  connecting: "connecting",
  dialing: "dialing",
  ongoing: "ongoing",
  closed: "closed",
  connection_failed: "connection failed",
  no_response: "no response",
};

// Plain-English versions of raw outcome_reason values. Covers the
// provider's disconnection_reason vocabulary (see CLAUDE.md's mapping
// table), the late-detected-voicemail upgrade string, and
// ProviderCallError's category bucket for connection_failed rows.
// Anything not listed here falls back to a humanized form of the raw
// value rather than being hidden -- see outcomeLabel().
const OUTCOME_LABELS: Record<string, string> = {
  user_hangup: "hung up",
  agent_hangup: "hung up",
  dial_no_answer: "no answer",
  voicemail_reached: "voicemail",
  "voicemail (detected late)": "voicemail",
  invalid_request: "invalid request",
  provider_config_error: "provider config error",
  unknown: "unknown error",
};

export function statusLabel(status: CallStatus): string {
  return STATUS_LABELS[status] ?? status;
}

function humanize(raw: string): string {
  return raw.replace(/_/g, " ");
}

export function outcomeLabel(outcomeReason: string | null): string | null {
  if (!outcomeReason) return null;
  return OUTCOME_LABELS[outcomeReason] ?? humanize(outcomeReason);
}

// e.g. "closed · voicemail", "no response · no answer" -- or just the
// plain status label when there's no outcome_reason to annotate it with.
export function terminalStatusLabel(
  status: CallStatus,
  outcomeReason: string | null
): string {
  const outcome = outcomeLabel(outcomeReason);
  return outcome ? `${statusLabel(status)} · ${outcome}` : statusLabel(status);
}