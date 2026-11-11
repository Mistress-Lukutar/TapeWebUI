<script setup lang="ts">
// Tag list editor: chips with remove buttons + an entry input
// (Enter or comma commits; duplicates are dropped).
import { ref } from 'vue'

const props = defineProps<{
  modelValue: string[]
  placeholder?: string
}>()

const emit = defineEmits<{ 'update:modelValue': [string[]] }>()

const draft = ref('')

function commit(): void {
  const items = [...props.modelValue]
  for (const part of draft.value.split(',')) {
    const item = part.trim()
    if (item && !items.includes(item)) items.push(item)
  }
  if (items.length !== props.modelValue.length) {
    emit('update:modelValue', items)
  }
  draft.value = ''
}

function remove(idx: number): void {
  emit('update:modelValue', props.modelValue.filter((_, i) => i !== idx))
}
</script>

<template>
  <div class="tag-row">
    <span v-for="(tag, idx) in modelValue" :key="tag + idx" class="rule-tag">
      {{ tag }}
      <button class="tag-remove" type="button" title="Remove" @click="remove(idx)">
        ×
      </button>
    </span>
    <input
      v-model="draft"
      class="tag-input"
      :placeholder="modelValue.length ? '' : placeholder"
      spellcheck="false"
      @keydown.enter.prevent="commit()"
      @change="commit()"
    />
  </div>
</template>
