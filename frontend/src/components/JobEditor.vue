<script setup lang="ts">
// Two-column job editor: structured form (left) <-> YAML document (right),
// kept in sync in both directions. The YAML pane is an editable textarea
// overlaid on a syntax-highlighted <pre> (no extra dependencies).
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { dump as yamlDump, load as yamlLoad } from 'js-yaml'
import { apiSend, formatBytes } from '../api'
import AppModal from './AppModal.vue'
import TagInput from './TagInput.vue'

interface JobRow {
  name: string
  yaml: string
}

interface SourcePreviewInfo {
  path: string
  exists: boolean
  files: number
  bytes: number
}

interface SourceModel {
  path: string
  patterns: string[]
  excludes: string[]
  archive: boolean
  preview: SourcePreviewInfo | null
}

interface JobModel {
  name: string
  sources: SourceModel[]
  mode: 'append' | 'overwrite'
  verify: 'none' | 'readback'
  continueOnError: boolean
  backupType: 'full' | 'incremental'
  fullEveryDays: number
  scheduleKind: 'manual' | 'daily' | 'weekly'
  scheduleTime: string
  scheduleDays: string[]
  catchUp: 'once' | 'skip'
}

const props = defineProps<{ job: JobRow | null }>()
const emit = defineEmits<{ close: []; saved: [string] }>()

const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/

const DEFAULT_SOURCE: Omit<SourceModel, 'preview'> = {
  path: 'D:/Data',
  patterns: [],
  excludes: ['temp/', '*.tmp'],
  archive: false,
}

function makeSource(over: Partial<SourceModel> = {}): SourceModel {
  return { ...DEFAULT_SOURCE, preview: null, ...over }
}

function initialModel(): JobModel {
  const base: JobModel = {
    name: 'newjob',
    sources: [makeSource()],
    mode: 'append',
    verify: 'readback',
    continueOnError: true,
    backupType: 'full',
    fullEveryDays: 30,
    scheduleKind: 'manual',
    scheduleTime: '03:00',
    scheduleDays: [],
    catchUp: 'once',
  }
  if (props.job) {
    const doc = parseDoc(props.job.yaml)
    if (doc) applyDocToModel(base, doc)
    base.name = props.job.name
  }
  return base
}

// -- form <-> YAML sync ------------------------------------------------------

const model = reactive<JobModel>(initialModel())
const yamlText = ref('')
const yamlError = ref('')
const busy = ref(false)
const saveError = ref('')
/** Guard: model mutations currently replaying a YAML edit must not rewrite the YAML. */
let replayingYaml = false

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function strList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

type RawDoc = Record<string, unknown>

function parseDoc(text: string): RawDoc | null {
  try {
    const doc = yamlLoad(text)
    return doc && typeof doc === 'object' ? (doc as RawDoc) : null
  } catch {
    return null
  }
}

function applyDocToModel(target: JobModel, doc: RawDoc): void {
  target.name = str(doc.name, target.name)
  const sources = Array.isArray(doc.sources) ? (doc.sources as RawDoc[]) : []
  target.sources = sources.map((source) =>
    makeSource({
      path: str(source.path),
      patterns: strList(source.patterns),
      excludes: strList(source.excludes),
      archive: Boolean(source.archive),
    }),
  )
  const options = (doc.options ?? {}) as RawDoc
  target.mode = options.mode === 'overwrite' ? 'overwrite' : 'append'
  target.verify = options.verify === 'none' ? 'none' : 'readback'
  target.continueOnError = options.continue_on_error !== false
  target.backupType = options.backup_type === 'incremental' ? 'incremental' : 'full'
  const days = typeof options.full_every_days === 'number' ? options.full_every_days : 30
  target.fullEveryDays = days
  const schedule = (doc.schedule ?? {}) as RawDoc
  target.scheduleKind =
    schedule.kind === 'daily' || schedule.kind === 'weekly' ? schedule.kind : 'manual'
  target.scheduleTime = TIME_RE.test(str(schedule.time)) ? str(schedule.time) : '03:00'
  target.scheduleDays = strList(schedule.days).filter((day) => DAYS.includes(day))
  target.catchUp = schedule.catch_up === 'skip' ? 'skip' : 'once'
}

function dumpModel(): string {
  const sources = model.sources.map((source) => {
    const entry: RawDoc = { path: source.path }
    if (source.patterns.length) entry.patterns = [...source.patterns]
    if (source.excludes.length) entry.excludes = [...source.excludes]
    if (source.archive) entry.archive = true
    return entry
  })
  const doc: RawDoc = {
    name: model.name,
    sources,
    options: {
      mode: model.mode,
      verify: model.verify,
      continue_on_error: model.continueOnError,
      backup_type: model.backupType,
      full_every_days: model.fullEveryDays,
    },
    schedule: {
      kind: model.scheduleKind,
      time: model.scheduleTime,
      ...(model.scheduleKind === 'weekly' ? { days: [...model.scheduleDays] } : {}),
      ...(model.scheduleKind === 'manual' ? {} : { catch_up: model.catchUp }),
    },
  }
  return yamlDump(doc, { lineWidth: -1, noRefs: true })
}

watch(
  model,
  () => {
    if (replayingYaml) return
    yamlText.value = dumpModel()
  },
  { deep: true, flush: 'sync' },
)

function onYamlInput(event: Event): void {
  const text = (event.target as HTMLTextAreaElement).value
  yamlText.value = text
  const doc = parseDoc(text)
    if (!doc) {
      try {
        yamlLoad(text)
      } catch (err) {
      yamlError.value = `YAML parse error: ${err instanceof Error ? err.message : String(err)}`
    }
    return
  }
  yamlError.value = ''
  replayingYaml = true
  try {
    applyDocToModel(model, doc)
  } finally {
    replayingYaml = false
  }
}

// -- YAML syntax highlighting (regex tokenizer, palette-consistent) ----------

function escHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;')
}

function highlightValue(value: string): string {
  const trimmed = value.trim()
  if (!trimmed) return escHtml(value)
  let cls = 'y-str'
  if (/^(true|false)$/.test(trimmed)) cls = 'y-bool'
  else if (/^-?\d+(\.\d+)?$/.test(trimmed)) cls = 'y-num'
  else if (trimmed.startsWith('#')) cls = 'y-comment'
  return `<span class="${cls}">${escHtml(value)}</span>`
}

const highlightedYaml = computed(() =>
  yamlText.value.split('\n').length > 4000 ? escHtml(yamlText.value) : highlightYaml(yamlText.value),
)

function highlightYaml(text: string): string {
  return text
    .split('\n')
    .map((line) => {
      if (/^\s*#/.test(line)) return `<span class="y-comment">${escHtml(line)}</span>`
      const key = line.match(/^(\s*(?:-\s+)?)([A-Za-z_][\w-]*)(:)(.*)$/)
      if (key) {
        return (
          escHtml(key[1]) +
          `<span class="y-key">${escHtml(key[2])}</span>` +
          `<span class="y-punc">:</span>` +
          highlightValue(key[4])
        )
      }
      const item = line.match(/^(\s*-\s+)(.*)$/)
      if (item) return escHtml(item[1]) + highlightValue(item[2])
      return escHtml(line)
    })
    .join('\n')
}

const highlightPre = ref<HTMLPreElement | null>(null)

function syncScroll(target: HTMLTextAreaElement): void {
  const pre = highlightPre.value
  if (pre) {
    pre.scrollTop = target.scrollTop
    pre.scrollLeft = target.scrollLeft
  }
}

// -- per-source file/bytes preview ------------------------------------------

let previewTimer: number | undefined

function schedulePreview(): void {
  window.clearTimeout(previewTimer)
  previewTimer = window.setTimeout(runPreview, 600)
}

async function runPreview(): Promise<void> {
  const snapshot = model.sources.map((source) => ({
    path: source.path,
    patterns: [...source.patterns],
    excludes: [...source.excludes],
    archive: source.archive,
  }))
  if (!snapshot.length) return
  try {
    const result = await apiSend<SourcePreviewInfo[]>(
      '/api/jobs/preview-sources',
      'POST',
      { sources: snapshot },
    )
    // Drop a stale response if the sources changed while the request ran.
    const current = JSON.stringify(
      model.sources.map((source) => ({
        path: source.path,
        patterns: [...source.patterns],
        excludes: [...source.excludes],
        archive: source.archive,
      })),
    )
    if (current !== JSON.stringify(snapshot)) return
    result.forEach((info, idx) => {
      if (model.sources[idx]) model.sources[idx].preview = info
    })
  } catch {
    // Preview is best-effort; the backend reports real errors on save.
  }
}

watch(
  () => model.sources.map((s) => `${s.path}|${s.patterns.join(',')}|${s.excludes.join(',')}`).join('\n'),
  schedulePreview,
)

onMounted(() => {
  yamlText.value = dumpModel()
  runPreview()
})

onUnmounted(() => window.clearTimeout(previewTimer))

// -- source list editing -----------------------------------------------------

function addSource(): void {
  model.sources.push(makeSource({ path: '' }))
}

function removeSource(idx: number): void {
  if (model.sources.length > 1) model.sources.splice(idx, 1)
}

const totalPreview = computed(() => {
  let files = 0
  let bytes = 0
  let known = 0
  for (const source of model.sources) {
    if (source.preview?.exists) {
      files += source.preview.files
      bytes += source.preview.bytes
      known += 1
    }
  }
  return { files, bytes, known, complete: known === model.sources.length }
})

// -- validation & save -------------------------------------------------------

const schemaError = computed(() => {
  if (!NAME_RE.test(model.name)) {
    return 'Job name: use lowercase letters, digits, "-" and "_" (max 64 chars).'
  }
  if (!model.sources.length) return 'At least one source is required.'
  for (const source of model.sources) {
    if (!source.path.trim()) return 'Every source needs a path.'
  }
  if (model.scheduleKind === 'weekly' && !model.scheduleDays.length) {
    return 'Weekly schedule needs at least one selected day.'
  }
  if (!TIME_RE.test(model.scheduleTime)) return 'Schedule time must be HH:MM.'
  if (!Number.isInteger(model.fullEveryDays) || model.fullEveryDays < 1 || model.fullEveryDays > 3650) {
    return 'Full-every days must be between 1 and 3650.'
  }
  return ''
})

const canSave = computed(() => !busy.value && !yamlError.value && !schemaError.value)

const title = computed(() => (props.job ? `Edit job: ${props.job.name}` : 'New job'))

async function save(): Promise<void> {
  saveError.value = ''
  busy.value = true
  try {
    if (props.job) {
      await apiSend(`/api/jobs/${props.job.name}`, 'PUT', { content: yamlText.value })
    } else {
      await apiSend('/api/jobs', 'POST', { content: yamlText.value })
    }
    emit('saved', model.name)
  } catch (err) {
    saveError.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppModal :title="title" wide @close="emit('close')">
    <div class="job-editor">
      <!-- Left: structured form -->
      <div class="job-editor-form">
        <div class="form-section">
          <h4>General</h4>
          <div class="form-grid">
            <label class="form-field">
              <span>Job name</span>
              <input
                v-model="model.name"
                :disabled="!!props.job"
                class="mono"
                spellcheck="false"
              />
            </label>
            <p v-if="props.job" class="dim field-hint">
              The name is fixed for an existing job.
            </p>
          </div>
        </div>

        <div class="form-section">
          <h4>Schedule</h4>
          <div class="form-grid schedule-row">
            <label class="form-field">
              <span>Run</span>
              <select v-model="model.scheduleKind">
                <option value="manual">manual (from UI only)</option>
                <option value="daily">daily</option>
                <option value="weekly">weekly</option>
              </select>
            </label>
            <label v-if="model.scheduleKind !== 'manual'" class="form-field">
              <span>At time (UTC)</span>
              <input v-model="model.scheduleTime" type="time" />
            </label>
            <label v-if="model.scheduleKind !== 'manual'" class="form-field">
              <span>Catch-up on missed runs</span>
              <select v-model="model.catchUp">
                <option value="once">run once</option>
                <option value="skip">skip</option>
              </select>
            </label>
          </div>
          <div v-if="model.scheduleKind === 'weekly'" class="day-pills">
            <label v-for="day in DAYS" :key="day" class="day-pill">
              <input v-model="model.scheduleDays" type="checkbox" :value="day" />
              {{ day }}
            </label>
          </div>
          <p v-else-if="model.scheduleKind === 'manual'" class="dim field-hint">
            The job runs only when started from the UI.
          </p>
        </div>

        <div class="form-section">
          <h4>Options</h4>
          <div class="form-grid">
            <label class="form-field">
              <span>Tape mode</span>
              <select v-model="model.mode">
                <option value="append">append (add after last file)</option>
                <option value="overwrite">overwrite (from beginning)</option>
              </select>
            </label>
            <label class="form-field">
              <span>Verify</span>
              <select v-model="model.verify">
                <option value="readback">readback (read after write)</option>
                <option value="none">none</option>
              </select>
            </label>
            <label class="form-field">
              <span>Backup type</span>
              <select v-model="model.backupType">
                <option value="full">full</option>
                <option value="incremental">incremental</option>
              </select>
            </label>
            <label v-if="model.backupType === 'incremental'" class="form-field">
              <span>Force full after (days)</span>
              <input v-model.number="model.fullEveryDays" type="number" min="1" max="3650" />
            </label>
            <label class="form-check">
              <input v-model="model.continueOnError" type="checkbox" />
              Continue on file errors
            </label>
          </div>
        </div>

        <div class="form-section">
          <div class="section-head">
            <h4>Sources</h4>
            <button type="button" @click="addSource()">+ Add source</button>
          </div>
          <div v-for="(source, idx) in model.sources" :key="idx" class="source-card">
            <div class="source-head">
              <span class="dim source-label">Source {{ idx + 1 }}</span>
              <span v-if="source.preview" :class="source.preview.exists ? 'preview-chip' : 'preview-chip warn'">
                <template v-if="source.preview.exists">
                  {{ source.preview.files }} files · {{ formatBytes(source.preview.bytes) }}
                </template>
                <template v-else>path not found</template>
              </span>
              <button
                type="button"
                class="danger source-remove"
                :disabled="model.sources.length <= 1"
                title="Remove this source"
                @click="removeSource(idx)"
              >
                ×
              </button>
            </div>
            <label class="form-field">
              <span>Directory</span>
              <input v-model="source.path" class="mono" spellcheck="false" placeholder="C:/path/to/data" />
            </label>
            <div class="source-rules">
              <div class="rule-block">
                <span class="form-label">Include patterns <span class="dim">(empty = all files)</span></span>
                <TagInput v-model="source.patterns" placeholder="e.g. models/**, *.jpg" />
              </div>
              <div class="rule-block">
                <span class="form-label">Ignore rules <span class="dim">(gitignore syntax)</span></span>
                <TagInput v-model="source.excludes" placeholder="temp/, *.tmp, cache/**" />
              </div>
            </div>
            <label class="form-check archive-check" title="Back the whole source up as one zip tape file">
              <input v-model="source.archive" type="checkbox" />
              archive → one zip tape file
            </label>
          </div>
          <p class="dim field-hint">
            Directories match with a trailing slash (temp/), * matches within a
            path segment, ** across segments, ! re-includes. Files under an
            ignored directory cannot be re-included.
          </p>
          <p v-if="totalPreview.complete && totalPreview.files" class="dim field-hint">
            Total: ~{{ totalPreview.files }} files · {{ formatBytes(totalPreview.bytes) }}
          </p>
        </div>
      </div>

      <!-- Right: YAML document -->
      <div class="job-editor-yaml">
        <div class="yaml-pane-head">
          <span class="dim">YAML</span>
          <span
            class="badge"
            :class="yamlError ? 'fail' : 'ok'"
          >
            {{ yamlError ? 'invalid' : 'valid' }}
          </span>
        </div>
        <p v-if="yamlError" class="error-strip yaml-error">{{ yamlError }}</p>
        <div class="yaml-stack">
          <pre ref="highlightPre" class="yaml-highlight" aria-hidden="true"><code v-html="highlightedYaml"></code></pre>
          <textarea
            :value="yamlText"
            class="yaml-input"
            spellcheck="false"
            @input="onYamlInput($event)"
            @scroll="syncScroll($event.target as HTMLTextAreaElement)"
          ></textarea>
        </div>
      </div>
    </div>

    <p v-if="schemaError" class="error-strip">{{ schemaError }}</p>
    <p v-if="saveError" class="error-strip">{{ saveError }}</p>

    <template #footer>
      <button @click="emit('close')">Cancel</button>
      <button class="primary" :disabled="!canSave" @click="save()">
        {{ busy ? 'Saving…' : 'Validate & save' }}
      </button>
    </template>
  </AppModal>
</template>
