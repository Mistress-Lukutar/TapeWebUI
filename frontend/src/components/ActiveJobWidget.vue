<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRuntimeStore } from '../composables/useRuntimeStore'
import { formatBytes } from '../api'
import { formatDuration } from '../utils/duration'

// Floating summary of the running job, shown on every page except Jobs.
// Collapsed it is a one-line pill; expanding reveals the full kv stats.

const runtime = useRuntimeStore()
const expanded = ref(false)

// Prefer the backup job; fall back to a running restore. Terminal states
// are transient (the store refreshes right after), so hide 'done'.
const job = computed(() => {
  if (runtime.state.job && runtime.state.job.state !== 'done') return runtime.state.job
  if (runtime.state.restore && runtime.state.restore.state !== 'done') {
    return runtime.state.restore
  }
  return null
})

const kindLabel = computed(() =>
  runtime.state.job && runtime.state.job.state !== 'done' ? 'Backup' : 'Restore',
)

const percent = computed(() => {
  const active = job.value
  if (!active || !active.bytes_total) return null
  return Math.min(100, Math.round((active.bytes_done / active.bytes_total) * 100))
})
</script>

<template>
  <div v-if="job" class="job-widget" :class="{ expanded }">
    <button class="job-widget-bar" @click="expanded = !expanded">
      <span
        class="job-widget-dot"
        :class="{ wait: job.state === 'waiting_media', error: job.error }"
      />
      <span class="job-widget-label">{{ kindLabel }}: {{ job.job_name || '—' }}</span>
      <span v-if="percent !== null" class="job-widget-pct">{{ percent }}%</span>
      <span class="job-widget-chevron">{{ expanded ? '▾' : '▴' }}</span>
    </button>
    <div v-if="expanded" class="job-widget-detail">
      <div class="job-widget-progress">
        <div
          class="job-widget-fill"
          :class="{ error: job.error }"
          :style="{ width: (percent ?? 0) + '%' }"
        />
      </div>
      <dl class="job-widget-grid">
        <dt>State</dt><dd>{{ job.state }}</dd>
        <dt>Files</dt><dd>{{ job.files_done }} / {{ job.files_total }}</dd>
        <dt>Bytes</dt><dd>{{ formatBytes(job.bytes_done) }} / {{ formatBytes(job.bytes_total) }}</dd>
        <dt>Speed</dt><dd>{{ formatBytes(job.speed_bps) }}/s</dd>
        <dt>ETA</dt><dd>{{ formatDuration(job.eta_s) }}</dd>
        <template v-if="job.current_file">
          <dt>Current file</dt><dd class="mono" :title="job.current_file">{{ job.current_file }}</dd>
        </template>
        <template v-if="job.waiting_for_tape">
          <dt>Waiting for</dt><dd>{{ job.waiting_for_tape }}</dd>
        </template>
        <template v-if="job.tape_label || job.tape_id">
          <dt>Tape</dt><dd>{{ job.tape_label || job.tape_id }}</dd>
        </template>
        <template v-if="runtime.state.queued.length && kindLabel === 'Backup'">
          <dt>Queued</dt><dd>{{ runtime.state.queued.map((j) => j.job_name).join(', ') }}</dd>
        </template>
        <template v-if="job.error">
          <dt>Error</dt><dd class="error-text">{{ job.error }}</dd>
        </template>
      </dl>
    </div>
  </div>
</template>
