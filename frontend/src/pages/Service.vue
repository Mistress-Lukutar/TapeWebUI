<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiGet, apiSend, formatIso } from '../api'

interface StatisticsResponse {
  reachable: boolean
  manufacturer: string | null
  product_id: string | null
  revision: string | null
  serial: string | null
  statistics: Record<string, string>
  updated_at: string
}

interface DriveOpResponse {
  op: string
  output: string
  status: { reachable: boolean }
}

interface OpDef {
  op: string
  label: string
  title: string
  danger?: boolean
  confirm?: string
}

// Manual operations mirroring the ITDT-SE command set for a single drive.
const OPS: OpDef[] = [
  { op: 'tur', label: 'Test unit ready', title: 'Issue SCSI Test Unit Ready' },
  { op: 'load', label: 'Load cartridge', title: 'Load the cartridge into the drive' },
  { op: 'unload', label: 'Unload / eject', title: 'Unload and eject the cartridge' },
  { op: 'lock', label: 'Lock door', title: 'Prevent medium removal (SCSI prevent)' },
  { op: 'unlock', label: 'Unlock door', title: 'Allow medium removal (SCSI allow)' },
  { op: 'reserve', label: 'Reserve unit', title: 'SCSI reserve unit' },
  { op: 'release', label: 'Release unit', title: 'SCSI release unit' },
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

const stats = ref<StatisticsResponse | null>(null)
const loading = ref(false)
const running = ref('')
const consoleOutput = ref<string[]>([])
const error = ref('')

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

async function runOp(def: OpDef): Promise<void> {
  if (def.confirm && !window.confirm(def.confirm)) return
  running.value = def.op
  error.value = ''
  try {
    const body = await apiSend<DriveOpResponse>(`/api/service/ops/${def.op}`, 'POST')
    consoleOutput.value.unshift(`[${def.op}] ${body.output}`)
    if (consoleOutput.value.length > 20) consoleOutput.value.pop()
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    consoleOutput.value.unshift(`[${def.op}] FAILED: ${message}`)
    if (consoleOutput.value.length > 20) consoleOutput.value.pop()
    error.value = message
  } finally {
    running.value = ''
  }
}

onMounted(load)
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
