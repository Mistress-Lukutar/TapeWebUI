<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiGet, apiSend, formatBytes, formatIso } from '../api'
import { useRuntimeStore } from '../composables/useRuntimeStore'

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
  session: { id: number; tape_seq: number; job_name: string; started_at: string }
  tape_uuid: string
  tape_label: string
}

interface Plan {
  tape_id: string
  tape_label: string
  files_count: number
  bytes_total: number
  items: { file_id: number; path: string; size: number; tape_file_index: number }[]
}

const runtime = useRuntimeStore()
const query = ref('')
const hits = ref<SearchHit[]>([])
const selected = ref<Map<number, FileRow>>(new Map())
const plan = ref<Plan | null>(null)
const destinationMode = ref<'original' | 'alternate'>('alternate')
const destinationDir = ref('')
const collision = ref<'skip' | 'overwrite' | 'rename'>('rename')
const error = ref('')
const busy = ref(false)

const selectedList = computed(() => Array.from(selected.value.values()))

async function search(): Promise<void> {
  error.value = ''
  try {
    hits.value = await apiGet<SearchHit[]>(
      `/api/catalog/search?q=${encodeURIComponent(query.value)}&limit=100`,
    )
  } catch (err) {
    error.value = String(err)
  }
}

function toggle(file: FileRow): void {
  if (selected.value.has(file.id)) selected.value.delete(file.id)
  else selected.value.set(file.id, file)
  selected.value = new Map(selected.value)
}

async function buildPlan(): Promise<void> {
  error.value = ''
  busy.value = true
  try {
    plan.value = await apiSend<Plan>('/api/restore/plan', 'POST', {
      file_ids: selectedList.value.map((f) => f.id),
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
      file_ids: plan.value.items.map((item) => item.file_id),
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

onMounted(() => {
  query.value = 'jpg'
  void search()
})
</script>

<template>
  <h2>Restore</h2>

  <div class="panel">
    <h3>1. Find files</h3>
    <div class="row-actions">
      <input v-model="query" placeholder="path substring…" @keyup.enter="search()" />
      <button class="primary" @click="search()">Search</button>
      <span class="dim">selected: {{ selectedList.length }}</span>
    </div>
    <table v-if="hits.length" class="table" style="margin-top: 8px">
      <thead>
        <tr><th></th><th>Path</th><th>Tape</th><th>Size</th><th>t#</th></tr>
      </thead>
      <tbody>
        <tr v-for="hit in hits" :key="hit.file.id">
          <td>
            <input
              type="checkbox"
              :checked="selected.has(hit.file.id)"
              @change="toggle(hit.file)"
            />
          </td>
          <td class="mono">{{ hit.file.path }}</td>
          <td>{{ hit.tape_label || hit.tape_uuid.slice(0, 8) }}</td>
          <td>{{ formatBytes(hit.file.size) }}</td>
          <td>{{ hit.file.tape_file_index }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="dim">Search the catalog to pick files for restore.</p>
  </div>

  <div class="panel" v-if="selectedList.length">
    <h3>2. Plan</h3>
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
        style="flex: 1"
      />
      <label class="dim">Collision:</label>
      <select v-model="collision">
        <option value="rename">Rename</option>
        <option value="overwrite">Overwrite</option>
        <option value="skip">Skip</option>
      </select>
      <button class="primary" :disabled="busy" @click="buildPlan()">Build plan</button>
    </div>
    <div v-if="plan" style="margin-top: 8px">
      <p>
        {{ plan.files_count }} files, {{ formatBytes(plan.bytes_total) }} —
        tape <b>{{ plan.tape_label || plan.tape_id.slice(0, 8) }}</b>
        (single pass in storage order).
      </p>
      <p class="dim">Insert this tape, then run the restore.</p>
      <div class="row-actions">
        <button class="primary" :disabled="busy" @click="run()">Start restore</button>
        <button class="danger" :disabled="!runtime.state.restore || ['done','failed','cancelled'].includes(runtime.state.restore.state)" @click="cancel()">
          Cancel
        </button>
      </div>
    </div>
    <p v-if="error" class="error-strip">{{ error }}</p>
  </div>

  <div class="panel" v-if="runtime.state.restore">
    <h3>3. Progress</h3>
    <div class="kv-grid">
      <dt>State</dt><dd>{{ runtime.state.restore.state }}</dd>
      <dt>Files</dt><dd>{{ runtime.state.restore.files_done }} / {{ runtime.state.restore.files_total }}</dd>
      <dt>Bytes</dt><dd>{{ formatBytes(runtime.state.restore.bytes_done) }} / {{ formatBytes(runtime.state.restore.bytes_total) }}</dd>
      <dt>Hash mismatches</dt><dd>{{ runtime.state.restore.hash_mismatches }}</dd>
      <dt>Current</dt><dd class="mono">{{ runtime.state.restore.current_file || '—' }}</dd>
      <dt v-if="runtime.state.restore.error">Error</dt>
      <dd v-if="runtime.state.restore.error" class="error-text">{{ runtime.state.restore.error }}</dd>
    </div>
  </div>

  <p class="dim">Now: {{ formatIso(new Date().toISOString()) }}</p>
</template>
