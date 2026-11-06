// Reactive drive status store: REST snapshot + SSE live updates.

import { reactive } from 'vue'

export interface DriveStatus {
  reachable: boolean
  no_medium: boolean
  manufacturer: string | null
  product_id: string | null
  revision: string | null
  partition: number | null
  block_id: number | null
  at_beginning_of_partition: boolean | null
  compression: boolean | null
  parms_block_size: number | null
  buffered_mode: boolean | null
  error: string | null
  updated_at: string
}

interface DriveStore {
  status: DriveStatus | null
  connected: boolean
  loading: boolean
  error: string | null
}

const state = reactive<DriveStore>({
  status: null,
  connected: false,
  loading: false,
  error: null,
})

let source: EventSource | null = null
let reconnectDelayMs = 1000

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
  source.addEventListener('drive', (event) => {
    try {
      state.status = JSON.parse((event as MessageEvent).data) as DriveStatus
    } catch {
      // Malformed payload: keep the last known good status.
    }
  })
}

async function fetchStatus(): Promise<void> {
  state.loading = true
  try {
    const response = await fetch('/api/drive/status')
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    state.status = (await response.json()) as DriveStatus
    state.error = null
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  } finally {
    state.loading = false
  }
}

async function refresh(): Promise<void> {
  state.loading = true
  try {
    const response = await fetch('/api/drive/refresh', { method: 'POST' })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    state.status = (await response.json()) as DriveStatus
    state.error = null
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  } finally {
    state.loading = false
  }
}

export function useDriveStore() {
  return {
    state,
    connectSse,
    fetchStatus,
    refresh,
  }
}
