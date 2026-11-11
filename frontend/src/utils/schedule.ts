/**
 * File:   utils/schedule.ts
 * Brief:  Render UTC schedule times in the browser's local timezone.
 * Author: Mistress-Lukutar
 * Date:   2026-11-11
 * Version: v0.11.3
 */

/**
 * Schedule times are stored and fired in UTC (scheduler_service.next_due),
 * so "daily 22:25" can mean a completely different wall-clock time — and
 * even a different weekday — where the user sits. These helpers convert
 * a UTC HH:MM into local time for display; the YAML keeps UTC as-is.
 */

const DAY_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const

export interface LocalSchedule {
  /** Local wall-clock time, HH:MM (24h). */
  time: string
  /** Local date offset from the UTC date: -1, 0 or 1 (midnight crossing). */
  dayShift: number
  /** Weekday names re-based onto local weekdays (weekly schedules). */
  days: string[]
}

/**
 * Convert a UTC HH:MM schedule time to the local timezone.
 *
 * The conversion anchors to today's date so midnight crossings surface as
 * a day shift. Weekly day names shift by the same amount, because a fixed
 * UTC time lands on every UTC date with the same local offset.
 *
 * Args:
 *   time: UTC time as "HH:MM".
 *   days: Optional UTC weekday names to re-base ("mon".."sun").
 *
 * Returns:
 *   LocalSchedule with local time, day shift and (possibly shifted) days.
 */
export function utcTimeToLocal(time: string, days: string[] = []): LocalSchedule {
  const [h, m] = time.split(':').map((part) => Number(part))
  const now = new Date()
  const local = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate(), h, m))
  const localDay = new Date(local.getFullYear(), local.getMonth(), local.getDate()).getTime()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const dayShift = Math.round((localDay - today) / 86400000)
  const shiftDay = (day: string): string => {
    const idx = DAY_NAMES.indexOf(day as (typeof DAY_NAMES)[number])
    if (idx < 0) return day
    return DAY_NAMES[(idx + dayShift + 7) % 7]
  }
  return {
    time: `${String(local.getHours()).padStart(2, '0')}:${String(local.getMinutes()).padStart(2, '0')}`,
    dayShift,
    days: days.map(shiftDay),
  }
}

/**
 * Format a single UTC HH:MM as local time, e.g. "01:25".
 *
 * Args:
 *   time: UTC time as "HH:MM".
 *
 * Returns:
 *   Local "HH:MM", or an empty string when the time is not parseable.
 */
export function formatLocalTime(time: string): string {
  if (!/^\d{1,2}:\d{2}$/.test(time)) return ''
  return utcTimeToLocal(time).time
}

/**
 * Format a schedule as a human-readable local-time summary.
 *
 * Examples: "daily 01:25", "weekly tue, fri 01:25".
 * Non-time kinds ("manual") pass through unchanged.
 *
 * Args:
 *   kind: Schedule kind ("manual" | "daily" | "weekly" or other).
 *   time: UTC time as "HH:MM".
 *   days: UTC weekday names for weekly schedules.
 *
 * Returns:
 *   Display string with local time, or just the kind for manual schedules.
 */
export function formatScheduleLocal(kind: string, time: string, days: string[] = []): string {
  if (kind !== 'daily' && kind !== 'weekly' || !/^\d{1,2}:\d{2}$/.test(time)) return kind
  const local = utcTimeToLocal(time, days)
  const when = kind === 'weekly' && local.days.length
    ? `${local.days.join(', ')} ${local.time}`
    : local.time
  return `${kind} ${when}`
}
