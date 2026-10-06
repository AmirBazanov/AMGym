import { useState } from 'react'
import { NumField } from 'gymapp-miniapp'

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 12 }}>
      <div className="grow">{label}</div>
      <div style={{ width: 96 }}>{children}</div>
    </div>
  )
}

export function Empty() {
  const [v, setV] = useState<number | null>(null)
  return <Row label="Повторы"><NumField value={v} onChange={setV} placeholder="8" /></Row>
}

export function WithValue() {
  const [v, setV] = useState<number | null>(80)
  return <Row label="Вес, кг"><NumField value={v} onChange={setV} decimal /></Row>
}

export function Decimal() {
  const [v, setV] = useState<number | null>(12.5)
  return <Row label="Гантель, кг"><NumField value={v} onChange={setV} decimal /></Row>
}

export function Invalid() {
  const [v, setV] = useState<number | null>(0)
  return <Row label="Ккал в день"><NumField value={v} onChange={setV} invalid placeholder="2600" /></Row>
}
