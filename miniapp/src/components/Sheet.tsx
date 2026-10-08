import { useEffect, useRef, useState, type ReactNode } from 'react'

// Open sheets by when they were first rendered: the largest is the topmost. Numbered while rendering, as a
// parent renders before its children (effects run the other way round).
let lastSheet = 0
const openSheets = new Set<number>()

export function Sheet({ onClose, children, className }: { onClose: () => void; children: ReactNode; className?: string }) {
  const [id] = useState(() => ++lastSheet)
  // Callers pass a new onClose on every render; the key handler reads the latest one.
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  })

  useEffect(() => {
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    openSheets.add(id)
    // Escape closes only the topmost sheet, not every stacked one.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && id === Math.max(...openSheets)) close.current()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      openSheets.delete(id)
      document.body.style.overflow = prev
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
