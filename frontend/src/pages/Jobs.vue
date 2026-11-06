<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiGet, apiSend, formatIso } from '../api'
import { useRuntimeStore } from '../composables/useRuntimeStore'

interface JobRow {
  name: string
  enabled: boolean
  schedule_kind: string
  yaml: string
}

const runtime = useRuntimeStore()
const jobs = ref<JobRow[]>([])
const selected = ref<JobRow | null>(null)
const editorText = ref('')
const error = ref('')
const notice = ref('')
const busy = ref(false)

async function load(): Promise<void> {
  try {
    jobs.value = await apiGet<JobRow[]>('/api/jobs')
  } catch (err) {
    error.value = String(err)
  }
}

function select(job: JobRow): void {
  selected.value = job
  editorText.value = job.yaml
  error.value = ''
  notice.value = ''
}

function blank(): void {
  selected.value = null
  editorText.value = `name: newjob\nsources:\n  - path: "D:/Data"\noptions:\n  mode: append\n  verify: readback\n  continue_on_error: true\nschedule:\n  kind: manual\n`
}

async function save(): Promise<void> {
  error.value = ''
  notice.value = ''
  busy.value = true
  try {
    if (selected.value) {
      await apiSend(`/api/jobs/${selected.value.name}`, 'PUT', { content: editorText.value })
      notice.value = `Saved ${selected.value.name}`
    } else {
      const created = await apiSend<{ name: string }>('/api/jobs', 'POST', {
        content: editorText.value,
      })
      notice.value = `Created ${created.name}`
    }
    await load()
    runtime.refreshSchedules()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function removeJob(name: string): Promise<void> {
  if (!window.confirm(`Delete job ${name}?`)) return
  await apiSend(`/api/jobs/${name}`, 'DELETE')
  selected.value = null
  await load()
  runtime.refreshSchedules()
}

async function runJob(name: string): Promise<void> {
  error.value = ''
  try {
    await apiSend(`/api/jobs/${name}/run`, 'POST')
    notice.value = `${name} queued`
    await runtime.refreshRuntime()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function cancel(): Promise<void> {
  await apiSend('/api/jobs/cancel', 'POST')
  notice.value = 'Cancel requested'
}

onMounted(load)
</script>

<template>
  <div class="columns">
    <div class="panel half">
      <h3>Defined jobs</h3>
      <table v-if="jobs.length" class="table">
        <thead>
          <tr><th>Name</th><th>Schedule</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="job in jobs" :key="job.name" :class="{ selected: selected?.name === job.name }">
            <td><a href="#" @click.prevent="select(job)">{{ job.name }}</a></td>
            <td>{{ job.schedule_kind }}</td>
            <td class="row-actions">
              <button @click="runJob(job.name)">Run</button>
              <button class="danger" @click="removeJob(job.name)">Delete</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="dim">No jobs defined yet.</p>
      <div class="row-actions" style="margin-top: 8px">
        <button @click="blank()">New job</button>
        <button class="danger" :disabled="!runtime.state.job" @click="cancel()">
          Cancel active job
        </button>
      </div>
    </div>

    <div class="panel half">
      <h3>{{ selected ? `Edit: ${selected.name}` : 'New job (YAML)' }}</h3>
      <textarea v-model="editorText" class="yaml-editor" spellcheck="false"></textarea>
      <div class="row-actions" style="margin-top: 8px">
        <button class="primary" :disabled="busy" @click="save()">Validate &amp; save</button>
      </div>
      <p v-if="notice" class="dim">{{ notice }}</p>
      <p v-if="error" class="error-strip">{{ error }}</p>
    </div>
  </div>

  <div class="panel" v-if="runtime.state.job">
    <h3>Active job</h3>
    <div class="kv-grid">
      <dt>Job</dt><dd>{{ runtime.state.job.job_name || '—' }} ({{ runtime.state.job.state }})</dd>
      <dt>Files</dt><dd>{{ runtime.state.job.files_done }} / {{ runtime.state.job.files_total }}</dd>
      <dt>Bytes</dt><dd>{{ runtime.state.job.bytes_done }} / {{ runtime.state.job.bytes_total }}</dd>
      <dt>Speed</dt><dd>{{ runtime.state.job.speed_bps }} B/s</dd>
      <dt>ETA</dt><dd>{{ runtime.state.job.eta_s ?? '—' }} s</dd>
      <dt>Current file</dt><dd>{{ runtime.state.job.current_file || '—' }}</dd>
      <dt v-if="runtime.state.job.error">Error</dt>
      <dd v-if="runtime.state.job.error" class="error-text">{{ runtime.state.job.error }}</dd>
    </div>
    <p v-if="runtime.state.queued.length" class="dim">
      Queued: {{ runtime.state.queued.map((j) => j.job_name).join(', ') }}
    </p>
  </div>

  <p class="dim" style="margin-top: 8px">Updated {{ formatIso(new Date().toISOString()) }}</p>
</template>
