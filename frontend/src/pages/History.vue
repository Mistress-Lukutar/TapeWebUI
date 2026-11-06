<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiGet, formatIso } from '../api'

interface RunRow {
  id: number
  job_id: number | null
  session_id: number | null
  started_at: string
  finished_at: string | null
  status: string
  error: string | null
  stats: Record<string, unknown> | null
}

const runs = ref<RunRow[]>([])
const error = ref('')

async function load(): Promise<void> {
  try {
    runs.value = await apiGet<RunRow[]>('/api/history?limit=200')
  } catch (err) {
    error.value = String(err)
  }
}

onMounted(load)
</script>

<template>
  <h2>History</h2>

  <div class="panel">
    <table class="table">
      <thead>
        <tr>
          <th>Run</th><th>Started</th><th>Finished</th><th>Status</th><th>Error</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="!runs.length"><td colspan="5" class="dim">No runs recorded yet.</td></tr>
        <tr v-for="run in runs" :key="run.id">
          <td>#{{ run.id }}<span v-if="run.job_id" class="dim"> (job {{ run.job_id }})</span></td>
          <td>{{ formatIso(run.started_at) }}</td>
          <td>{{ formatIso(run.finished_at) }}</td>
          <td><span class="badge" :class="run.status === 'done' ? 'ok' : 'fail'">{{ run.status }}</span></td>
          <td class="error-text">{{ run.error || '' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="error" class="error-strip">{{ error }}</p>
    <button style="margin-top: 8px" @click="load()">Refresh</button>
  </div>
</template>
