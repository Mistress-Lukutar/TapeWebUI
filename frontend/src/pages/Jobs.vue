<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { dump as yamlDump, load as yamlLoad } from 'js-yaml'
import { apiGet, apiSend, formatIso } from '../api'
import { useRuntimeStore } from '../composables/useRuntimeStore'

interface JobRow {
  name: string
  enabled: boolean
  schedule_kind: string
  yaml: string
}

interface YamlSource {
  path?: unknown
  excludes?: unknown
}

const runtime = useRuntimeStore()
const jobs = ref<JobRow[]>([])
const selected = ref<JobRow | null>(null)
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

watch(parsedSources, (sources) => {
  if (ignoreSourceIdx.value >= sources.length) ignoreSourceIdx.value = 0
})

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
  ignoreSourceIdx.value = 0
  pickIgnoreSource()
}

function blank(): void {
  selected.value = null
  editorText.value = `name: newjob\nsources:\n  - path: "D:/Data"\n    excludes: ["temp/", "*.tmp"]\noptions:\n  mode: append\n  verify: readback\n  continue_on_error: true\nschedule:\n  kind: manual\n`
  error.value = ''
  notice.value = ''
  ignoreSourceIdx.value = 0
  pickIgnoreSource()
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
      <div class="ignore-editor">
        <h4>Ignore rules <span class="dim">(gitignore syntax, per source)</span></h4>
        <p v-if="ignoreParseError" class="error-strip">{{ ignoreParseError }}</p>
        <template v-else>
          <div class="row-actions">
            <select v-model.number="ignoreSourceIdx" @change="pickIgnoreSource()">
              <option v-for="(source, idx) in parsedSources" :key="idx" :value="idx">
                {{ typeof source.path === 'string' ? source.path : `source ${idx + 1}` }}
              </option>
            </select>
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
