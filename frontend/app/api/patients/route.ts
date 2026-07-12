import { API_BASE_URL } from "@/lib/config";
import { proxyJson } from "@/lib/proxy";

export async function GET() {
  return proxyJson(`${API_BASE_URL}/patients`);
}
