import { capitalize, getProgram, WEEKDAY_SHORT, formatPrescription } from '../program'
import { useStore } from '../store'
import { exerciseSeries, formatKg, formatLongDate } from '../stats'
import { LineSeries } from './LazyCharts'
import { Sheet } from './Sheet'

export function ExerciseSheet({ name, onClose }: { name: string; onClose: () => void }) {
  const { history, programId } = useStore()
  const program = getProgram(programId)
  const series = exerciseSeries(history, name, null)
  const last = [...history].reverse().find((w) => w.exercises.some((e) => e.name === name))
  const lastEx = last?.exercises.find((e) => e.name === name)
  const best = series.length ? Math.max(...series.map((p) => p.maxWeight)) : null

  const plan = program.weeks.flatMap((w) =>
    w.days.flatMap((d) =>
      d.exercises.filter((e) => e.name === name).map((e) => ({ week: w.number, weekday: d.weekday, e })),
    ),
  )

  return (
    <Sheet onClose={onClose}>
      <h1 style={{ fontSize: 22 }}>{capitalize(name)}</h1>
      <div className="hint">
        {plan.length} раз в программе{best != null && ` · рекорд ${formatKg(best)} кг`}
      </div>

      {series.length > 1 && (
        <>
          <h2>Рабочий вес</h2>
          <div className="chart-card">
            <LineSeries data={series} x="label" y="maxWeight" unit="кг" />
          </div>
        </>
      )}

      {lastEx && last && (
        <>
          <h2>В прошлый раз · {formatLongDate(last.startedAt)}</h2>
          <div className="card">
            <div className="w-sets" style={{ marginTop: 0 }}>
              {lastEx.sets.map((s, i) => (
                <span key={i} className="w-set">
                  {formatKg(s.weight ?? 0)} × {s.reps ?? '—'}
                </span>
              ))}
            </div>
          </div>
        </>
      )}

      <h2>По программе</h2>
      <div className="list">
        {plan.map(({ week, weekday, e }) => (
          <div className="row" key={`${week}-${weekday}-${e.order}`}>
            <div className="grow">
              Неделя {week} · {WEEKDAY_SHORT[weekday]}
            </div>
            <div className="num" style={{ fontWeight: 600 }}>
              {formatPrescription(e.prescription)}
            </div>
          </div>
        ))}
      </div>
    </Sheet>
  )
}
