<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { load as yamlLoad } from 'js-yaml'
import { apiGet, apiSend, formatBytes, formatIso } from '../api'
import { useRuntimeStore } from '../composables/useRuntimeStore'
import { useConfirm } from '../composables/useConfirm'
import { formatDuration } from '../utils/duration'
import { formatScheduleLocal } from '../utils/schedule'
import JobEditor from '../components/JobEditor.vue'

interface JobRow {
  name: string
  enabled: boolean
  schedule_kind: string
  yaml: string
}

interface YamlSource {
  path?: unknown
  excludes?: unknown
  archive?: unknown
}

interface JobSourceInfo {
  path: string
  archive: boolean
}

interface JobInfo {
  sources: JobSourceInfo[]
  mode: string
  verify: string
  backupType: string
  schedule: string
}

interface JobEntry extends JobRow {
  info: JobInfo
}

const runtime = useRuntimeStore()
const { confirm } = useConfirm()

const percent = computed(() => {
  const job = runtime.state.job
  if (!job || !job.bytes_total || job.indeterminate) return null
  return Math.min(100, Math.round((job.bytes_done / job.bytes_total) * 100))
})

const jobs = ref<JobRow[]>([])
const selected = ref<JobRow | null>(null)
const editorOpen = ref(false)
const error = ref('')
const notice = ref('')

const SOURCE_PREVIEW_LIMIT = 5
const expandedSources = ref<Set<string>>(new Set())

function visibleSources(job: JobEntry): JobSourceInfo[] {
  return expandedSources.value.has(job.name)
    ? job.info.sources
    : job.info.sources.slice(0, SOURCE_PREVIEW_LIMIT)
}

function toggleSources(job: JobEntry) {
  const next = new Set(expandedSources.value)
  if (next.has(job.name)) {
    next.delete(job.name)
  } else {
    next.add(job.name)
  }
  expandedSources.value = next
}

const jobEntries = computed<JobEntry[]>(() =>
  jobs.value.map((job) => ({ ...job, info: parseJobInfo(job.yaml) })),
)

function onEditorSaved(name: string): void {
  editorOpen.value = false
  notice.value = selected.value ? `Saved ${selected.value.name}` : `Created ${name}`
  load()
  runtime.refreshSchedules()
}

function parseJobInfo(yaml: string): JobInfo {
  const fallback: JobInfo = {
    sources: [],
    mode: '—',
    verify: '—',
    backupType: '—',
    schedule: '—',
  }
  try {
    const doc = yamlLoad(yaml) as {
      sources?: YamlSource[]
      options?: Record<string, unknown>
      schedule?: Record<string, unknown>
    } | null
    if (!doc || typeof doc !== 'object') return fallback
    const sources = Array.isArray(doc.sources) ? doc.sources : []
    const options = doc.options ?? {}
    const schedule = doc.schedule ?? {}
    const kind = typeof schedule.kind === 'string' ? schedule.kind : 'manual'
    const time = typeof schedule.time === 'string' ? schedule.time : ''
    const days = Array.isArray(schedule.days)
      ? schedule.days.filter((day): day is string => typeof day === 'string')
      : []
    let scheduleText = kind
    if (kind !== 'manual') {
      // YAML stores UTC; display the fire time converted to the local timezone.
      scheduleText = formatScheduleLocal(kind, time, days)
    }
    return {
      sources: sources.map((source, idx) => ({
        path: typeof source.path === 'string' ? source.path : `source ${idx + 1}`,
        archive: Boolean(source.archive),
      })),
      mode: typeof options.mode === 'string' ? options.mode : '—',
      verify: typeof options.verify === 'string' ? options.verify : '—',
      backupType:
        typeof options.backup_type === 'string' ? options.backup_type.replace('_', ' ') : 'full',
      schedule: scheduleText,
    }
  } catch {
    return fallback
  }
}

async function load(): Promise<void> {
  try {
    jobs.value = await apiGet<JobRow[]>('/api/jobs')
  } catch (err) {
    error.value = String(err)
  }
}

function openEditor(job: JobRow | null): void {
  selected.value = job
  error.value = ''
  editorOpen.value = true
}

async function removeJob(name: string): Promise<void> {
  const ok = await confirm({
    title: 'Delete job',
    message: `Delete job ${name}? Its definition file is removed permanently.`,
    confirmLabel: 'Delete',
    danger: true,
  })
  if (!ok) return
  error.value = ''
  try {
    await apiSend(`/api/jobs/${name}`, 'DELETE')
    if (selected.value?.name === name) selected.value = null
    notice.value = `Deleted ${name}`
    await load()
    runtime.refreshSchedules()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
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
  error.value = ''
  try {
    await apiSend('/api/jobs/cancel', 'POST')
    notice.value = 'Cancel requested'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="jobs-toolbar">
      <h3>Defined jobs</h3>
      <div class="row-actions">
        <button class="primary" @click="openEditor(null)">New job</button>
        <button class="danger" :disabled="!runtime.state.job" @click="cancel()">
          Cancel active job
        </button>
      </div>
    </div>
    <table v-if="jobEntries.length" class="table">
      <thead>
        <tr>
          <th>Name</th><th>Sources</th><th>Options</th><th>Schedule</th><th></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="job in jobEntries"
          :key="job.name"
          :class="{ selected: selected?.name === job.name }"
        >
          <td class="nowrap">{{ job.name }}</td>
          <td class="job-sources">
            <template v-if="job.info.sources.length">
              <div
                v-for="(source, idx) in visibleSources(job)"
                :key="idx"
                class="job-source"
                :title="source.path"
              >
                <span class="mono">{{ source.path }}</span>
                <span v-if="source.archive" class="badge ok">zip</span>
              </div>
              <button
                v-if="job.info.sources.length > SOURCE_PREVIEW_LIMIT"
                class="link-btn source-toggle"
                @click="toggleSources(job)"
              >
                {{
                  expandedSources.has(job.name)
                    ? '▲ collapse'
                    : `▼ show ${job.info.sources.length - SOURCE_PREVIEW_LIMIT} more`
                }}
              </button>
            </template>
            <span v-else class="dim">—</span>
          </td>
          <td class="dim nowrap">
            {{ job.info.mode }} · {{ job.info.verify }} · {{ job.info.backupType }}
          </td>
          <td class="nowrap">{{ job.info.schedule }}</td>
          <td class="nowrap">
            <div class="row-actions">
              <button @click="runJob(job.name)">Run</button>
              <button @click="openEditor(job)">Edit</button>
              <button class="danger" @click="removeJob(job.name)">Delete</button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="dim">No jobs defined yet. Create one with "New job".</p>
    <p v-if="notice" class="dim" style="margin-top: 8px">{{ notice }}</p>
    <p v-if="error" class="error-strip">{{ error }}</p>
  </div>

  <JobEditor
    v-if="editorOpen"
    :job="selected"
    @close="editorOpen = false"
    @saved="onEditorSaved"
  />

  <div class="panel" v-if="runtime.state.job">
    <h3>Active job</h3>
    <div class="job-widget-progress">
      <div
        class="job-widget-fill"
        :class="{ error: runtime.state.job.error, indeterminate: percent === null }"
        :style="{ width: (percent === null ? 100 : percent) + '%' }"
      />
    </div>
    <div class="kv-grid">
      <dt>Job</dt><dd>{{ runtime.state.job.job_name || '—' }} ({{ runtime.state.job.state }})</dd>
      <dt>Files</dt>
      <dd>
        {{ runtime.state.job.files_done }} / {{ runtime.state.job.files_total }}
        <span
          v-if="(runtime.state.job.archives ?? 0) > 0"
          class="dim"
        >({{ runtime.state.job.archived_files }} files in
          {{ runtime.state.job.archives }} zip{{
            (runtime.state.job.archives ?? 0) > 1 ? 's' : ''
          }})</span>
      </dd>
      <dt>Bytes</dt><dd>{{ formatBytes(runtime.state.job.bytes_done) }} / {{ formatBytes(runtime.state.job.bytes_total) }}</dd>
      <dt>Speed</dt><dd>{{ runtime.state.job.indeterminate ? '—' : formatBytes(runtime.state.job.speed_bps) + '/s' }}</dd>
      <dt>ETA</dt><dd>{{ formatDuration(runtime.state.job.eta_s) }}</dd>
      <dt>Current file</dt><dd>{{ runtime.state.job.current_file || '—' }}</dd>
      <template v-if="runtime.state.job.tape_label || runtime.state.job.tape_id">
        <dt>Tape</dt><dd>{{ runtime.state.job.tape_label || runtime.state.job.tape_id }}</dd>
      </template>
      <dt v-if="runtime.state.job.error">Error</dt>
      <dd v-if="runtime.state.job.error" class="error-text">{{ runtime.state.job.error }}</dd>
    </div>
    <p v-if="runtime.state.queued.length" class="dim">
      Queued: {{ runtime.state.queued.map((j) => j.job_name).join(', ') }}
    </p>
  </div>

  <p class="dim" style="margin-top: 8px">Updated {{ formatIso(new Date().toISOString()) }}</p>
</template>
