import { useEffect, type ReactNode } from 'react'

export function Sheet({ onClose, children, className }: { onClose: () => void; children: ReactNode; className?: string }) {
  useEffect(() => {
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = prev
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className={`sheet ${className ?? ''}`} onClick={(e) => e.stopPropagation()} role="dialog">
        <div className="sheet-grip" />
        {children}
      </div>
    </div>
  )
}
