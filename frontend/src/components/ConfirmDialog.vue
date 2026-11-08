<script setup lang="ts">
import AppModal from './AppModal.vue'
import { useConfirm } from '../composables/useConfirm'

const { state, accept, dismiss } = useConfirm()
</script>

<template>
  <AppModal v-if="state.open" :title="state.title" @close="dismiss()">
    <p class="confirm-message">{{ state.message }}</p>
    <template #footer>
      <button @click="dismiss()">{{ state.cancelLabel }}</button>
      <button :class="state.danger ? 'danger-solid' : 'primary'" @click="accept()">
        {{ state.confirmLabel }}
      </button>
    </template>
  </AppModal>
</template>
