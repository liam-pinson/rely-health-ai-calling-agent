// fetch() rejects with a TypeError for actual network failures (server
// unreachable, DNS, offline) as opposed to a resolved-but-non-2xx response,
// which is handled separately by the caller. Used to give a clearer message
// for that specific case instead of a raw browser error string.
export function networkAwareMessage(err: unknown, fallback: string): string {
  if (err instanceof TypeError) {
    return "Could not reach the server. Check your connection and try again.";
  }
  if (err instanceof Error && err.message) {
    return err.message;
  }
  return fallback;
}
