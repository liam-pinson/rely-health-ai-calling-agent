import { API_BASE_URL } from "@/lib/config";
import { proxyJson } from "@/lib/proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ callId: string }> }
) {
  const { callId } = await params;
  return proxyJson(`${API_BASE_URL}/calls/${callId}`);
}
