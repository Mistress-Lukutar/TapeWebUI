<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiGet, apiSend, formatBytes, formatIso } from '../api'

interface Tape {
  id: number
  uuid: string
  label: string
  status: string
  bytes_used: number
  created_at: string
  notes: string
  last_verified_at: string | null
}

interface Session {
  id: number
  tape_seq: number
  job_name: string
  started_at: string
  status: string
  kind: string
  files_count: number
  bytes_total: number
}

interface TapeDetails extends Tape {
  sessions: Session[]
}

interface FileRow {
  id: number
  session_id: number
  tape_file_index: number
  path: string
  size: number
  status: string
}

interface SearchHit {
  file: FileRow
  session: Session
  tape_uuid: string
  tape_label: string
}

interface SessionDiff {
  kind: string
  file_base: number
  session_id: string
  job_name: string
  catalog_session_id: number | null
  catalog_status: string
  detail: string
}

interface DeepReport {
  tape_id: number | null
  tape_uuid: string | null
  tape_label: string
  on_tape_count: number
  catalog_count: number
  diffs: SessionDiff[]
}

interface VerifyResponse {
  status: string
  tape_id: number | null
  tape_uuid: string | null
  tape_label: string
  detail: string
  report: DeepReport | null
}

interface StaleNotice {
  active: boolean
  reason: string
  at: string
}

const tapes = ref<Tape[]>([])
const details = ref<TapeDetails | null>(null)
const files = ref<FileRow[]>([])
const query = ref('')
const hits = ref<SearchHit[] | null>(null)
const error = ref('')
const staleNotice = ref<StaleNotice | null>(null)
const verifying = ref(false)
const verifyResult = ref<VerifyResponse | null>(null)
const applying = ref(false)

async function load(): Promise<void> {
  try {
    tapes.value = await apiGet<Tape[]>('/api/tapes')
  } catch (err) {
    error.value = String(err)
  }
}

async function loadStale(): Promise<void> {
  try {
    staleNotice.value = await apiGet<StaleNotice | null>('/api/sync/stale-notice')
  } catch {
    staleNotice.value = null
  }
}

async function dismissStale(): Promise<void> {
  try {
    await apiSend('/api/sync/stale-notice/dismiss', 'POST')
    staleNotice.value = null
  } catch (err) {
    error.value = String(err)
  }
}

async function verify(deep: boolean): Promise<void> {
  error.value = ''
  verifying.value = true
  verifyResult.value = null
  try {
    verifyResult.value = await apiSend<VerifyResponse>(
      `/api/drive/verify-tape?deep=${deep}`,
      'POST',
    )
    await load()
  } catch (err) {
    error.value = String(err)
  } finally {
    verifying.value = false
  }
}

async function markSessionsErased(sessionIds: number[]): Promise<void> {
  if (!verifyResult.value?.report || !sessionIds.length) return
  const tapeId = verifyResult.value.report.tape_id
  if (tapeId === null) return
  if (!window.confirm(`Mark ${sessionIds.length} session(s) erased in the catalog?`))
    return
  error.value = ''
  applying.value = true
  try {
    await apiSend(`/api/tapes/${tapeId}/sessions/erase-mark`, 'POST', {
      session_ids: sessionIds,
    })
    // Re-run the deep verify so the report reflects the catalog change.
    await verify(true)
  } catch (err) {
    error.value = String(err)
  } finally {
    applying.value = false
  }
}

const catalogOnlyIds = computed<number[]>(() => {
  const report = verifyResult.value?.report
  if (!report) return []
  return report.diffs
    .filter((d) => d.kind === 'catalog_only' && d.catalog_session_id !== null)
    .map((d) => d.catalog_session_id as number)
})

async function open(tape: Tape): Promise<void> {
  error.value = ''
  hits.value = null
  try {
    details.value = await apiGet<TapeDetails>(`/api/tapes/${tape.id}`)
    files.value = await apiGet<FileRow[]>(`/api/tapes/${tape.id}/files`)
  } catch (err) {
    error.value = String(err)
  }
}

async function search(): Promise<void> {
  if (!query.value.trim()) {
    hits.value = null
    return
  }
  error.value = ''
  try {
    hits.value = await apiGet<SearchHit[]>(
      `/api/catalog/search?q=${encodeURIComponent(query.value)}`,
    )
  } catch (err) {
    error.value = String(err)
  }
}

async function markErased(tape: Tape): Promise<void> {
  if (!window.confirm(`Mark all sessions of ${tape.label || tape.uuid} erased?`)) return
  await fetch(`/api/tapes/${tape.id}/erase-mark`, { method: 'POST' })
  await load()
}

const renamingId = ref<number | null>(null)
const renameLabel = ref('')

function startRename(tape: Tape): void {
  error.value = ''
  renamingId.value = tape.id
  renameLabel.value = tape.label
}

async function saveRename(tape: Tape): Promise<void> {
  error.value = ''
  try {
    const updated = await apiSend<Tape>(`/api/tapes/${tape.id}`, 'PUT', {
      label: renameLabel.value,
    })
    tapes.value = tapes.value.map((item) => (item.id === updated.id ? updated : item))
    if (details.value?.id === updated.id) {
      details.value = { ...details.value, label: updated.label }
    }
    renamingId.value = null
  } catch (err) {
    error.value = String(err)
  }
}

onMounted(() => {
  load()
  loadStale()
})
</script>

<template>
  <div v-if="staleNotice" class="error-strip" style="margin-bottom: 12px">
    Catalog may be stale: a tape was erased on {{ formatIso(staleNotice.at) }}
    but could not be matched to a catalog entry. Use "Verify tape" below to
    check the loaded cartridge.
    <button class="danger" style="margin-left: 8px" @click="dismissStale()">Dismiss</button>
  </div>

  <div class="columns">
    <div class="panel half">
      <h3>Catalog search</h3>
      <div class="row-actions">
        <input v-model="query" placeholder="path substring…" @keyup.enter="search()" />
        <button class="primary" @click="search()">Search</button>
        <button @click="query = ''; hits = null">Clear</button>
      </div>
      <table v-if="hits" class="table" style="margin-top: 8px">
        <thead>
          <tr><th>Path</th><th>Tape</th><th>Session</th><th>Size</th><th>Status</th></tr>
        </thead>
        <tbody>
          <tr v-if="!hits.length"><td colspan="5" class="dim">No matches.</td></tr>
          <tr v-for="hit in hits" :key="hit.file.id">
            <td class="mono">{{ hit.file.path }}</td>
            <td>{{ hit.tape_label || hit.tape_uuid.slice(0, 8) }}</td>
            <td>#{{ hit.session.tape_seq }} {{ hit.session.job_name }}</td>
            <td>{{ formatBytes(hit.file.size) }}</td>
            <td>{{ hit.file.status }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel half">
      <h3>Tapes</h3>
      <div class="row-actions" style="margin-bottom: 8px">
        <button class="primary" :disabled="verifying" @click="verify(false)">
          {{ verifying ? 'Checking…' : 'Verify tape in drive' }}
        </button>
        <button :disabled="verifying" title="Walk the whole tape and compare all sessions with the catalog" @click="verify(true)">
          Deep verify
        </button>
      </div>
      <table class="table">
        <thead>
          <tr><th>Label</th><th>Status</th><th>Used</th><th>Created</th><th>Verified</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-if="!tapes.length"><td colspan="6" class="dim">No tapes in the catalog yet.</td></tr>
          <tr v-for="tape in tapes" :key="tape.id">
            <td>
              <input
                v-if="renamingId === tape.id"
                v-model="renameLabel"
                class="rename-input"
                @keyup.enter="saveRename(tape)"
                @keyup.esc="renamingId = null"
              />
              <a v-else href="#" @click.prevent="open(tape)">{{ tape.label || tape.uuid.slice(0, 8) }}</a>
            </td>
            <td>{{ tape.status }}</td>
            <td>{{ formatBytes(tape.bytes_used) }}</td>
            <td>{{ formatIso(tape.created_at) }}</td>
            <td>{{ formatIso(tape.last_verified_at) }}</td>
            <td class="row-actions">
              <template v-if="renamingId === tape.id">
                <button class="primary" @click="saveRename(tape)">Save</button>
                <button @click="renamingId = null">Cancel</button>
              </template>
              <template v-else>
                <button @click="startRename(tape)">Rename</button>
                <button class="danger" @click="markErased(tape)">Erase-mark</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="error" class="error-strip">{{ error }}</p>

      <div v-if="verifyResult" style="margin-top: 12px">
        <h4>Verify result: {{ verifyResult.status }}</h4>
        <p class="dim">
          <template v-if="verifyResult.tape_label || verifyResult.tape_uuid">
            Tape: {{ verifyResult.tape_label || verifyResult.tape_uuid?.slice(0, 8) }}.
          </template>
          {{ verifyResult.detail }}
        </p>
        <template v-if="verifyResult.report">
          <p class="dim">
            On tape: {{ verifyResult.report.on_tape_count }} session(s);
            in catalog: {{ verifyResult.report.catalog_count }} non-erased session(s).
          </p>
          <table class="table">
            <thead>
              <tr><th>Kind</th><th>Position</th><th>Job</th><th>Detail</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="diff in verifyResult.report.diffs" :key="`${diff.kind}-${diff.file_base}`">
                <td>{{ diff.kind }}</td>
                <td>t{{ diff.file_base }}</td>
                <td>{{ diff.job_name || diff.session_id.slice(0, 8) }}</td>
                <td>{{ diff.detail }}</td>
                <td>
                  <button
                    v-if="diff.kind === 'catalog_only' && diff.catalog_session_id !== null"
                    class="danger"
                    :disabled="applying"
                    @click="markSessionsErased([diff.catalog_session_id])"
                  >
                    Mark erased
                  </button>
                </td>
              </tr>
              <tr v-if="!verifyResult.report.diffs.length">
                <td colspan="5" class="dim">Tape and catalog are in sync.</td>
              </tr>
            </tbody>
          </table>
          <button
            v-if="catalogOnlyIds.length"
            class="danger"
            :disabled="applying"
            style="margin-top: 8px"
            @click="markSessionsErased(catalogOnlyIds)"
          >
            Mark all {{ catalogOnlyIds.length }} missing session(s) erased
          </button>
        </template>
      </div>
    </div>
  </div>

  <div class="panel" v-if="details">
    <h3>{{ details.label || details.uuid.slice(0, 8) }} — sessions</h3>
    <table class="table">
      <thead>
        <tr><th>#</th><th>Job</th><th>Started</th><th>Kind</th><th>Files</th><th>Bytes</th><th>Status</th></tr>
      </thead>
      <tbody>
        <tr v-for="session in details.sessions" :key="session.id">
          <td>{{ session.tape_seq }}</td>
          <td>{{ session.job_name }}</td>
          <td>{{ formatIso(session.started_at) }}</td>
          <td>{{ session.kind }}</td>
          <td>{{ session.files_count }}</td>
          <td>{{ formatBytes(session.bytes_total) }}</td>
          <td>{{ session.status }}</td>
        </tr>
      </tbody>
    </table>
    <h3 style="margin-top: 12px">Files</h3>
    <table class="table">
      <thead>
        <tr><th>t#</th><th>Path</th><th>Size</th><th>Status</th></tr>
      </thead>
      <tbody>
        <tr v-for="file in files" :key="file.id">
          <td>{{ file.tape_file_index }}</td>
          <td class="mono">{{ file.path }}</td>
          <td>{{ formatBytes(file.size) }}</td>
          <td>{{ file.status }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
