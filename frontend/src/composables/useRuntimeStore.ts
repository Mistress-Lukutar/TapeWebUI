// Singleton SSE store: job runtime, restore progress and schedules.

import { reactive } from 'vue'

export interface RuntimeJob {
  job_name?: string
  state: string
  files_total: number
  files_done: number
  bytes_total: number
  bytes_done: number
  current_file: string
  speed_bps: number
  eta_s: number | null
  error: string
  kind?: string
  hash_mismatches?: number
  tape_id?: string
  tape_label?: string
  tapes_total?: number
  waiting_for_tape?: string
}

export interface ScheduleRow {
  name: string
  kind: string
  days: string[]
  time: string
  catch_up: string
  last_run_at: string | null
  next_due: string | null
  overdue: boolean
}

interface RuntimeStore {
  connected: boolean
  job: RuntimeJob | null
  queued: RuntimeJob[]
  restore: RuntimeJob | null
  schedules: ScheduleRow[]
}

const state = reactive<RuntimeStore>({
  connected: false,
  job: null,
  queued: [],
  restore: null,
  schedules: [],
})

let source: EventSource | null = null
let reconnectDelayMs = 1000

async function refreshSchedules(): Promise<void> {
  try {
    const response = await fetch('/api/schedules')
    if (response.ok) state.schedules = (await response.json()) as ScheduleRow[]
  } catch {
    /* non-fatal */
  }
}

async function refreshRuntime(): Promise<void> {
  try {
    const response = await fetch('/api/jobs/runtime/status')
    if (response.ok) {
      const payload = (await response.json()) as {
        active: RuntimeJob | null
        queued: RuntimeJob[]
      }
      state.job = payload.active
      state.queued = payload.queued ?? []
    }
  } catch {
    /* non-fatal */
  }
}

function connectSse(): void {
  source = new EventSource('/events')
  source.onopen = () => {
    state.connected = true
    reconnectDelayMs = 1000
  }
  source.onerror = () => {
    state.connected = false
    source?.close()
    source = null
    window.setTimeout(connectSse, reconnectDelayMs)
    reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000)
  }
  source.addEventListener('job', (event) => {
    const payload = JSON.parse((event as MessageEvent).data) as RuntimeJob
    if (payload.state === 'waiting_media' || payload.state !== 'done') {
      state.job = payload
    } else {
      state.job = payload // terminal snapshot (done/failed/cancelled)
      window.setTimeout(refreshRuntime, 200)
    }
  })
  source.addEventListener('restore', (event) => {
    state.restore = JSON.parse((event as MessageEvent).data) as RuntimeJob
  })
  source.addEventListener('schedule', () => {
    void refreshSchedules()
  })
}

export function useRuntimeStore() {
  return { state, connectSse, refreshSchedules, refreshRuntime }
}
