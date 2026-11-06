<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { apiGet } from '../api'

interface EditableSettings {
  drive_poll_interval: number
  itdt_timeout_s: number
  itdt_open_retry_s: number
  itdt_open_retries: number
}

interface AppInfo {
  version: string
  host: string
  port: number
  device_name: string
  itdt_exe: string
  force_generic_dd: boolean
  fake_transport: boolean
  data_dir: string
  log_dir: string
  catalog_db_size_bytes: number | null
}

interface SettingsResponse {
  editable: EditableSettings
  info: AppInfo
}

const editable = reactive<EditableSettings>({
  drive_poll_interval: 15,
  itdt_timeout_s: 600,
  itdt_open_retry_s: 2,
  itdt_open_retries: 3,
})
const info = ref<AppInfo | null>(null)
const saving = ref(false)
const message = ref('')
const error = ref('')

async function load(): Promise<void> {
  try {
    const body = await apiGet<SettingsResponse>('/api/settings')
    Object.assign(editable, body.editable)
    info.value = body.info
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function save(): Promise<void> {
  saving.value = true
  message.value = ''
  error.value = ''
  try {
    const response = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...editable }),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      throw new Error(payload?.detail || `HTTP ${response.status}`)
    }
    const body = (await response.json()) as SettingsResponse
    Object.assign(editable, body.editable)
    info.value = body.info
    message.value = 'Settings applied and saved.'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="columns">
    <div class="panel half">
      <h2>Tuning</h2>
      <p class="dim">
        Applied immediately and persisted to <span class="mono">data/settings.json</span>.
        Paths and device names can only be changed via TAPE_* environment variables.
      </p>
      <form @submit.prevent="save()">
        <label class="field">
          <span>Drive status poll interval (s)</span>
          <input v-model.number="editable.drive_poll_interval" type="number" min="1" step="1" />
        </label>
        <label class="field">
          <span>ITDT command timeout (s)</span>
          <input v-model.number="editable.itdt_timeout_s" type="number" min="1" step="1" />
        </label>
        <label class="field">
          <span>Open-failure retry delay (s)</span>
          <input v-model.number="editable.itdt_open_retry_s" type="number" min="0" step="0.5" />
        </label>
        <label class="field">
          <span>Open-failure retries</span>
          <input v-model.number="editable.itdt_open_retries" type="number" min="0" step="1" />
        </label>
        <div style="margin-top: 12px">
          <button class="primary" type="submit" :disabled="saving">
            {{ saving ? 'Saving…' : 'Save settings' }}
          </button>
        </div>
      </form>
      <p v-if="message" class="ok-text">{{ message }}</p>
      <p v-if="error" class="error-strip">{{ error }}</p>
    </div>

    <div class="panel half">
      <h2>Application</h2>
      <div v-if="info" class="kv-grid">
        <dt>Version</dt><dd>{{ info.version }}</dd>
        <dt>Listen</dt><dd class="mono">{{ info.host }}:{{ info.port }}</dd>
        <dt>Device</dt><dd class="mono">{{ info.device_name }}</dd>
        <dt>ITDT</dt><dd class="mono">{{ info.itdt_exe }}</dd>
        <dt>Force generic DD</dt><dd>{{ info.force_generic_dd ? 'Yes' : 'No' }}</dd>
        <dt>Fake transport</dt>
        <dd>
          <span class="badge" :class="info.fake_transport ? 'warn' : 'ok'">
            {{ info.fake_transport ? 'ON (no hardware)' : 'Off' }}
          </span>
        </dd>
        <dt>Data dir</dt><dd class="mono">{{ info.data_dir }}</dd>
        <dt>Log dir</dt><dd class="mono">{{ info.log_dir }}</dd>
        <dt>Catalog DB size</dt>
        <dd>
          <template v-if="info.catalog_db_size_bytes !== null">
            {{ (info.catalog_db_size_bytes / 1024).toFixed(1) }} KiB
          </template>
          <template v-else>—</template>
        </dd>
      </div>
      <p v-else class="dim">Loading…</p>
    </div>
  </div>
</template>
