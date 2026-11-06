<script setup lang="ts">
import { onMounted, computed, ref } from 'vue'
import { useDriveStore } from './composables/useDriveStore'
import { useRuntimeStore } from './composables/useRuntimeStore'
import { formatBytes, formatIso } from './api'
import Jobs from './pages/Jobs.vue'
import Tapes from './pages/Tapes.vue'
import Restore from './pages/Restore.vue'
import History from './pages/History.vue'

type Page = 'dashboard' | 'jobs' | 'tapes' | 'restore' | 'history'

const drive = useDriveStore()
const runtime = useRuntimeStore()
const page = ref<Page>('dashboard')

const status = computed(() => drive.state.status)
const lastUpdated = computed(() => {
  const updated = status.value?.updated_at
  if (!updated) return '—'
  const iso = updated.endsWith('Z') ? updated : updated + 'Z'
  return new Date(iso).toLocaleString('en-GB', { hour12: false })
})

const pages: { key: Page; label: string }[] = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'jobs', label: 'Jobs' },
  { key: 'tapes', label: 'Tapes' },
  { key: 'restore', label: 'Catalog' },
  { key: 'history', label: 'History' },
]

onMounted(() => {
  drive.fetchStatus()
  drive.connectSse()
  runtime.connectSse()
  void runtime.refreshSchedules()
})
</script>

<template>
  <header class="topbar">
    <h1>TapeWebUI</h1>
    <nav>
      <button
        v-for="item in pages"
        :key="item.key"
        :class="{ active: page === item.key }"
        @click="page = item.key"
      >
        {{ item.label }}
      </button>
    </nav>
    <span class="dim sse" :title="runtime.state.connected ? 'SSE connected' : 'SSE reconnecting…'">
      {{ runtime.state.connected ? '● live' : '○ offline' }}
    </span>
  </header>

  <main v-if="page === 'dashboard'">
    <div class="panel">
      <h2>Drive status</h2>
      <div v-if="status" class="kv-grid">
        <dt>State</dt>
        <dd>
          <span class="badge" :class="status.reachable ? 'ok' : 'fail'">
            {{ status.reachable ? 'Reachable' : 'Unreachable' }}
          </span>
        </dd>
        <dt>Drive</dt>
        <dd>
          <template v-if="status.manufacturer">
            {{ status.manufacturer }} {{ status.product_id }} (fw {{ status.revision }})
          </template>
          <template v-else>—</template>
        </dd>
        <dt>Position</dt>
        <dd>
          <template v-if="status.block_id !== null">
            partition {{ status.partition }}, block {{ status.block_id }}
            <span v-if="status.at_beginning_of_partition" class="dim"> (BOP)</span>
          </template>
          <template v-else>—</template>
        </dd>
        <dt>Compression</dt>
        <dd>{{ status.compression === null ? '—' : status.compression ? 'On' : 'Off' }}</dd>
        <dt>Updated</dt>
        <dd>{{ lastUpdated }}</dd>
      </div>
      <div v-else class="dim">Loading drive status…</div>
      <div v-if="status?.error" class="error-strip">{{ status.error }}</div>
      <div style="margin-top: 12px">
        <button class="primary" :disabled="drive.state.loading" @click="drive.refresh()">
          Refresh now
        </button>
      </div>
    </div>

    <div class="panel" v-if="runtime.state.job && runtime.state.job.state !== 'done'">
      <h2>Active job</h2>
      <div class="kv-grid">
        <dt>Job</dt><dd>{{ runtime.state.job.job_name || '—' }} ({{ runtime.state.job.state }})</dd>
        <dt>Files</dt><dd>{{ runtime.state.job.files_done }} / {{ runtime.state.job.files_total }}</dd>
        <dt>Bytes</dt>
        <dd>{{ formatBytes(runtime.state.job.bytes_done) }} / {{ formatBytes(runtime.state.job.bytes_total) }}</dd>
        <dt>Speed</dt><dd>{{ formatBytes(runtime.state.job.speed_bps) }}/s</dd>
        <dt>ETA</dt><dd>{{ runtime.state.job.eta_s !== null ? `${runtime.state.job.eta_s} s` : '—' }}</dd>
        <dt>Current file</dt><dd class="mono">{{ runtime.state.job.current_file || '—' }}</dd>
        <template v-if="runtime.state.job.hash_mismatches">
          <dt>Damaged</dt><dd class="error-text">{{ runtime.state.job.hash_mismatches }}</dd>
        </template>
        <dt v-if="runtime.state.job.error">Error</dt>
        <dd v-if="runtime.state.job.error" class="error-text">{{ runtime.state.job.error }}</dd>
      </div>
    </div>

    <div class="panel" v-if="runtime.state.schedules.length">
      <h2>Upcoming schedules</h2>
      <table class="table">
        <thead>
          <tr><th>Job</th><th>Kind</th><th>When</th><th>Next due</th><th>Catch-up</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in runtime.state.schedules" :key="row.name">
            <td>{{ row.name }} <span v-if="row.overdue" class="badge fail">overdue</span></td>
            <td>{{ row.kind }}</td>
            <td>
              <template v-if="row.kind === 'weekly'">{{ row.days.join(', ') }} </template>
              <template v-if="row.kind !== 'manual'">{{ row.time }}</template>
              <template v-else>manual</template>
            </td>
            <td>{{ formatIso(row.next_due) }}</td>
            <td>{{ row.catch_up }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </main>

  <main v-else-if="page === 'jobs'"><Jobs /></main>
  <main v-else-if="page === 'tapes'"><Tapes /></main>
  <main v-else-if="page === 'restore'"><Restore /></main>
  <main v-else-if="page === 'history'"><History /></main>
</template>
