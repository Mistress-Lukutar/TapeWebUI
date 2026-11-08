<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { dump as yamlDump, load as yamlLoad } from 'js-yaml'
import { apiGet, apiSend, formatIso } from '../api'
import { useRuntimeStore } from '../composables/useRuntimeStore'
import { useConfirm } from '../composables/useConfirm'
import { formatDuration } from '../utils/duration'
import AppModal from '../components/AppModal.vue'

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

const BLANK_YAML = `name: newjob
sources:
  - path: "D:/Data"
    excludes: ["temp/", "*.tmp"]
options:
  mode: append
  verify: readback
  continue_on_error: true
schedule:
  kind: manual
`

const runtime = useRuntimeStore()
const { confirm } = useConfirm()
const jobs = ref<JobRow[]>([])
const selected = ref<JobRow | null>(null)
const editorOpen = ref(false)
const editorText = ref('')
const error = ref('')
const notice = ref('')
const busy = ref(false)

const ignoreSourceIdx = ref(0)
const ignoreRules = ref<string[]>([])
const newRule = ref('')
const ignoreDirty = ref(false)

const parsedSources = computed<YamlSource[]>(() => {
  try {
    const doc = yamlLoad(editorText.value)
    if (doc && typeof doc === 'object' && Array.isArray((doc as { sources?: unknown }).sources)) {
      return (doc as { sources: YamlSource[] }).sources
    }
  } catch {
    // Unparseable YAML: the panel reports it below.
  }
  return []
})

const ignoreParseError = computed(() => {
  try {
    yamlLoad(editorText.value)
    return ''
  } catch (err) {
    return `YAML parse error: ${err instanceof Error ? err.message : String(err)}`
  }
})

const jobEntries = computed<JobEntry[]>(() =>
  jobs.value.map((job) => ({ ...job, info: parseJobInfo(job.yaml) })),
)

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
      const when = kind === 'weekly' && days.length ? `${days.join(', ')} ${time}` : time
      scheduleText = `${kind} ${when}`.trim()
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

function pickIgnoreSource(): void {
  const source = parsedSources.value[ignoreSourceIdx.value]
  const excludes = source && Array.isArray(source.excludes) ? source.excludes : []
  ignoreRules.value = excludes.filter((rule): rule is string => typeof rule === 'string')
  ignoreDirty.value = false
}

function addRule(): void {
  const rule = newRule.value.trim()
  if (!rule || ignoreRules.value.includes(rule)) {
    newRule.value = ''
    return
  }
  ignoreRules.value.push(rule)
  newRule.value = ''
  ignoreDirty.value = true
}

function removeRule(idx: number): void {
  ignoreRules.value.splice(idx, 1)
  ignoreDirty.value = true
}

function applyIgnoreRules(): void {
  error.value = ''
  try {
    const doc = yamlLoad(editorText.value) as { sources?: YamlSource[] } | null
    if (!doc || !Array.isArray(doc.sources)) {
      error.value = 'Job YAML has no sources to attach rules to'
      return
    }
    const source = doc.sources[ignoreSourceIdx.value]
    if (!source || typeof source !== 'object') {
      error.value = 'Selected source no longer exists in the YAML'
      return
    }
    if (ignoreRules.value.length) {
      source.excludes = [...ignoreRules.value]
    } else {
      delete source.excludes
    }
    editorText.value = yamlDump(doc, { lineWidth: -1, noRefs: true })
    ignoreDirty.value = false
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

function setSourceArchive(value: boolean): void {
  error.value = ''
  try {
    const doc = yamlLoad(editorText.value) as { sources?: YamlSource[] } | null
    if (!doc || !Array.isArray(doc.sources)) {
      error.value = 'Job YAML has no sources to attach rules to'
      return
    }
    const source = doc.sources[ignoreSourceIdx.value]
    if (!source || typeof source !== 'object') {
      error.value = 'Selected source no longer exists in the YAML'
      return
    }
    if (value) {
      source.archive = true
    } else {
      delete source.archive
    }
    editorText.value = yamlDump(doc, { lineWidth: -1, noRefs: true })
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

const archiveChecked = computed<boolean>({
  get() {
    const source = parsedSources.value[ignoreSourceIdx.value]
    return Boolean(source && source.archive)
  },
  set(value: boolean) {
    setSourceArchive(value)
  },
})

async function load(): Promise<void> {
  try {
    jobs.value = await apiGet<JobRow[]>('/api/jobs')
  } catch (err) {
    error.value = String(err)
  }
}

function openEditor(job: JobRow | null): void {
  selected.value = job
  editorText.value = job ? job.yaml : BLANK_YAML
  error.value = ''
  ignoreSourceIdx.value = 0
  pickIgnoreSource()
  editorOpen.value = true
}

async function save(): Promise<void> {
  error.value = ''
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
    editorOpen.value = false
    await load()
    runtime.refreshSchedules()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
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
          <td>
            <template v-if="job.info.sources.length">
              <div v-for="(source, idx) in job.info.sources" :key="idx" class="job-source">
                <span class="mono">{{ source.path }}</span>
                <span v-if="source.archive" class="badge ok">zip</span>
              </div>
            </template>
            <span v-else class="dim">—</span>
          </td>
          <td class="dim nowrap">
            {{ job.info.mode }} · {{ job.info.verify }} · {{ job.info.backupType }}
          </td>
          <td class="nowrap">{{ job.info.schedule }}</td>
          <td class="row-actions nowrap">
            <button @click="runJob(job.name)">Run</button>
            <button @click="openEditor(job)">Edit</button>
            <button class="danger" @click="removeJob(job.name)">Delete</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="dim">No jobs defined yet. Create one with "New job".</p>
    <p v-if="notice" class="dim" style="margin-top: 8px">{{ notice }}</p>
    <p v-if="error" class="error-strip">{{ error }}</p>
  </div>

  <AppModal
    v-if="editorOpen"
    :title="selected ? `Edit job: ${selected.name}` : 'New job'"
    wide
    @close="editorOpen = false"
  >
    <textarea v-model="editorText" class="yaml-editor" spellcheck="false"></textarea>
    <div class="ignore-editor">
      <h4>Ignore rules <span class="dim">(gitignore syntax, per source)</span></h4>
      <p v-if="ignoreParseError" class="error-strip">{{ ignoreParseError }}</p>
      <template v-else>
        <div class="row-actions">
          <select v-model.number="ignoreSourceIdx" @change="pickIgnoreSource()">
            <option v-for="(source, idx) in parsedSources" :key="idx" :value="idx">
              {{ typeof source.path === 'string' ? source.path : `source ${idx + 1}` }}{{ source.archive ? ' [zip]' : '' }}
            </option>
          </select>
          <label class="archive-toggle" title="Back the whole source up as one zip tape file">
            <input
              type="checkbox"
              :checked="archiveChecked"
              :disabled="!parsedSources.length"
              @change="archiveChecked = ($event.target as HTMLInputElement).checked"
            />
            archive → one zip tape file
          </label>
          <span v-if="!parsedSources.length" class="dim">No sources in YAML</span>
        </div>
        <div class="rule-tags">
          <span v-for="(rule, idx) in ignoreRules" :key="rule + idx" class="rule-tag">
            {{ rule }}
            <button class="tag-remove" title="Remove rule" @click="removeRule(idx)">×</button>
          </span>
          <span v-if="!ignoreRules.length" class="dim">No ignore rules for this source.</span>
        </div>
        <div class="row-actions">
          <input
            v-model="newRule"
            placeholder="temp/, *.tmp, cache/**, !keep.tmp"
            @keyup.enter="addRule()"
          />
          <button :disabled="!newRule.trim() || !parsedSources.length" @click="addRule()">
            Add
          </button>
          <button
            class="primary"
            :disabled="!ignoreDirty || !parsedSources.length"
            @click="applyIgnoreRules()"
          >
            Apply to YAML
          </button>
        </div>
        <p class="dim">
          Directories match with a trailing slash (temp/), * matches within a
          path segment, ** across segments, ! re-includes. Files under an
          ignored directory cannot be re-included.
        </p>
      </template>
    </div>
    <template #footer>
      <button @click="editorOpen = false">Cancel</button>
      <button class="primary" :disabled="busy" @click="save()">Validate &amp; save</button>
    </template>
  </AppModal>

  <div class="panel" v-if="runtime.state.job">
    <h3>Active job</h3>
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
      <dt>Bytes</dt><dd>{{ runtime.state.job.bytes_done }} / {{ runtime.state.job.bytes_total }}</dd>
      <dt>Speed</dt><dd>{{ runtime.state.job.speed_bps }} B/s</dd>
      <dt>ETA</dt><dd>{{ formatDuration(runtime.state.job.eta_s) }}</dd>
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
