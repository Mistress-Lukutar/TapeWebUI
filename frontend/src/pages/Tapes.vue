<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiGet, apiSend, formatBytes, formatIso } from '../api'

interface Tape {
  id: number
  uuid: string
  label: string
  status: string
  bytes_used: number
  created_at: string
  notes: string
}

interface Session {
  id: number
  tape_seq: number
  job_name: string
  started_at: string
  status: string
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

const tapes = ref<Tape[]>([])
const details = ref<TapeDetails | null>(null)
const files = ref<FileRow[]>([])
const query = ref('')
const hits = ref<SearchHit[] | null>(null)
const error = ref('')

async function load(): Promise<void> {
  try {
    tapes.value = await apiGet<Tape[]>('/api/tapes')
  } catch (err) {
    error.value = String(err)
  }
}

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

onMounted(load)
</script>

<template>
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
      <table class="table">
        <thead>
          <tr><th>Label</th><th>Status</th><th>Used</th><th>Created</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-if="!tapes.length"><td colspan="5" class="dim">No tapes in the catalog yet.</td></tr>
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
    </div>
  </div>

  <div class="panel" v-if="details">
    <h3>{{ details.label || details.uuid.slice(0, 8) }} — sessions</h3>
    <table class="table">
      <thead>
        <tr><th>#</th><th>Job</th><th>Started</th><th>Files</th><th>Bytes</th><th>Status</th></tr>
      </thead>
      <tbody>
        <tr v-for="session in details.sessions" :key="session.id">
          <td>{{ session.tape_seq }}</td>
          <td>{{ session.job_name }}</td>
          <td>{{ formatIso(session.started_at) }}</td>
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
