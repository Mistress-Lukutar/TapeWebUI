<script setup lang="ts">
// Catalog explorer: browse what each tape/session contains directly
// from the database and queue a restore — no tape reads while browsing.
import { computed, onMounted, provide, ref } from 'vue'
import {
  apiGet,
  apiSend,
  formatBytes,
  formatIso,
  type CatalogListing,
  type PlanSelection,
  type RestorePlan,
  type SelectionSummary,
  type SessionFileRow,
} from '../api'
import { useRuntimeStore } from '../composables/useRuntimeStore'
import CatalogNode from '../components/catalog/CatalogNode.vue'
import { CATALOG_TREE_KEY, type CatalogTreeContext } from '../components/catalog/tree-context'

interface TapeRow {
  id: number
  uuid: string
  label: string
  status: string
  bytes_used: number
}

interface SessionRow {
  id: number
  tape_seq: number
  job_name: string
  started_at: string
  status: string
  files_count: number
  bytes_total: number
}

interface SearchHit {
  file: SessionFileRow
  session: { id: number; job_name: string; started_at: string }
  tape_uuid: string
  tape_label: string
}

type Scope = { kind: 'session'; id: number; label: string; tapeId: number } | { kind: 'tape'; id: number; label: string } | null

const runtime = useRuntimeStore()

// -- scope (tape / session) -------------------------------------------------

const tapes = ref<TapeRow[]>([])
const tapeSessions = ref<Map<number, SessionRow[]>>(new Map())
const expandedTapes = ref<Set<number>>(new Set())
const scope = ref<Scope>(null)

async function loadTapes(): Promise<void> {
  tapes.value = await apiGet<TapeRow[]>('/api/tapes')
}

async function toggleTape(tapeId: number): Promise<void> {
  if (expandedTapes.value.has(tapeId)) {
    expandedTapes.value.delete(tapeId)
  } else {
    expandedTapes.value.add(tapeId)
    if (!tapeSessions.value.has(tapeId)) {
      const details = await apiGet<{ sessions: SessionRow[] }>(`/api/tapes/${tapeId}`)
      tapeSessions.value.set(tapeId, details.sessions)
    }
  }
  expandedTapes.value = new Set(expandedTapes.value)
}

async function pickSession(tapeId: number, session: SessionRow): Promise<void> {
  resetScope()
  scope.value = {
    kind: 'session',
    id: session.id,
    label: `Session ${session.tape_seq + 1} — ${session.job_name || 'manual'} (${formatIso(session.started_at)})`,
    tapeId,
  }
  await fetchListing('')
}

async function pickTape(tape: TapeRow): Promise<void> {
  resetScope()
  scope.value = { kind: 'tape', id: tape.id, label: tape.label || tape.uuid.slice(0, 8) }
  await fetchListing('')
}

// -- tree browsing ----------------------------------------------------------

const listings = ref<Map<string, CatalogListing>>(new Map())
const loadingDirs = ref<Set<string>>(new Set())
const expanded = ref<Set<string>>(new Set())
const currentDir = ref('')

function listingFor(path: string): CatalogListing | undefined {
  return listings.value.get(path)
}

function loadingFor(path: string): boolean {
  return loadingDirs.value.has(path)
}

function expandedFor(path: string): boolean {
  return expanded.value.has(path)
}

async function toggleExpand(path: string): Promise<void> {
  if (expanded.value.has(path)) {
    expanded.value.delete(path)
  } else {
    expanded.value.add(path)
    if (!listings.value.has(path)) await fetchListing(path)
  }
  expanded.value = new Set(expanded.value)
}

async function activate(path: string): Promise<void> {
  currentDir.value = path
  if (!listings.value.has(path)) await fetchListing(path)
}

async function fetchListing(path: string): Promise<void> {
  if (!scope.value || loadingDirs.value.has(path)) return
  loadingDirs.value.add(path)
  loadingDirs.value = new Set(loadingDirs.value)
  try {
    const query =
      scope.value.kind === 'session'
        ? `session_id=${scope.value.id}`
        : `tape_id=${scope.value.id}`
    const listing = await apiGet<CatalogListing>(
      `/api/catalog/list?${query}&dir=${encodeURIComponent(path)}`,
    )
    listings.value.set(path, listing)
    listings.value = new Map(listings.value)
  } finally {
    loadingDirs.value.delete(path)
    loadingDirs.value = new Set(loadingDirs.value)
  }
}

function resetScope(): void {
  listings.value = new Map()
  loadingDirs.value = new Set()
  expanded.value = new Set()
  currentDir.value = ''
  selected.value = new Map()
  dirChecked.value = new Set()
  dirSelections.value = new Map()
  plan.value = null
  error.value = ''
}

const currentListing = computed(() => listingFor(currentDir.value))
const currentLabel = computed(() => currentDir.value || '(root)')

const breadcrumb = computed(() => {
  if (!currentDir.value) return []
  // Split on either separator, keeping the full prefix of each segment.
  const parts: { label: string; path: string }[] = []
  let acc = ''
  for (const raw of currentDir.value.split(/([/\\])/)) {
    acc += raw
    if (raw === '/' || raw === '\\') {
      parts.push({ label: raw === '\\' ? acc.slice(0, -1) : acc, path: acc })
    } else if (raw) {
      parts.push({ label: raw, path: acc })
    }
  }
  return parts
})

// -- selection --------------------------------------------------------------

const selected = ref<Map<number, { path: string; size: number }>>(new Map())
const dirChecked = ref<Set<string>>(new Set())
const dirSelections = ref<Map<string, number[]>>(new Map())
const searching = ref(false)
const searchQuery = ref('')
const searchHits = ref<SearchHit[]>([])

function checkedFor(path: string): boolean {
  return dirChecked.value.has(path)
}

async function toggleCheck(path: string): Promise<void> {
  if (!scope.value) return
  if (dirChecked.value.has(path)) {
    for (const id of dirSelections.value.get(path) ?? []) selected.value.delete(id)
    dirSelections.value.delete(path)
    dirChecked.value.delete(path)
    dirChecked.value = new Set(dirChecked.value)
    selected.value = new Map(selected.value)
    return
  }
  const query =
    scope.value.kind === 'session'
      ? `session_id=${scope.value.id}`
      : `tape_id=${scope.value.id}`
  const summary = await apiGet<SelectionSummary>(
    `/api/catalog/subtree?${query}&dir=${encodeURIComponent(path)}&latest_only=true`,
  )
  dirSelections.value.set(path, summary.file_ids)
  for (const id of summary.file_ids) {
    selected.value.set(id, { path: '', size: 0 })
  }
  // Pull sizes/paths from any loaded listing containing them (best effort;
  // the plan endpoint holds the authoritative values).
  for (const listing of listings.value.values()) {
    for (const row of listing.files) {
      if (selected.value.has(row.id)) {
        selected.value.set(row.id, { path: row.path, size: row.size })
      }
    }
    for (const merged of listing.merged_files) {
      for (const version of merged.versions) {
        if (selected.value.has(version.file_id)) {
          selected.value.set(version.file_id, {
            path: merged.path,
            size: version.size,
          })
        }
      }
    }
  }
  dirChecked.value.add(path)
  dirChecked.value = new Set(dirChecked.value)
  selected.value = new Map(selected.value)
}

function activeFor(path: string): boolean {
  return currentDir.value === path
}

function toggleFile(file: SessionFileRow): void {
  if (selected.value.has(file.id)) selected.value.delete(file.id)
  else selected.value.set(file.id, { path: file.path, size: file.size })
  selected.value = new Map(selected.value)
}

function toggleVersion(row: { path: string; versions: { file_id: number; size: number }[] }, fileId: number): void {
  if (selected.value.has(fileId)) selected.value.delete(fileId)
  else {
    const version = row.versions.find((v) => v.file_id === fileId)
    selected.value.set(fileId, { path: row.path, size: version?.size ?? 0 })
  }
  selected.value = new Map(selected.value)
}

const selectionSize = computed(() => selected.value.size)
const selectionBytes = computed(() =>
  Array.from(selected.value.values()).reduce((sum, item) => sum + item.size, 0),
)

function isVersionSelected(fileId: number): boolean {
  return selected.value.has(fileId)
}

// -- search -----------------------------------------------------------------

async function search(): Promise<void> {
  if (!searchQuery.value) return
  searching.value = true
  error.value = ''
  try {
    searchHits.value = await apiGet<SearchHit[]>(
      `/api/catalog/search?q=${encodeURIComponent(searchQuery.value)}&limit=100`,
    )
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    searching.value = false
  }
}

function hitSelected(hit: SearchHit): boolean {
  return selected.value.has(hit.file.id)
}

function toggleHit(hit: SearchHit): void {
  if (hitSelected(hit)) selected.value.delete(hit.file.id)
  else selected.value.set(hit.file.id, { path: hit.file.path, size: hit.file.size })
  selected.value = new Map(selected.value)
}

// -- plan and run -----------------------------------------------------------

const plan = ref<RestorePlan | null>(null)
const destinationMode = ref<'original' | 'alternate'>('alternate')
const destinationDir = ref('')
const collision = ref<'skip' | 'overwrite' | 'rename'>('rename')
const error = ref('')
const busy = ref(false)

async function buildPlan(): Promise<void> {
  if (selected.value.size === 0) return
  error.value = ''
  busy.value = true
  try {
    plan.value = await apiSend<RestorePlan>('/api/restore/plan', 'POST', {
      file_ids: Array.from(selected.value.keys()),
      selections: [] as PlanSelection[],
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function run(): Promise<void> {
  if (!plan.value) return
  error.value = ''
  busy.value = true
  try {
    await apiSend('/api/restore/run', 'POST', {
      file_ids: Array.from(selected.value.keys()),
      selections: [],
      destination_mode: destinationMode.value,
      destination_dir: destinationDir.value || null,
      collision: collision.value,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function cancel(): Promise<void> {
  await apiSend('/api/restore/cancel', 'POST')
}

const restoreActive = computed(() => {
  const state = runtime.state.restore?.state
  return !!state && !['done', 'failed', 'cancelled'].includes(state)
})

const rootDir = computed(() => ({
  path: '',
  name: scope.value?.label ?? 'Catalog',
}))

const treeCtx: CatalogTreeContext = {
  get scope() {
    if (!scope.value) return { kind: 'session' as const, id: -1 }
    return { kind: scope.value.kind, id: scope.value.id }
  },
  listingFor,
  loadingFor,
  expandedFor,
  toggleExpand,
  checkedFor,
  toggleCheck,
  activeFor,
  activate,
}

provide(CATALOG_TREE_KEY, treeCtx)

onMounted(() => {
  void loadTapes()
})
</script>

<template>
  <div class="panel catalog-toolbar">
    <div class="row-actions">
      <input
        v-model="searchQuery"
        placeholder="search paths across all tapes…"
        style="flex: 1; min-width: 200px"
        @keyup.enter="search()"
      />
      <button class="primary" :disabled="searching || !searchQuery" @click="search()">
        Search
      </button>
      <span class="dim">
        selected: {{ selectionSize }} files,
        {{ formatBytes(selectionBytes) }}
      </span>
    </div>
    <table v-if="searchHits.length" class="table" style="margin-top: 8px">
      <thead>
        <tr><th></th><th>Path</th><th>Tape</th><th>Session</th><th>Size</th></tr>
      </thead>
      <tbody>
        <tr v-for="hit in searchHits" :key="hit.file.id">
          <td>
            <input type="checkbox" :checked="hitSelected(hit)" @change="toggleHit(hit)" />
          </td>
          <td class="mono">{{ hit.file.path }}</td>
          <td>{{ hit.tape_label || hit.tape_uuid.slice(0, 8) }}</td>
          <td>{{ formatIso(hit.session.started_at) }}</td>
          <td>{{ formatBytes(hit.file.size) }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="catalog-layout">
    <aside class="panel cat-sidebar">
      <h3>Tapes</h3>
      <p v-if="!tapes.length" class="dim">No tapes registered yet.</p>
      <div v-for="tape in tapes" :key="tape.id" class="tape-group">
        <div class="tree-row" :class="{ active: scope?.kind === 'tape' && scope.id === tape.id }">
          <button
            class="tree-chevron"
            :class="{ collapsed: !expandedTapes.has(tape.id) }"
            @click.stop="toggleTape(tape.id)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
          <div class="tree-item" @click="pickTape(tape)">
            <svg class="tree-icon tape-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="2" y="6" width="20" height="12" rx="2" />
              <circle cx="8" cy="12" r="2" />
              <circle cx="16" cy="12" r="2" />
            </svg>
            <span class="tree-name" :title="tape.uuid">{{ tape.label || tape.uuid.slice(0, 8) }}</span>
            <span class="tree-count">{{ tape.status }}</span>
          </div>
        </div>
        <template v-if="expandedTapes.has(tape.id)">
          <div
            v-for="session in tapeSessions.get(tape.id) ?? []"
            :key="session.id"
            class="tree-row session-row"
            :class="{ active: scope?.kind === 'session' && scope.id === session.id }"
            :style="{ paddingLeft: '20px' }"
          >
            <span class="tree-chevron-placeholder" />
            <div class="tree-item" @click="pickSession(tape.id, session)">
              <svg
                class="tree-icon session-icon"
                :class="`session-${session.status}`"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
              </svg>
              <span class="tree-name">
                {{ formatIso(session.started_at) }} — {{ session.job_name || 'manual' }}
              </span>
              <span class="tree-count">{{ session.status }}</span>
            </div>
          </div>
        </template>
      </div>
    </aside>

    <section class="panel cat-pane">
      <h3>Folders</h3>
      <p v-if="!scope" class="dim">Pick a tape (merged view) or a session on the left.</p>
      <template v-else>
        <div class="crumbs row-actions">
          <a class="crumb" @click.prevent="activate('')">root</a>
          <template v-for="(part, index) in breadcrumb" :key="part.path">
            <span class="dim">›</span>
            <a class="crumb" :class="{ last: index === breadcrumb.length - 1 }" @click.prevent="activate(part.path)">
              {{ part.label }}
            </a>
          </template>
        </div>
        <div class="cat-tree">
          <CatalogNode :dir="rootDir" :depth="0" />
        </div>
      </template>
    </section>

    <section class="panel cat-pane">
      <h3>Files <span class="dim" v-if="scope">in {{ currentLabel }}</span></h3>
      <p v-if="!scope" class="dim">—</p>
      <p v-else-if="!currentListing" class="dim">Loading…</p>
      <template v-else>
        <table v-if="currentListing.mode === 'session' && currentListing.files.length" class="table">
          <thead>
            <tr><th></th><th>Name</th><th>Size</th><th>Modified</th><th>Status</th></tr>
          </thead>
          <tbody>
            <tr v-for="file in currentListing.files" :key="file.id">
              <td>
                <input type="checkbox" :checked="selected.has(file.id)" @change="toggleFile(file)" />
              </td>
              <td class="mono">{{ file.name }}</td>
              <td>{{ formatBytes(file.size) }}</td>
              <td>{{ formatIso(file.mtime) }}</td>
              <td>
                <span class="badge" :class="file.status === 'ok' ? 'ok' : file.status === 'damaged' ? 'fail' : 'warn'">
                  {{ file.status }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <p
          v-else-if="currentListing.mode === 'session'"
          class="dim"
        >
          No files directly in this folder.
        </p>

        <table v-if="currentListing.mode === 'tape' && currentListing.merged_files.length" class="table">
          <thead>
            <tr><th>Name</th><th>Versions</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in currentListing.merged_files" :key="row.path">
              <td class="mono">{{ row.name }}</td>
              <td>
                <div v-for="version in row.versions" :key="version.file_id" class="version-row">
                  <input
                    type="checkbox"
                    :checked="isVersionSelected(version.file_id)"
                    @change="toggleVersion(row, version.file_id)"
                  />
                  <span class="dim">{{ formatIso(version.session_started_at) }}</span>
                  <span class="badge" :class="version.status === 'ok' ? 'ok' : version.status === 'damaged' ? 'fail' : 'warn'">
                    {{ version.status }}
                  </span>
                  <span class="dim">{{ formatBytes(version.size) }}</span>
                  <span class="dim mono">t#{{ version.tape_file_index }}</span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else-if="currentListing.mode === 'tape'" class="dim">No files directly in this folder.</p>
      </template>
    </section>
  </div>

  <div class="panel" v-if="selectionSize">
    <h3>Restore selection</h3>
    <div class="row-actions">
      <label class="dim">Destination:</label>
      <select v-model="destinationMode">
        <option value="original">Original paths</option>
        <option value="alternate">Alternate directory</option>
      </select>
      <input
        v-if="destinationMode === 'alternate'"
        v-model="destinationDir"
        placeholder="D:/Restore"
        style="flex: 1; min-width: 160px"
      />
      <label class="dim">Collision:</label>
      <select v-model="collision">
        <option value="rename">Rename</option>
        <option value="overwrite">Overwrite</option>
        <option value="skip">Skip</option>
      </select>
      <button class="primary" :disabled="busy || restoreActive" @click="buildPlan()">
        Build plan
      </button>
    </div>
    <p v-if="error" class="error-strip">{{ error }}</p>
  </div>

  <div class="panel" v-if="plan">
    <h3>Plan</h3>
    <p>
      {{ plan.files_count }} files, {{ formatBytes(plan.bytes_total) }} across
      {{ plan.tapes.length }} tape(s):
      <b v-for="(group, index) in plan.tapes" :key="group.tape_id">
        {{ index ? ', ' : '' }}{{ group.tape_label || group.tape_id.slice(0, 8) }}
      </b>
    </p>
    <p class="dim">Insert each tape when the restore asks for it.</p>
    <div class="row-actions">
      <button class="primary" :disabled="busy || restoreActive" @click="run()">Start restore</button>
      <button
        class="danger"
        :disabled="!restoreActive"
        @click="cancel()"
      >
        Cancel
      </button>
    </div>
  </div>

  <div class="panel" v-if="runtime.state.restore">
    <h3>Progress</h3>
    <div class="kv-grid">
      <dt>State</dt>
      <dd>
        {{ runtime.state.restore.state }}
        <span v-if="runtime.state.restore.state === 'waiting_media'" class="dim">
          — insert {{ runtime.state.restore.waiting_for_tape || runtime.state.restore.tape_id }}
        </span>
      </dd>
      <dt>Tape</dt>
      <dd>{{ runtime.state.restore.tape_label || runtime.state.restore.tape_id || '—' }}</dd>
      <dt>Files</dt>
      <dd>{{ runtime.state.restore.files_done }} / {{ runtime.state.restore.files_total }}</dd>
      <dt>Bytes</dt>
      <dd>{{ formatBytes(runtime.state.restore.bytes_done) }} / {{ formatBytes(runtime.state.restore.bytes_total) }}</dd>
      <dt>Hash mismatches</dt>
      <dd>{{ runtime.state.restore.hash_mismatches }}</dd>
      <dt>Current</dt>
      <dd class="mono">{{ runtime.state.restore.current_file || '—' }}</dd>
      <dt v-if="runtime.state.restore.error">Error</dt>
      <dd v-if="runtime.state.restore.error" class="error-text">{{ runtime.state.restore.error }}</dd>
    </div>
  </div>
</template>
