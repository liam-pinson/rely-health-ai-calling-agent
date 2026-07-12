import { API_BASE_URL } from "@/lib/config";
import { proxyJson } from "@/lib/proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ patientId: string }> }
) {
  const { patientId } = await params;
  return proxyJson(`${API_BASE_URL}/patients/${patientId}/call`, {
    method: "POST",
  });
}
