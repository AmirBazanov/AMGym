import { useState } from 'react'
import { BarSeries, LineSeries } from '../components/LazyCharts'
import { capitalize, getProgram, programExerciseNames } from '../program'
import { bestE1rm } from '../progression'
import { currentRun, useStore } from '../store'
import { exerciseSeries, formatKg, formatShortDate, formatTonnage, weeklyVolume } from '../stats'
import { haptic } from '../telegram'
import { BodyWeightSection } from './ProgressWeight'

type Metric = 'maxWeight' | 'e1rm' | 'volume'
const METRICS: { key: Metric; label: string; title: string; unit: string }[] = [
  { key: 'maxWeight', label: 'Вес', title: 'Рабочий вес', unit: 'кг' },
  { key: 'e1rm', label: '1ПМ', title: 'Расчётный 1ПМ', unit: 'кг' },
  { key: 'volume', label: 'Объём', title: 'Объём за тренировку', unit: 'кг' },
]
const PERIODS: { days: number | null; label: string }[] = [
  { days: 28, label: '4 нед' },
  { days: 56, label: '8 нед' },
  { days: null, label: 'Всё' },
]

export function Progress() {
  const state = useStore()
  const { history, programId } = state
  const program = getProgram(programId)
  const done = new Set(history.flatMap((w) => w.exercises.map((e) => e.name)))
  const names = programExerciseNames(program).filter((n) => done.has(n))
  const [name, setName] = useState(names[0] ?? '')
  const [metric, setMetric] = useState<Metric>('maxWeight')
  const [period, setPeriod] = useState<number | null>(56)

  // Body weight does not depend on workouts: shown even before the first one.
  if (!names.length)
    return (
      <>
        <div className="hero">
          <div className="eyebrow">Прогресс</div>
        </div>
        <div className="card hint">Графики силы появятся после первой тренировки.</div>
        <BodyWeightSection />
      </>
    )

  const series = exerciseSeries(history, name, period)
  const m = METRICS.find((x) => x.key === metric)!
  const first = series[0]
  const lastP = series[series.length - 1]
  const best = series.length ? Math.max(...series.map((p) => p.maxWeight)) : 0
  const since = period == null ? 0 : Date.now() - period * 86_400_000
  // Same record function as the exercise sheet, so "Всё" shows the same 1RM there and here.
  const bestE1 = bestE1rm(history.filter((w) => new Date(w.startedAt).getTime() >= since), name)?.e1rm ?? 0
  const growth = first && lastP && first.e1rm ? ((lastP.e1rm - first.e1rm) / first.e1rm) * 100 : 0
  const weekly = weeklyVolume(currentRun(state).filter((w) => new Date(w.startedAt).getTime() >= since)).map((w) => ({
    ...w,
    label: `Н${w.week}`,
    tons: Math.round(w.volume / 100) / 10,
  }))

  return (
    <>
      <div className="hero">
        <div className="eyebrow">Прогресс</div>
      </div>
      <select className="select" value={name} onChange={(e) => setName(e.target.value)}>
        {names.map((n) => (
          <option key={n} value={n}>
            {capitalize(n)}
          </option>
        ))}
      </select>
      <div className="spacer" />
      <div className="segmented">
        {PERIODS.map((p) => (
          <button
            key={p.label}
            className={p.days === period ? 'active' : ''}
            onClick={() => {
              haptic.select()
              setPeriod(p.days)
            }}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div className="spacer" />
      <div className="tiles">
        <div className="tile">
          <div className="v">
            {formatKg(best)}
            <small>кг</small>
          </div>
          <div className="k">макс. вес</div>
        </div>
        <div className="tile">
          <div className="v">
            {formatKg(Math.round(bestE1))}
            <small>кг</small>
          </div>
          <div className="k">оценка 1ПМ</div>
        </div>
        <div className="tile">
          <div className={`v ${growth > 0 ? 'delta-up' : ''}`}>
            {growth > 0 ? '+' : ''}
            {growth.toFixed(0)}
            <small>%</small>
          </div>
          <div className="k">рост 1ПМ</div>
        </div>
      </div>

      <h2>{m.title}</h2>
      <div className="segmented" style={{ marginBottom: 10 }}>
        {METRICS.map((x) => (
          <button
            key={x.key}
            className={x.key === metric ? 'active' : ''}
            onClick={() => {
              haptic.select()
              setMetric(x.key)
            }}
          >
            {x.label}
          </button>
        ))}
      </div>
      <div className="chart-card">
        {series.length > 1 ? (
          <LineSeries data={series} x="label" y={metric} unit={m.unit} />
        ) : (
          <div className="empty">Нужно хотя бы две тренировки за период</div>
        )}
      </div>

      {series.length > 0 && (
        <div className="card" style={{ marginTop: 10 }}>
          <table className="points">
            <thead>
              <tr>
                <th>Дата</th>
                <th>Вес</th>
                <th>1ПМ</th>
                <th>Подходы</th>
                <th>Объём</th>
              </tr>
            </thead>
            <tbody>
              {[...series]
                .reverse()
                .slice(0, 6)
                .map((p) => (
                  <tr key={p.date}>
                    <td>{formatShortDate(p.date)}</td>
                    <td>{formatKg(p.maxWeight)}</td>
                    <td>{formatKg(Math.round(p.e1rm))}</td>
                    <td>{p.sets}</td>
                    <td>{formatTonnage(p.volume)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>Тоннаж по неделям, все упражнения</h2>
      <div className="chart-card">
        <BarSeries data={weekly} x="label" y="tons" unit="т" />
      </div>

      <BodyWeightSection />
    </>
  )
}
