<script setup lang="ts">
// Recursive directory node for the catalog explorer tree.
// Design adapted from Synth-Gallery's folder tree (chevron rotation,
// 16px indent steps, count chips) with TapeWebUI tokens.

import { computed, inject } from 'vue'
import { CATALOG_TREE_KEY } from './tree-context'

const props = defineProps<{
  dir: { path: string; name: string; count?: number }
  depth: number
}>()

const ctx = inject(CATALOG_TREE_KEY)!

const listing = computed(() => ctx.listingFor(props.dir.path))
const expanded = computed(() => ctx.expandedFor(props.dir.path))
const loading = computed(() => ctx.loadingFor(props.dir.path))
const checked = computed(() => ctx.checkedFor(props.dir.path))
const active = computed(() => ctx.activeFor(props.dir.path))
const childDirs = computed(() => listing.value?.dirs ?? [])
const count = computed(() => props.dir.count ?? listing.value?.dirs.reduce((sum, d) => sum + d.files_count, 0))
const hasChildren = computed(() => !listing.value || listing.value.dirs.length > 0)
</script>

<template>
  <div class="tree-row" :class="{ active }" :style="{ paddingLeft: depth * 16 + 'px' }">
    <button
      v-if="hasChildren"
      class="tree-chevron"
      :class="{ collapsed: !expanded }"
      :title="expanded ? 'Collapse' : 'Expand'"
      @click.stop="ctx.toggleExpand(dir.path)"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polyline points="6 9 12 15 18 9" />
      </svg>
    </button>
    <span v-else class="tree-chevron-placeholder" />

    <div class="tree-item" @click="ctx.activate(dir.path)">
      <svg class="tree-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
      <span class="tree-name" :title="dir.path || dir.name">{{ dir.name }}</span>
      <span v-if="count !== undefined" class="tree-count">{{ count }}</span>
      <span v-else-if="loading" class="tree-count">…</span>
      <input
        type="checkbox"
        class="tree-check"
        :checked="checked"
        :title="'Select whole folder'"
        @click.stop
        @change="ctx.toggleCheck(dir.path)"
      />
    </div>
  </div>

  <template v-if="expanded && listing">
    <CatalogNode
      v-for="child in childDirs"
      :key="child.path"
      :dir="child"
      :depth="depth + 1"
    />
    <div
      v-if="childDirs.length === 0 && !loading"
      class="tree-empty"
      :style="{ paddingLeft: (depth + 1) * 16 + 'px' }"
    >
      no subfolders
    </div>
  </template>
</template>
