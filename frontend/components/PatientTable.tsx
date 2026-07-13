import type { Patient } from "@/lib/types";
import PatientRow from "@/components/PatientRow";

export default function PatientTable({ patients }: { patients: Patient[] }) {
  if (patients.length === 0) {
    return <p className="text-slate-500">No patients found.</p>;
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-left">
            <th className="py-3 px-4 font-medium text-slate-500">Name</th>
            <th className="py-3 px-4 font-medium text-slate-500">
              Date of birth
            </th>
            <th className="py-3 px-4 font-medium text-slate-500">
              Appointment
            </th>
            <th className="py-3 px-4 font-medium text-slate-500">
              Timezone
            </th>
            <th className="py-3 px-4 font-medium text-slate-500">Phone</th>
            <th className="py-3 px-4 font-medium text-slate-500">Call</th>
          </tr>
        </thead>
        <tbody>
          {patients.map((patient) => (
            <PatientRow key={patient.id} patient={patient} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
