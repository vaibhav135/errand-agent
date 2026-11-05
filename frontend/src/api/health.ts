export type HealthStatus = 'checking' | 'ok' | 'down'

export async function fetchHealth(signal?: AbortSignal): Promise<HealthStatus> {
  try {
    const res = await fetch('/api/health', { signal })
    if (!res.ok) return 'down'
    const body: { status?: string } = await res.json()
    return body.status === 'ok' ? 'ok' : 'down'
  } catch {
    return 'down'
  }
}
