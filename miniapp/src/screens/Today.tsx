import { useEffect, useState } from 'react'
import { useScrollActive } from '../components/useScrollActive'
import { DropBadge, IntensityBadge } from '../components/Badges'
import { ExerciseSheet } from '../components/ExerciseSheet'
import { IconCheck, IconChevron, IconPlus } from '../components/icons'
import { NumField } from '../components/NumField'
import {
  capitalize,
  dayFocus,
  formatPrescription,
  getDay,
  getProgram,
  isDropset,
  nextTrainingDay,
  plural,
  programPosition,
  WEEKDAY_LONG,
  WEEKDAY_SHORT,
} from '../program'
import { actions, currentRun, lastSetsFor, useStore, type Workout } from '../store'
import { formatKg } from '../stats'
import { confirm, haptic } from '../telegram'

export function Today() {
  const state = useStore()
  return state.active ? <ActiveWorkout workout={state.active} /> : <DayPreview />
}

function DayPreview() {
  const state = useStore()
  const { programId, startDate, history } = state
  const run = currentRun(state)
  const program = getProgram(programId)
  const pos = programPosition(program, startDate)
  const suggested = nextTrainingDay(program, pos.week, pos.weekday)
  const [week, setWeek] = useState(suggested.week)
  const chipsRef = useScrollActive<HTMLDivElement>(week)
  const [weekday, setWeekday] = useState(suggested.weekday)
  const [sheet, setSheet] = useState<string | null>(null)
  const day = getDay(program, week, weekday)
  const weekDays = program.weeks.find((w) => w.number === week)?.days ?? []
  const doneHere = run.some((w) => w.week === week && w.weekday === weekday)

  const isToday = !pos.finished && !pos.notStarted && week === pos.week && weekday === pos.weekday
  const label = pos.finished
    ? 'Программа завершена'
    : pos.notStarted
      ? 'Программа ещё не началась'
      : isToday
        ? 'Сегодня'
        : suggested.isToday
          ? 'Выбранный день'
          : 'Следующая тренировка'

  return (
    <>
      <div className="hero">
        <div className="eyebrow">{label}</div>
        <h1>{WEEKDAY_LONG[weekday]}</h1>
        {day && (
          <div className="hero-meta">
            <span>{dayFocus(day)}</span>
            <span>{plural(day.exercises.length, ['упражнение', 'упражнения', 'упражнений'])}</span>
            <span>
              {plural(
                day.exercises.reduce((n, e) => n + e.prescription.sets, 0),
                ['подход', 'подхода', 'подходов'],
              )}
            </span>
          </div>
        )}
        <div className="week-progress">
          <span className="hint num">
            Неделя {week} из {program.weeks.length}
          </span>
          <div className="progress">
            <div style={{ width: `${(week / program.weeks.length) * 100}%` }} />
          </div>
        </div>
      </div>

      <div className="spacer" />
      <div className="chips" ref={chipsRef}>
        {program.weeks.map((w) => (
          <button
            key={w.number}
            className={`chip ${w.number === week ? 'active' : ''}`}
            onClick={() => {
              haptic.select()
              setWeek(w.number)
              if (w.days.length && !w.days.some((d) => d.weekday === weekday)) setWeekday(w.days[0].weekday)
            }}
          >
            Н{w.number}
            {w.number === pos.week && <span className="dot" />}
          </button>
        ))}
      </div>
      <div className="spacer" />
      <div className="segmented">
        {weekDays.map((d) => (
          <button
            key={d.weekday}
            className={d.weekday === weekday ? 'active' : ''}
            onClick={() => {
              haptic.select()
              setWeekday(d.weekday)
            }}
          >
            {WEEKDAY_SHORT[d.weekday]} · {dayFocus(d, true)}
          </button>
        ))}
      </div>

      <h2>Упражнения{doneHere && ' · уже выполнено'}</h2>
      <div className="list">
        {day?.exercises.map((e) => {
          const last = lastSetsFor(e.name, history)
          const lastTop = last ? last.reduce((a, b) => ((b.weight ?? 0) > (a.weight ?? 0) ? b : a)) : null
          return (
            <button className="row" key={e.order} onClick={() => setSheet(e.name)}>
              <div className="ex-num">{e.order}</div>
              <div className="grow">
                <div className="title">{capitalize(e.name)}</div>
                <div className="ex-meta">
                  <span className="ex-target num">{formatPrescription(e.prescription)}</span>
                  <IntensityBadge value={e.intensity} />
                  {isDropset(e.prescription) && <DropBadge />}
                  {lastTop && (
                    <span className="num">
                      прошлый раз {formatKg(lastTop.weight ?? 0)} кг × {lastTop.reps}
                    </span>
                  )}
                </div>
              </div>
              <IconChevron />
            </button>
          )
        })}
      </div>

      <div className="bottom-action">
        <button
          className="btn"
          onClick={() => {
            haptic.tap()
            actions.startWorkout(week, weekday)
            window.scrollTo({ top: 0 })
          }}
        >
          Начать тренировку
        </button>
      </div>

      {sheet && <ExerciseSheet name={sheet} onClose={() => setSheet(null)} />}
    </>
  )
}

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [active])
  return now
}

function mmss(sec: number) {
  const s = Math.max(0, Math.round(sec))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function ActiveWorkout({ workout }: { workout: Workout }) {
  const { restSeconds, restEnd } = useStore()
  const setRestEnd = actions.setRestEnd
  const [sheet, setSheet] = useState<string | null>(null)
  const now = useNow(true)
  const program = getProgram(workout.programId)
  const day = getDay(program, workout.week, workout.weekday)

  const total = workout.exercises.reduce((n, e) => n + e.sets.length, 0)
  const done = workout.exercises.reduce((n, e) => n + e.sets.filter((s) => s.done).length, 0)
  const elapsed = (now - new Date(workout.startedAt).getTime()) / 1000
  const restLeft = restEnd ? (restEnd - now) / 1000 : 0

  useEffect(() => {
    if (restEnd && restLeft <= 0) {
      haptic.success()
      setRestEnd(null)
    }
  }, [restEnd, restLeft])

  const toggle = (ei: number, si: number) => {
    const s = workout.exercises[ei].sets[si]
    const next = !s.done
    const reps = s.reps ?? (next ? defaultReps(ei) : null)
    actions.updateSet(ei, si, { done: next, reps })
    haptic.tap()
    if (next) setRestEnd(Date.now() + restSeconds * 1000)
  }

  const defaultReps = (ei: number) => {
    const p = day?.exercises[ei]?.prescription
    return p?.drop_reps?.[0] ?? p?.reps_max ?? null
  }

  return (
    <>
      {restEnd && restLeft > 0 && (
        <div className="rest">
          <span className="num">Отдых {mmss(restLeft)}</span>
          <button onClick={() => setRestEnd(restEnd + 15_000)}>+15</button>
          <button onClick={() => setRestEnd(null)}>✕</button>
        </div>
      )}

      <div className="hero">
        <div className="eyebrow">
          Неделя {workout.week} · {WEEKDAY_SHORT[workout.weekday]} · идёт тренировка
        </div>
        <h1 className="num">{mmss(elapsed)}</h1>
        <div className="week-progress" style={{ marginTop: 6 }}>
          <div className="progress">
            <div style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
          </div>
          <span className="hint num">
            {done}/{total} подходов
          </span>
        </div>
      </div>

      {workout.exercises.map((ex, ei) => {
        const pe = day?.exercises[ei]
        const exDone = ex.sets.length > 0 && ex.sets.every((s) => s.done)
        const p = pe?.prescription
        const repsHint =
          p?.drop_reps?.join('-') ??
          (p?.reps_min != null ? (p.reps_max && p.reps_max !== p.reps_min ? `${p.reps_min}–${p.reps_max}` : `${p.reps_min}`) : '')
        return (
          <div className="ex-card" key={ei}>
            <button className="ex-head" style={{ width: '100%', textAlign: 'left' }} onClick={() => setSheet(ex.name)}>
              <div className={`ex-num ${exDone ? 'complete' : ''}`}>{exDone ? <IconCheck /> : ei + 1}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="ex-name">{capitalize(ex.name)}</div>
                <div className="ex-meta">
                  {pe && <span className="ex-target num">{formatPrescription(pe.prescription)}</span>}
                  {pe && <IntensityBadge value={pe.intensity} />}
                  {ex.dropset && <DropBadge />}
                </div>
              </div>
              <IconChevron />
            </button>

            <div className="sets">
              <div className="sets-head">
                <span>#</span>
                <span>кг</span>
                <span>{ex.dropset ? 'повт. (1-й)' : 'повт.'}</span>
                <span />
              </div>
              {ex.sets.map((s, si) => (
                <div key={si} className={`set-row ${s.done ? 'done' : ''}`}>
                  <span className="set-idx">{si + 1}</span>
                  <NumField decimal value={s.weight} onChange={(v) => actions.updateSet(ei, si, { weight: v })} />
                  <NumField
                    value={s.reps}
                    placeholder={repsHint}
                    onChange={(v) => actions.updateSet(ei, si, { reps: v })}
                  />
                  <button
                    className={`check ${s.done ? 'on' : ''}`}
                    aria-label={s.done ? 'Снять отметку' : 'Подход выполнен'}
                    onClick={() => toggle(ei, si)}
                  >
                    <IconCheck />
                  </button>
                </div>
              ))}
              <button className="add-set" onClick={() => actions.addSet(ei)}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  <IconPlus /> подход
                </span>
              </button>
            </div>
          </div>
        )
      })}

      <div className="spacer" />
      <button
        className="btn danger"
        onClick={async () => {
          if (await confirm('Отменить тренировку? Отмеченные подходы не сохранятся.')) actions.cancelWorkout()
        }}
      >
        Отменить тренировку
      </button>

      <div className="bottom-action">
        <button
          className="btn"
          onClick={async () => {
            const msg = done
              ? `Завершить и сохранить ${done} из ${total} подходов?`
              : 'Ни одного подхода не отмечено. Завершить без сохранения?'
            if (await confirm(msg)) {
              haptic.success()
              actions.finishWorkout()
              window.scrollTo({ top: 0 })
            }
          }}
        >
          Завершить тренировку
        </button>
      </div>

      {sheet && <ExerciseSheet name={sheet} onClose={() => setSheet(null)} />}
    </>
  )
}
