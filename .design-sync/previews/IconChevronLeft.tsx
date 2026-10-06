import { IconChevronLeft } from 'gymapp-miniapp'

// Icons are fixed 24px SVGs that inherit currentColor.
export function Colors() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 20, padding: 8 }}>
      <span style={{ color: 'var(--hint)', display: 'inline-flex' }}><IconChevronLeft /></span>
      <span style={{ color: 'var(--text)', display: 'inline-flex' }}><IconChevronLeft /></span>
      <span style={{ color: 'var(--accent)', display: 'inline-flex' }}><IconChevronLeft /></span>
    </div>
  )
}

export function InTabBar() {
  return (
    <div className="tabbar" style={{ position: 'static' }}>
      <button className="tab active"><IconChevronLeft /><span>Подпись</span></button>
    </div>
  )
}
