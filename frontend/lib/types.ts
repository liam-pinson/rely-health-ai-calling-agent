export interface Patient {
  id: string;
  first_name: string;
  last_name: string;
  date_of_birth: string;
  phone_number: string;
  appointment_date: string;
  appointment_time: string;
  timezone: string;
}

export type CallStatus =
  | "connecting"
  | "dialing"
  | "ongoing"
  | "closed"
  | "connection_failed"
  | "no_response";

export interface CallLog {
  call_id: string;
  patient_id: string;
  provider_call_id: string | null;
  status: CallStatus;
  started_at: string;
  ended_at: string | null;
  error_reason: string | null;
  outcome_reason: string | null;
}

// Statuses that will never transition further -- polling stops here.
export const TERMINAL_CALL_STATUSES: readonly CallStatus[] = [
  "closed",
  "connection_failed",
  "no_response",
];

export function isTerminalStatus(status: CallStatus): boolean {
  return TERMINAL_CALL_STATUSES.includes(status);
}

export interface TranscriptTurn {
  turn_index: number;
  role: "navigator" | "patient" | "unknown";
  content: string;
  started_at: number | null;
  ended_at: number | null;
}

export interface Escalation {
  severity: "high" | "low";
  status: "pending" | "notified";
  matched_phrase: string | null;
  flagged_role: string | null;
}
