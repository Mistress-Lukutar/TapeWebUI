// Thin fetch helpers for the TapeWebUI API.

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function apiGet<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) {
    const detail = await response.text()
    throw new ApiError(response.status, detail || `HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

export async function apiSend<T>(
  url: string,
  method: 'POST' | 'PUT' | 'DELETE',
  body?: unknown,
): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const payload = await response.json()
      if (payload.detail) message = String(payload.detail)
    } catch {
      /* keep default message */
    }
    throw new ApiError(response.status, message)
  }
  return (await response.json()) as T
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let unit = 'B'
  for (const next of units) {
    if (value < 1024) break
    value /= 1024
    unit = next
  }
  return `${value.toFixed(1)} ${unit}`
}

export function formatIso(value: string | null | undefined): string {
  if (!value) return '—'
  const iso = value.endsWith('Z') || value.includes('+') ? value : value + 'Z'
  const date = new Date(iso)
  return isNaN(date.getTime()) ? value : date.toLocaleString('en-GB', { hour12: false })
}

// Catalog explorer types (GET /api/catalog/*).

export interface DirSummary {
  path: string
  name: string
  files_count: number
  bytes_total: number
}

export interface SessionFileRow {
  id: number
  path: string
  name: string
  size: number
  mtime: string
  sha256: string
  status: string
  tape_file_index: number
}

export interface FileVersionRow {
  file_id: number
  session_id: number
  job_name: string
  session_started_at: string
  size: number
  mtime: string
  sha256: string
  status: string
  tape_file_index: number
}

export interface MergedFileRow {
  path: string
  name: string
  versions: FileVersionRow[]
}

export interface CatalogListing {
  mode: 'session' | 'tape'
  session_id: number | null
  tape_id: number | null
  dir: string
  dirs: DirSummary[]
  files: SessionFileRow[]
  merged_files: MergedFileRow[]
}

export interface SelectionSummary {
  file_ids: number[]
  files_count: number
  bytes_total: number
}

// Restore plan/request types (POST /api/restore/*).

export interface PlanSelection {
  session_id?: number | null
  tape_id?: number | null
  prefix: string
  latest_only: boolean
}

export interface PlanItem {
  file_id: number
  session_id: number
  tape_file_index: number
  path: string
  size: number
  sha256: string
}

export interface PlanTapeGroup {
  tape_id: string
  tape_label: string
  items: PlanItem[]
}

export interface RestorePlan {
  tapes: PlanTapeGroup[]
  bytes_total: number
  files_count: number
}
