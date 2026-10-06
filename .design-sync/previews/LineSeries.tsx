import { LineSeries } from 'gymapp-miniapp'

const weights = [
  { label: '2 сен', maxWeight: 70 },
  { label: '5 сен', maxWeight: 72.5 },
  { label: '9 сен', maxWeight: 72.5 },
  { label: '12 сен', maxWeight: 75 },
  { label: '16 сен', maxWeight: 75 },
  { label: '19 сен', maxWeight: 77.5 },
  { label: '23 сен', maxWeight: 80 },
  { label: '26 сен', maxWeight: 80 },
  { label: '30 сен', maxWeight: 82.5 },
  { label: '3 окт', maxWeight: 85 },
]

export function WorkingWeight() {
  return (
    <div className="chart-card">
      <LineSeries data={weights} x="label" y="maxWeight" unit="кг" />
    </div>
  )
}

export function Volume() {
  const data = weights.map((p, i) => ({ label: p.label, volume: 1800 + i * 120 + (i % 3) * 90 }))
  return (
    <div className="chart-card">
      <LineSeries data={data} x="label" y="volume" unit="кг" />
    </div>
  )
}
