"use client";

import { useEffect, useState } from "react";
import type { Patient } from "@/lib/types";
import { networkAwareMessage } from "@/lib/errors";
import PatientTable from "@/components/PatientTable";

async function fetchPatients(): Promise<Patient[]> {
  const res = await fetch("/api/patients");
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(
      data?.detail ?? `Failed to load patients (HTTP ${res.status})`
    );
  }
  return data as Patient[];
}

export default function HomePage() {
  const [patients, setPatients] = useState<Patient[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetchPatients().then(
      (data) => {
        if (cancelled) return;
        setPatients(data);
        setError(null);
      },
      (err) => {
        if (cancelled) return;
        setError(networkAwareMessage(err, "Failed to load patients."));
      }
    );
    return () => {
      cancelled = true;
    };
  }, [retryToken]);

  return (
    <main className="mx-auto max-w-5xl px-6 py-10">
      <h1 className="text-2xl font-semibold text-primary mb-6">Patients</h1>
      {error && (
        <div className="mb-6 flex items-center gap-3">
          <p className="text-red-600">{error}</p>
          <button
            onClick={() => setRetryToken((n) => n + 1)}
            className="rounded-md border border-slate-300 px-3 py-1 text-sm text-foreground hover:bg-slate-100"
          >
            Retry
          </button>
        </div>
      )}
      {!error && patients === null && (
        <p className="text-slate-500">Loading patients…</p>
      )}
      {patients && <PatientTable patients={patients} />}
    </main>
  );
}
