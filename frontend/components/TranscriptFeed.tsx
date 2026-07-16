"use client";

import { useEffect, useRef, useState } from "react";
import type { Escalation, TranscriptTurn } from "@/lib/types";
import { transcriptFeedUrl } from "@/lib/transcriptFeedUrl";

const ROLE_LABEL: Record<TranscriptTurn["role"], string> = {
  navigator: "Navigator",
  patient: "Patient",
  unknown: "Unknown",
};

const SEVERITY_RANK: Record<Escalation["severity"], number> = { low: 0, high: 1 };

export default function TranscriptFeed({ callId }: { callId: string }) {
  const [turns, setTurns] = useState<TranscriptTurn[]>([]);
  const [unavailable, setUnavailable] = useState(false);
  const [escalation, setEscalation] = useState<Escalation | null>(null);
  // Tracks whether the server sent its own "call_ended" message -- the
  // socket closing right after that is expected, not a failure, so
  // onclose shouldn't show the "unavailable" notice in that case.
  const endedCleanly = useRef(false);

  // Fetched once on mount/reconnect so the banner survives a page reload
  // mid-call -- a safety indicator that disappears on refresh is worse
  // than no indicator at all.
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/calls/${callId}/escalation`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!cancelled && data) setEscalation(data as Escalation);
      })
      .catch(() => {
        // No escalation yet, or the fetch failed -- either way, the live
        // socket below is still the primary channel; this is best-effort.
      });
    return () => {
      cancelled = true;
    };
  }, [callId]);

  useEffect(() => {
    setTurns([]);
    setUnavailable(false);
    endedCleanly.current = false;

    const socket = new WebSocket(transcriptFeedUrl(callId));

    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "turn") {
        const turn = message as TranscriptTurn;
        // Upsert by turn_index, not append -- the backend resends a
        // turn_index whenever its content grows (Retell delivers the same
        // utterance's transcription progressively), so a resend must
        // replace the existing line in place rather than duplicate it.
        setTurns((prev) => {
          const i = prev.findIndex((t) => t.turn_index === turn.turn_index);
          if (i === -1) {
            return [...prev, turn].sort((a, b) => a.turn_index - b.turn_index);
          }
          const next = [...prev];
          next[i] = turn;
          return next;
        });
      } else if (message.type === "escalation") {
        // Mirrors the server-side ratchet on the client too: never clears,
        // never downgrades, even if a stale message somehow arrived out of
        // order.
        const incomingSeverity = message.severity as Escalation["severity"];
        setEscalation((prev) => {
          if (prev && SEVERITY_RANK[prev.severity] >= SEVERITY_RANK[incomingSeverity]) {
            return prev;
          }
          return {
            severity: incomingSeverity,
            status: "notified",
            matched_phrase: message.matched_phrase,
            flagged_role: message.flagged_role,
          };
        });
      } else if (message.type === "call_ended") {
        endedCleanly.current = true;
      }
    };

    // No reconnect loop on error/early close -- deliberately out of scope
    // (see plan). The existing CallLog status polling in PatientRow.tsx
    // stays authoritative regardless of whether this channel works.
    socket.onerror = () => {
      if (!endedCleanly.current) setUnavailable(true);
    };
    socket.onclose = () => {
      if (!endedCleanly.current) setUnavailable(true);
    };

    return () => {
      socket.close();
    };
  }, [callId]);

  if (turns.length === 0 && !unavailable && !escalation) {
    return null;
  }

  return (
    <div className="mx-auto w-full max-w-3xl">
      {escalation && (
        <div
          className={`mb-2 rounded-md border p-3 text-sm ${
            escalation.severity === "high"
              ? "border-red-300 bg-red-50 text-red-800"
              : "border-amber-300 bg-amber-50 text-amber-800"
          }`}
        >
          <span className="font-semibold">
            {escalation.severity === "high" ? "High-severity escalation" : "Escalation flagged"}
          </span>
          {escalation.flagged_role && (
            <span>
              {" "}
              &middot; {ROLE_LABEL[escalation.flagged_role as TranscriptTurn["role"]] ?? escalation.flagged_role}
              {escalation.matched_phrase && <>: "{escalation.matched_phrase}"</>}
            </span>
          )}
        </div>
      )}
      <div className="max-h-64 overflow-y-auto rounded-md border border-slate-200 bg-slate-50 p-4 text-sm">
        {unavailable && (
          <p className="text-slate-500">Live transcript unavailable.</p>
        )}
        {turns.map((turn) => (
          <p key={turn.turn_index} className="mb-2 leading-relaxed last:mb-0">
            <span className="font-medium text-slate-700">
              {ROLE_LABEL[turn.role]}:{" "}
            </span>
            <span className="text-slate-600">{turn.content}</span>
          </p>
        ))}
      </div>
    </div>
  );
}
