import { useSyncExternalStore } from 'react'

// In-page confirmation for plain browsers, where window.confirm may be blocked (e.g. sandboxed previews).
interface Pending {
  message: string
  resolve: (ok: boolean) => void
}

let pending: Pending | null = null
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((l) => l())

export function askInPage(message: string): Promise<boolean> {
  return new Promise((resolve) => {
    pending?.resolve(false)
    pending = { message, resolve }
    emit()
  })
}

function settle(ok: boolean) {
  pending?.resolve(ok)
  pending = null
  emit()
}

export function ConfirmHost() {
  const p = useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    () => pending,
  )
  if (!p) return null
  return (
    <div className="sheet-backdrop" style={{ alignItems: 'center', padding: 24 }} onClick={() => settle(false)}>
      <div className="card dialog" role="alertdialog" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 500, marginBottom: 16 }}>{p.message}</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn secondary" onClick={() => settle(false)}>
            Отмена
          </button>
          <button className="btn" onClick={() => settle(true)}>
            Да
          </button>
        </div>
      </div>
    </div>
  )
}
