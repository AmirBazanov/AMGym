import { Sheet } from 'gymapp-miniapp'

// The sheet is position:fixed at the bottom of the viewport; the transformed wrapper
// makes this phone-sized box its containing block so the card captures it.
export function WithContent() {
  return (
    <div style={{ position: 'relative', height: 520, overflow: 'hidden', transform: 'translateZ(0)', background: 'var(--bg)' }}>
      <div style={{ padding: 16 }} className="hint">Экран под шторкой</div>
      <Sheet onClose={() => {}}>
        <h1 style={{ fontSize: 22 }}>Жим лёжа</h1>
        <div className="hint">12 раз в программе · рекорд 90 кг</div>
        <h2>В прошлый раз · 4 октября</h2>
        <div className="card">
          <div className="w-sets" style={{ marginTop: 0 }}>
            <span className="w-set">80 × 8</span>
            <span className="w-set">80 × 8</span>
            <span className="w-set">80 × 7</span>
          </div>
        </div>
        <button className="btn" style={{ marginTop: 16 }}>Закрыть</button>
      </Sheet>
    </div>
  )
}
