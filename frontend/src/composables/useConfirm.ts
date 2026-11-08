import { reactive } from 'vue'

export interface ConfirmOptions {
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  /** Style the confirm button as a destructive action. */
  danger?: boolean
}

export interface ConfirmState {
  open: boolean
  title: string
  message: string
  confirmLabel: string
  cancelLabel: string
  danger: boolean
}

// Module-level singleton: every caller shares one dialog host.
const state = reactive<ConfirmState>({
  open: false,
  title: '',
  message: '',
  confirmLabel: 'Confirm',
  cancelLabel: 'Cancel',
  danger: false,
})

let resolver: ((accepted: boolean) => void) | null = null

function settle(accepted: boolean): void {
  if (!state.open) return
  state.open = false
  const resolve = resolver
  resolver = null
  resolve?.(accepted)
}

export function useConfirm(): {
  state: ConfirmState
  confirm: (options: ConfirmOptions) => Promise<boolean>
  accept: () => void
  dismiss: () => void
} {
  function confirm(options: ConfirmOptions): Promise<boolean> {
    settle(false) // a second request cancels the pending dialog
    state.title = options.title
    state.message = options.message
    state.confirmLabel = options.confirmLabel ?? 'Confirm'
    state.cancelLabel = options.cancelLabel ?? 'Cancel'
    state.danger = options.danger ?? false
    state.open = true
    return new Promise((resolve) => {
      resolver = resolve
    })
  }

  return {
    state,
    confirm,
    accept: () => settle(true),
    dismiss: () => settle(false),
  }
}
