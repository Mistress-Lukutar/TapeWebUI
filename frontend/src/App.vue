<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useDriveStore } from './composables/useDriveStore'

const drive = useDriveStore()

const status = computed(() => drive.state.status)
const lastUpdated = computed(() => {
  if (!status.value) return '—'
  const iso = status.value.updated_at.endsWith('Z')
    ? status.value.updated_at
    : status.value.updated_at + 'Z'
  return new Date(iso).toLocaleString('en-GB', { hour12: false })
})

onMounted(() => {
  drive.fetchStatus()
  drive.connectSse()
})
</script>

<template>
  <h1>TapeWebUI</h1>

  <div class="panel">
    <h2>Drive status</h2>

    <div v-if="status" class="kv-grid">
      <dt>State</dt>
      <dd>
        <span class="badge" :class="status.reachable ? 'ok' : 'fail'">
          {{ status.reachable ? 'Reachable' : 'Unreachable' }}
        </span>
        <span v-if="!drive.state.connected" class="dim"> · SSE reconnecting…</span>
      </dd>

      <dt>Drive</dt>
      <dd>
        <template v-if="status.manufacturer">
          {{ status.manufacturer }} {{ status.product_id }} (fw {{ status.revision }})
        </template>
        <template v-else>—</template>
      </dd>

      <dt>Position</dt>
      <dd>
        <template v-if="status.block_id !== null">
          partition {{ status.partition }}, block {{ status.block_id }}
          <span v-if="status.at_beginning_of_partition" class="dim"> (BOP)</span>
        </template>
        <template v-else>—</template>
      </dd>

      <dt>Compression</dt>
      <dd>{{ status.compression === null ? '—' : status.compression ? 'On' : 'Off' }}</dd>

      <dt>Block size</dt>
      <dd>{{ status.parms_block_size ?? '—' }}</dd>

      <dt>Updated</dt>
      <dd>{{ lastUpdated }}</dd>
    </div>

    <div v-else class="dim">Loading drive status…</div>

    <div v-if="status?.error" class="error-strip">{{ status.error }}</div>
    <div v-if="drive.state.error" class="error-strip">API error: {{ drive.state.error }}</div>

    <div style="margin-top: 12px">
      <button class="primary" :disabled="drive.state.loading" @click="drive.refresh()">
        Refresh now
      </button>
    </div>
  </div>
</template>
