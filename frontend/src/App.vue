<script setup lang="ts">
import { onMounted, computed, ref } from 'vue'
import { useDriveStore } from './composables/useDriveStore'
import { useRuntimeStore } from './composables/useRuntimeStore'
import { formatBytes, formatIso } from './api'
import Jobs from './pages/Jobs.vue'
import Tapes from './pages/Tapes.vue'
import Restore from './pages/Restore.vue'
import History from './pages/History.vue'
import Settings from './pages/Settings.vue'
import Service from './pages/Service.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'

type Page = 'dashboard' | 'jobs' | 'tapes' | 'restore' | 'history' | 'settings' | 'service'

const drive = useDriveStore()
const runtime = useRuntimeStore()
const page = ref<Page>('dashboard')

const status = computed(() => drive.state.status)
const currentLabel = computed(() => pages.find((item) => item.key === page.value)?.label ?? '')
const lastUpdated = computed(() => {
  const updated = status.value?.updated_at
  if (!updated) return '—'
  const iso = updated.endsWith('Z') ? updated : updated + 'Z'
  return new Date(iso).toLocaleString('en-GB', { hour12: false })
})

// Sidebar nav entries; icon markup is trusted static SVG paths (feather-style strokes).
const pages: { key: Page; label: string; icon: string }[] = [
  {
    key: 'dashboard',
    label: 'Dashboard',
    icon: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
  },
  {
    key: 'jobs',
    label: 'Jobs',
    icon: '<polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
  },
  {
    key: 'tapes',
    label: 'Tapes',
    icon: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
  },
  {
    key: 'restore',
    label: 'Catalog',
    icon: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  },
  {
    key: 'history',
    label: 'History',
    icon: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  },
  {
    key: 'settings',
    label: 'Settings',
    icon: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  },
  {
    key: 'service',
    label: 'Service',
    icon: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  },
]

onMounted(() => {
  drive.fetchStatus()
  drive.connectSse()
  runtime.connectSse()
  void runtime.refreshSchedules()
})
</script>

<template>
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-logo">
        <span class="logo">TapeWebUI</span>
      </div>
      <nav class="sidebar-nav">
        <button
          v-for="item in pages"
          :key="item.key"
          class="nav-btn"
          :class="{ active: page === item.key }"
          @click="page = item.key"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            width="17"
            height="17"
            class="nav-icon"
            v-html="item.icon"
          />
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <div class="sidebar-footer">
        <span class="dim sse" :title="runtime.state.connected ? 'SSE connected' : 'SSE reconnecting…'">
          {{ runtime.state.connected ? '● live' : '○ offline' }}
        </span>
      </div>
    </aside>

    <div class="main-content">
      <header class="page-header">
        <h1>{{ currentLabel }}</h1>
      </header>

      <main v-if="page === 'dashboard'">
        <div class="columns">
          <div class="panel half">
            <h2>Drive status</h2>
            <div v-if="status" class="kv-grid">
              <dt>State</dt>
              <dd>
                <span
                  class="badge"
                  :class="status.reachable ? (status.no_medium ? 'warn' : 'ok') : 'fail'"
                >
                  {{ status.reachable ? (status.no_medium ? 'No cartridge' : 'Reachable') : 'Unreachable' }}
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
                <template v-else-if="status.no_medium">no cartridge loaded</template>
                <template v-else>—</template>
              </dd>
              <dt>Compression</dt>
              <dd>{{ status.compression === null ? '—' : status.compression ? 'On' : 'Off' }}</dd>
              <dt>Updated</dt>
              <dd>{{ lastUpdated }}</dd>
            </div>
            <div v-else class="dim">Loading drive status…</div>
            <div v-if="status?.no_medium" class="info-strip">
              No cartridge loaded in the drive. Position and compression
              become available after a tape is inserted.
            </div>
            <div v-else-if="status?.error" class="error-strip">{{ status.error }}</div>
            <div style="margin-top: 12px">
              <button class="primary" :disabled="drive.state.loading" @click="drive.refresh()">
                Refresh now
              </button>
            </div>
          </div>

          <div class="panel half" v-if="runtime.state.schedules.length">
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
      </main>

      <main v-else-if="page === 'jobs'"><Jobs /></main>
      <main v-else-if="page === 'tapes'"><Tapes /></main>
      <main v-else-if="page === 'restore'"><Restore /></main>
      <main v-else-if="page === 'history'"><History /></main>
      <main v-else-if="page === 'settings'"><Settings /></main>
      <main v-else-if="page === 'service'"><Service /></main>
    </div>
  </div>
  <ConfirmDialog />
</template>
