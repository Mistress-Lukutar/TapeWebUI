<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiGet, apiSend, formatIso } from '../api'
import { useConfirm } from '../composables/useConfirm'

interface StatisticsResponse {
  reachable: boolean
  manufacturer: string | null
  product_id: string | null
  revision: string | null
  serial: string | null
  statistics: Record<string, string>
  updated_at: string
}

interface OpsState {
  door_locked: boolean
  unit_reserved: boolean
}

interface DriveOpResponse {
  op: string
  output: string
  status: { reachable: boolean; no_medium: boolean }
  state: OpsState
}

interface OpsStateResponse extends OpsState {
  cartridge_loaded: boolean | null
}

interface OpDef {
  op: string
  label: string
  title: string
  danger?: boolean
  confirm?: string
  disabled?: boolean
}

// Manual single-shot operations (paired ones are rendered as toggles).
const OPS: OpDef[] = [
  { op: 'tur', label: 'Test unit ready', title: 'Issue SCSI Test Unit Ready' },
  { op: 'rewind', label: 'Rewind', title: 'Rewind to beginning of partition' },
  { op: 'seod', label: 'Space to EOD', title: 'Space to end of data (append point)' },
  {
    op: 'erase_short',
    label: 'Short erase',
    title: 'Erase from BOP (fast, blocks invalidation)',
    danger: true,
    confirm: 'Short erase DESTROYS all data on the tape. Continue?',
  },
  {
    op: 'erase_long',
    label: 'Long erase',
    title: 'Full physical erase from BOP (very slow)',
    danger: true,
    confirm: 'Long erase DESTROYS all data and can take hours. Continue?',
  },
]

// Friendly labels for the lifecycle counters (devicestatistics output).
const STAT_LABELS: Record<string, string> = {
  lifetime_poh: 'Lifetime power-on hours',
  lifetime_mmh: 'Lifetime motion mechanism hours',
  lifetime_media_loads: 'Lifetime media loads',
  lifetime_cleaning_op: 'Lifetime cleaning operations',
  lifetime_power_cycles: 'Lifetime power cycles',
  lt_meters_tape_processed: 'Tape meters processed',
  lt_mmh_incompatib_media: 'MMH with incompatible media',
  lt_poh_temperature_cond: 'POH under temperature cond.',
  lt_poh_power_cons_cond: 'POH under power cond.',
  lt_poh_forced_reset_eject: 'POH forced reset/eject',
  mmh_since_last_clean: 'MMH since last clean',
  mmh_since_2_last_clean: 'MMH since 2-last clean',
  mmh_since_3_last_clean: 'MMH since 3-last clean',
  vol_loads_since_par_reset: 'Vol loads since param reset',
  hard_write_errors: 'Hard write errors',
  hard_read_errors: 'Hard read errors',
  duty_cycle_sample_time: 'Duty cycle sample time',
  read_duty_cycle: 'Read duty cycle',
  write_duty_cycle: 'Write duty cycle',
  activity_duty_cycle: 'Activity duty cycle',
  vol_not_present_duty_cycle: 'No-media duty cycle',
  ready_duty_cycle: 'Ready duty cycle',
  drive_mfg_sn: 'Drive MFG serial',
  drive_sn: 'Drive serial',
}

const { confirm } = useConfirm()
const stats = ref<StatisticsResponse | null>(null)
const loading = ref(false)
const running = ref('')
const consoleOutput = ref<string[]>([])
const error = ref('')
const opsState = ref<OpsStateResponse>({
  door_locked: false,
  unit_reserved: false,
  cartridge_loaded: null,
})

// Paired toggles: label/op flip with the tracked state; the button gets
// a "pressed" look while the active side (locked / reserved) is on.
const cartridgeToggle = computed<OpDef>(() => ({
  op: opsState.value.cartridge_loaded ? 'unload' : 'load',
  label: opsState.value.cartridge_loaded ? 'Unload / eject' : 'Load cartridge',
  title: opsState.value.cartridge_loaded
    ? 'Unload and eject the cartridge'
    : 'Load the cartridge into the drive',
  disabled: opsState.value.cartridge_loaded === null,
}))
const doorToggle = computed<OpDef>(() => ({
  op: opsState.value.door_locked ? 'unlock' : 'lock',
  label: opsState.value.door_locked ? 'Unlock door' : 'Lock door',
  title: opsState.value.door_locked
    ? 'Allow medium removal (SCSI allow)'
    : 'Prevent medium removal (SCSI prevent)',
}))
const reserveToggle = computed<OpDef>(() => ({
  op: opsState.value.unit_reserved ? 'release' : 'reserve',
  label: opsState.value.unit_reserved ? 'Release unit' : 'Reserve unit',
  title: opsState.value.unit_reserved
    ? 'SCSI release unit'
    : 'SCSI reserve unit',
}))

function applyOpState(response: DriveOpResponse): void {
  opsState.value.door_locked = response.state.door_locked
  opsState.value.unit_reserved = response.state.unit_reserved
  opsState.value.cartridge_loaded = response.status.reachable
    ? !response.status.no_medium
    : null
}

const orderedStats = computed<[string, string][]>(() => {
  if (!stats.value) return []
  const entries = Object.entries(stats.value.statistics)
  const known = entries.filter(([key]) => STAT_LABELS[key])
  known.sort((a, b) => Object.keys(STAT_LABELS).indexOf(a[0]) - Object.keys(STAT_LABELS).indexOf(b[0]))
  const unknown = entries.filter(([key]) => !STAT_LABELS[key])
  return [...known, ...unknown]
})

function statLabel(key: string): string {
  return STAT_LABELS[key] ?? key.replaceAll('_', ' ')
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    stats.value = await apiGet<StatisticsResponse>('/api/service/statistics')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function loadOpsState(): Promise<void> {
  try {
    opsState.value = await apiGet<OpsStateResponse>('/api/service/ops/state')
  } catch {
    // Keep the defaults; the buttons still work (the backend tracks state).
  }
}

async function runOp(def: OpDef): Promise<void> {
  if (
    def.confirm &&
    !(await confirm({
      title: def.label || def.op,
      message: def.confirm,
      confirmLabel: 'Run',
      danger: def.danger,
    }))
  )
    return
  running.value = def.op
  error.value = ''
  try {
    const body = await apiSend<DriveOpResponse>(`/api/service/ops/${def.op}`, 'POST')
    consoleOutput.value.unshift(`[${def.op}] ${body.output}`)
    if (consoleOutput.value.length > 20) consoleOutput.value.pop()
    applyOpState(body)
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    consoleOutput.value.unshift(`[${def.op}] FAILED: ${message}`)
    if (consoleOutput.value.length > 20) consoleOutput.value.pop()
    error.value = message
  } finally {
    running.value = ''
  }
}

onMounted(() => {
  load()
  loadOpsState()
})
</script>

<template>
  <div class="columns">
    <div class="panel half">
      <h2>Drive identity</h2>
      <div v-if="stats" class="kv-grid">
        <dt>Drive</dt>
        <dd>{{ stats.manufacturer || '—' }} {{ stats.product_id || '' }} (fw {{ stats.revision || '—' }})</dd>
        <dt>Serial</dt>
        <dd class="mono">{{ stats.serial || '—' }}</dd>
        <dt>Reachable</dt>
        <dd>
          <span class="badge" :class="stats.reachable ? 'ok' : 'fail'">
            {{ stats.reachable ? 'Yes' : 'No' }}
          </span>
        </dd>
        <dt>Collected</dt>
        <dd>{{ formatIso(stats.updated_at) }}</dd>
      </div>
      <p v-else class="dim">{{ loading ? 'Collecting device statistics…' : 'No data.' }}</p>
      <div style="margin-top: 12px">
        <button class="primary" :disabled="loading" @click="load()">Refresh statistics</button>
      </div>
      <div v-if="error" class="error-strip" style="margin-top: 12px">{{ error }}</div>
    </div>

    <div class="panel half">
      <h2>Cartridge operations</h2>
      <p class="dim">
        Manual ITDT-SE operations; refused while a job is running. Erase operations
        destroy all data on the loaded tape.
      </p>
      <div class="op-grid">
        <button
          class="op-btn"
          :class="{ toggled: opsState.door_locked }"
          :title="doorToggle.title"
          :disabled="running !== ''"
          @click="runOp(doorToggle)"
        >
          {{ running === doorToggle.op ? '…' : doorToggle.label }}
        </button>
        <button
          class="op-btn"
          :class="{ toggled: opsState.unit_reserved }"
          :title="reserveToggle.title"
          :disabled="running !== ''"
          @click="runOp(reserveToggle)"
        >
          {{ running === reserveToggle.op ? '…' : reserveToggle.label }}
        </button>
        <button
          class="op-btn"
          :title="cartridgeToggle.title"
          :disabled="running !== '' || cartridgeToggle.disabled"
          @click="runOp(cartridgeToggle)"
        >
          {{ running === cartridgeToggle.op ? '…' : cartridgeToggle.label }}
        </button>
        <button
          v-for="def in OPS"
          :key="def.op"
          class="op-btn"
          :class="{ danger: def.danger }"
          :title="def.title"
          :disabled="running !== ''"
          @click="runOp(def)"
        >
          {{ running === def.op ? '…' : def.label }}
        </button>
      </div>
      <template v-if="consoleOutput.length">
        <h2 style="margin-top: 16px">Operation log</h2>
        <pre class="console">{{ consoleOutput.join('\n') }}</pre>
      </template>
    </div>
  </div>

  <div class="panel" style="margin-top: 16px">
    <h2>Lifecycle statistics</h2>
    <p class="dim">
      Collected from drive log pages via <span class="mono">devicestatistics</span>
      (same data the IBM ITDT-SE utility reports).
    </p>
    <div v-if="stats" class="stats-grid">
      <div v-for="[key, value] in orderedStats" :key="key" class="stat-cell">
        <span class="stat-label">{{ statLabel(key) }}</span>
        <span class="stat-value mono" :class="{ 'error-text': key.includes('errors') && value !== '0' }">
          {{ value }}
        </span>
      </div>
    </div>
    <p v-else class="dim">No statistics collected yet.</p>
  </div>
</template>

<style scoped>
.op-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.op-btn.danger {
  border-color: var(--danger, #b3413c);
  color: var(--danger, #e07a75);
}

.op-btn.toggled {
  background: rgba(53, 201, 142, 0.15);
  border-color: var(--accent, #35c98e);
  color: var(--accent, #35c98e);
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.stat-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid var(--border, #2a2a35);
  border-radius: 6px;
}

.stat-label {
  font-size: 12px;
  color: var(--text-dim);
}

.stat-value {
  font-size: 15px;
}

.console {
  max-height: 220px;
  overflow: auto;
  font-size: 12px;
  white-space: pre-wrap;
  padding: 10px;
  border: 1px solid var(--border, #2a2a35);
  border-radius: 6px;
}
</style>
