import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { tg } from '../telegram'

// Open sheets by when they were first rendered: the largest is the topmost. Numbered while rendering, as a
// parent renders before its children (effects run the other way round). Each maps to its latest onClose.
let lastSheet = 0
const openSheets = new Map<number, RefObject<() => void>>()
// The body's overflow before the first sheet opened; restored only when the last one closes. Saving it per
// sheet broke when a parent and its child unmounted together: the child put back the parent's 'hidden'.
let savedOverflow = ''

function closeTop() {
  if (!openSheets.size) return
  openSheets.get(Math.max(...openSheets.keys()))?.current()
}

/** Shows Telegram's back button while a sheet is open; it closes the topmost one. Older clients: no-op. */
function setBackButton(on: boolean) {
  const b = tg?.BackButton
  if (!b) return
  try {
    if (on) {
      b.onClick(closeTop)
      b.show()
    } else {
      b.offClick(closeTop)
      b.hide()
    }
  } catch {
    // Unsupported in this client version.
  }
}

/** Registers an open sheet: the first one locks page scroll and shows the back button. */
export function registerSheet(id: number, close: RefObject<() => void>) {
  if (!openSheets.size) {
    savedOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    setBackButton(true)
  }
  openSheets.set(id, close)
}

/** The last sheet to close restores page scroll and hides the back button. */
export function unregisterSheet(id: number) {
  if (!openSheets.delete(id) || openSheets.size) return
  document.body.style.overflow = savedOverflow
  setBackButton(false)
}

export function Sheet({ onClose, children, className }: { onClose: () => void; children: ReactNode; className?: string }) {
  const [id] = useState(() => ++lastSheet)
  // Callers pass a new onClose on every render; the key handler reads the latest one.
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  })

  useEffect(() => {
    registerSheet(id, close)
    // Escape closes only the topmost sheet, not every stacked one.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && id === Math.max(...openSheets.keys())) close.current()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      unregisterSheet(id)
      window.removeEventListener('keydown', onKey)
    }
  }, [id])

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className={`sheet ${className ?? ''}`} onClick={(e) => e.stopPropagation()} role="dialog">
        <div className="sheet-grip" />
        {children}
      </div>
    </div>
  )
}
