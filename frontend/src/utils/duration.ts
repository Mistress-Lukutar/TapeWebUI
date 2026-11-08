/**
 * File:   utils/duration.ts
 * Brief:  Human-readable duration formatting for ETA-style values.
 * Author: Mistress-Lukutar
 * Date:   2026-11-08
 * Version: v0.8.0
 */

/**
 * Format seconds as a compact human-readable duration.
 *
 * "222770084 s" is unreadable; large values render as "2d 13h 45m",
 * small ones keep seconds. Null/negative/non-finite → em dash.
 *
 * Args:
 *   seconds: Duration in seconds (may be null while unknown).
 *
 * Returns:
 *   Formatted string, e.g. "42s", "5m 30s", "1d 3h 12m", "—".
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return '—'
  const total = Math.round(seconds)
  if (total < 60) return `${total}s`
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  const parts: string[] = []
  if (days) parts.push(`${days}d`)
  if (hours) parts.push(`${hours}h`)
  if (minutes) parts.push(`${minutes}m`)
  if (secs && !days) parts.push(`${secs}s`)
  return parts.join(' ') || '0s'
}
