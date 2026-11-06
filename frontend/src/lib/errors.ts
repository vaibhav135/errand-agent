/** A caught value as text for a message: ApiError carries the server's `detail`. */
export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}
