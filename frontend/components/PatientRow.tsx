"use client";

import { useEffect, useState } from "react";
import type { CallLog, Patient } from "@/lib/types";
import { isTerminalStatus } from "@/lib/types";
import { networkAwareMessage } from "@/lib/errors";
import { statusLabel, terminalStatusLabel } from "@/lib/outcomeLabels";

const POLL_INTERVAL_MS = 2500;

export default function PatientRow({ patient }: { patient: Patient }) {
  const [callLog, setCallLog] = useState<CallLog | null>(null);
  const [placing, setPlacing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function placeCall() {
    setPlacing(true);
    setError(null);
    try {
      const res = await fetch(`/api/patients/${patient.id}/call`, {
        method: "POST",
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(data?.detail ?? `Call failed (HTTP ${res.status})`);
      }
      setCallLog(data as CallLog);
    } catch (err) {
      setError(networkAwareMessage(err, "Failed to place call."));
    } finally {
      setPlacing(false);
    }
  }

  // Poll the call's status until it reaches a terminal state.
  useEffect(() => {
    if (!callLog || isTerminalStatus(callLog.status)) {
      return;
    }

    const callId = callLog.call_id;
    let cancelled = false;

    const intervalId = setInterval(async () => {
      try {
        const res = await fetch(`/api/calls/${callId}`);
        const data = await res.json().catch(() => null);
        if (cancelled) return;

        if (res.status === 404) {
          // The call vanished server-side -- this will never resolve
          // differently, so stop polling instead of retrying forever.
          setError("This call could not be found anymore. Stopped checking.");
          clearInterval(intervalId);
          return;
        }
        if (!res.ok) {
          throw new Error(
            data?.detail ?? `Status check failed (HTTP ${res.status})`
          );
        }
        setError(null);
        setCallLog(data as CallLog);
      } catch (err) {
        // Transient failures (network blip, proxy 502) keep retrying on
        // the next tick rather than giving up.
        if (!cancelled) {
          setError(networkAwareMessage(err, "Failed to check call status."));
        }
      }
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
    // Deliberately depend on the primitive call_id/status, not the whole
    // callLog object -- polling itself replaces callLog on every tick, and
    // depending on the object reference would tear down and rebuild the
    // interval every 2.5s instead of only when call_id or status changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callLog?.call_id, callLog?.status]);

  const inProgress = callLog !== null && !isTerminalStatus(callLog.status);
  const buttonLabel = placing ? "Calling…" : callLog ? "Call again" : "Call";

  return (
    <tr className="border-b border-slate-100 align-top last:border-0">
      <td className="py-3 px-4">
        {patient.first_name} {patient.last_name}
      </td>
      <td className="py-3 px-4">{patient.date_of_birth}</td>
      <td className="py-3 px-4">
        {patient.appointment_date} {patient.appointment_time}
      </td>
      <td className="py-3 px-4">{patient.timezone}</td>
      <td className="py-3 px-4">{patient.phone_number}</td>
      <td className="py-3 px-4">
        <button
          onClick={placeCall}
          disabled={placing || inProgress}
          className="rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
        >
          {buttonLabel}
        </button>
        {callLog && (
          <p className="mt-1.5 text-sm text-slate-600">
            status:{" "}
            {isTerminalStatus(callLog.status)
              ? terminalStatusLabel(callLog.status, callLog.outcome_reason)
              : statusLabel(callLog.status)}
            {inProgress && " (polling…)"}
          </p>
        )}
        {error && <p className="mt-1.5 text-sm text-red-600">{error}</p>}
      </td>
    </tr>
  );
}
