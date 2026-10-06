import { useState } from 'react'
import { capitalize, WEEKDAY_SHORT } from '../program'
import { actions, useStore, type Workout } from '../store'
import { exerciseVolume, formatKg, formatLongDate, formatTonnage, workoutSetCount, workoutVolume } from '../stats'
import { confirm } from '../telegram'

export function History() {
  const { history } = useStore()
  const [open, setOpen] = useState<string | null>(null)
  const sorted = [...history].reverse()
  const monthAgo = Date.now() - 30 * 86_400_000
  const recent = history.filter((w) => new Date(w.startedAt).getTime() >= monthAgo)

  if (!history.length)
    return (
      <div className="empty">
        <div className="big">🏋️</div>
        Тренировок пока нет.
        <br />
        Начни первую на вкладке «Сегодня».
      </div>
    )

  // Group by program week, newest first.
  const groups: { week: number; items: Workout[] }[] = []
  for (const w of sorted) {
    const g = groups[groups.length - 1]
    if (g && g.week === w.week) g.items.push(w)
    else groups.push({ week: w.week, items: [w] })
  }

  return (
    <>
      <div className="hero">
        <div className="eyebrow">История</div>
        <h1>За 30 дней</h1>
      </div>
      <div className="spacer" />
      <div className="tiles">
        <div className="tile">
          <div className="v">{recent.length}</div>
          <div className="k">тренировок</div>
        </div>
        <div className="tile">
          <div className="v">{recent.reduce((n, w) => n + workoutSetCount(w), 0)}</div>
          <div className="k">подходов</div>
        </div>
        <div className="tile">
          <div className="v">{formatTonnage(recent.reduce((n, w) => n + workoutVolume(w), 0))}</div>
          <div className="k">тоннаж</div>
        </div>
      </div>

      {groups.map((g) => (
        <div key={`${g.week}-${g.items[0].id}`}>
          <h2>Неделя {g.week}</h2>
          {g.items.map((w) => (
            <WorkoutCard key={w.id} w={w} open={open === w.id} onToggle={() => setOpen(open === w.id ? null : w.id)} />
          ))}
        </div>
      ))}
    </>
  )
}

function WorkoutCard({ w, open, onToggle }: { w: Workout; open: boolean; onToggle: () => void }) {
  const d = new Date(w.startedAt)
  const minutes = w.finishedAt ? Math.round((new Date(w.finishedAt).getTime() - d.getTime()) / 60_000) : null
  return (
    <div className="w-card">
      <button className="w-head" onClick={onToggle}>
        <div className="w-icon">
          {d.getDate()}
          <small>{WEEKDAY_SHORT[w.weekday]}</small>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="title" style={{ fontWeight: 600 }}>
            {capitalize(formatLongDate(w.startedAt))}
          </div>
          <div className="sub hint num">
            {w.exercises.length} упр. · {workoutSetCount(w)} подх. · {formatTonnage(workoutVolume(w))}
            {minutes != null && ` · ${minutes} мин`}
          </div>
        </div>
      </button>
      {open && (
        <div className="w-body">
          {w.exercises.map((ex) => (
            <div className="w-ex" key={ex.name}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ fontWeight: 500 }}>{capitalize(ex.name)}</span>
                <span className="hint num" style={{ whiteSpace: 'nowrap' }}>
                  {formatTonnage(exerciseVolume(ex))}
                </span>
              </div>
              <div className="w-sets">
                {ex.sets.map((s, i) => (
                  <span className="w-set" key={i}>
                    {formatKg(s.weight ?? 0)} × {s.reps ?? '—'}
                  </span>
                ))}
              </div>
            </div>
          ))}
          <button
            className="btn danger"
            onClick={async () => {
              if (await confirm('Удалить эту тренировку из истории?')) actions.deleteWorkout(w.id)
            }}
          >
            Удалить тренировку
          </button>
        </div>
      )}
    </div>
  )
}
