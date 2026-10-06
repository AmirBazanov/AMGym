import { useEffect, useState } from 'react'
import { useScrollActive } from '../components/useScrollActive'
import { DropBadge, IntensityBadge } from '../components/Badges'
import { ExerciseSheet } from '../components/ExerciseSheet'
import { IconCheck, IconChevron, IconPlus } from '../components/icons'
import { NumField } from '../components/NumField'
import { Sheet } from '../components/Sheet'
import { useWellbeing, useWellbeingSheet, WellbeingDaySheet } from '../components/Wellbeing'
import {
  capitalize,
  dayFocus,
  formatPrescription,
  getDay,
  getProgram,
  isDropset,
  nextTrainingDay,
  plural,
  programExerciseNames,
  programPosition,
  WEEKDAY_LONG,
  WEEKDAY_SHORT,
  type ProgramExercise,
} from '../program'
import { suggestWeight } from '../progression'
import { actions, currentRun, isStarted, lastSetsFor, useStore, type Workout } from '../store'
import { formatKg } from '../stats'
import { confirm, haptic } from '../telegram'
import { entriesCount, formatWellbeing, groupByDate, localISODate } from '../wellbeing'

/** Why the day's weight is what it is, under the exercise name. */
function SuggestHint({ history, exercise }: { history: Workout[]; exercise: ProgramExercise }) {
  const s = suggestWeight(history, exercise)
  if (!s) return null
  return (
    <div className="ex-suggest num">
      {/* The step is already visible in the weight, keep the line short on narrow screens. */}
      предложено {formatKg(s.weight)} кг: {s.reason.replace(/, \+[\d,]+ кг$/, '')}
    </div>
  )
}

export function Today() {
  const state = useStore()
  return (
    <>
      {state.pending.length > 0 && (
        <div className="card notice">
          Не отправлено на сервер: {state.pending.length}. Отправлю, когда появится связь.
        </div>
      )}
      {state.rejected.length > 0 && (
        <div className="card notice">
          Сервер не принял тренировок: {state.rejected.length} (неверные значения). Они остались в истории на этом
          телефоне.
        </div>
      )}
      {state.active ? <ActiveWorkout workout={state.active} /> : <DayPreview />}
    </>
  )
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

      <WellbeingToday />

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
                <SuggestHint history={history} exercise={e} />
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

/**
 * Today's wellbeing from the chat, or a quiet hint; nothing while loading or without access (401).
 * Shown under the hero of both the day preview and the prepared or running workout.
 */
function WellbeingToday({ hint = true }: { hint?: boolean }) {
  const wb = useWellbeing()
  const sheet = useWellbeingSheet(wb.entries)
  if (!wb.entries) return null
  const today = localISODate()
  const entries = groupByDate(wb.entries).find((d) => d.date === today)?.entries
  if (!entries) return hint ? <p className="hint wb-hint">Напиши боту, как спал и что болит</p> : null
  const latest = entries[0]
  return (
    <>
      <button
        type="button"
        className="card wb-card"
        onClick={() => {
          haptic.tap()
          sheet.open(today)
        }}
      >
        <div className="grow">
          <div className="eyebrow">
            Самочувствие сегодня
            {entries.length > 1 && <span className="wb-count"> · {entriesCount(entries.length)}</span>}
          </div>
          <div className="wb-text">{formatWellbeing(latest)}</div>
        </div>
        <IconChevron />
      </button>
      <WellbeingDaySheet wb={wb} sheet={sheet} />
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
  const { restSeconds, restEnd, history } = useStore()
  const setRestEnd = actions.setRestEnd
  const [sheet, setSheet] = useState<string | null>(null)
  const now = useNow(true)
  const program = getProgram(workout.programId)
  const day = getDay(program, workout.week, workout.weekday)

  const total = workout.exercises.reduce((n, e) => n + e.sets.length, 0)
  const done = workout.exercises.reduce((n, e) => n + e.sets.filter((s) => s.done).length, 0)
  const started = isStarted(workout)
  const [adding, setAdding] = useState(false)
  const elapsed = (now - new Date(workout.startedAt).getTime()) / 1000
  const restLeft = restEnd ? (restEnd - now) / 1000 : 0

  useEffect(() => {
    if (restEnd && restLeft <= 0) {
      haptic.success()
      setRestEnd(null)
    }
  }, [restEnd, restLeft])

  const [needReps, setNeedReps] = useState<string | null>(null)
  const toggle = (ei: number, si: number) => {
    const s = workout.exercises[ei].sets[si]
    const next = !s.done
    // Reps: what was typed, else the previous set's, else the top of the planned range.
    const reps = s.reps ?? (next ? (workout.exercises[ei].sets[si - 1]?.reps ?? defaultReps(ei)) : null)
    if (next && reps == null) {
      haptic.error()
      setNeedReps(`${ei}-${si}`)
      return
    }
    setNeedReps(null)
    actions.updateSet(ei, si, { done: next, reps })
    haptic.tap()
    if (next) setRestEnd(Date.now() + restSeconds * 1000)
  }

  const defaultReps = (ei: number) => {
    const p = day?.exercises.find((e) => e.name === workout.exercises[ei].name)?.prescription
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
        {started ? (
          <>
            <div className="eyebrow">
              Неделя {workout.week} · {WEEKDAY_SHORT[workout.weekday]} · идёт тренировка
            </div>
            <h1 className="num">{mmss(elapsed)}</h1>
          </>
        ) : (
          <>
            <div className="eyebrow">Тренировка на сегодня · неделя {workout.week}</div>
            <h1>
              {day ? dayFocus(day) : WEEKDAY_LONG[workout.weekday]}
              <span className="muted" style={{ fontWeight: 500 }}>
                {' '}
                · {WEEKDAY_SHORT[workout.weekday]}
              </span>
            </h1>
            <div className="hint">Отметь первый подход, и пойдёт секундомер</div>
          </>
        )}
        <div className="week-progress" style={{ marginTop: 6 }}>
          <div className="progress">
            <div style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
          </div>
          <span className="hint num">
            {done}/{total} подходов
          </span>
        </div>
      </div>
      {/* Also during the workout: a sore shoulder matters when picking the weight. The hint only before it starts. */}
      <WellbeingToday hint={!started} />

      {workout.exercises.map((ex, ei) => {
        const pe = day?.exercises.find((e) => e.name === ex.name)
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
                {pe && <SuggestHint history={history} exercise={pe} />}
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
                    invalid={needReps === `${ei}-${si}`}
                    placeholder={needReps === `${ei}-${si}` ? 'повт.?' : repsHint}
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
              {!pe && (
                <button className="add-set" style={{ color: 'var(--danger)' }} onClick={() => actions.removeExercise(ei)}>
                  Убрать упражнение
                </button>
              )}
            </div>
          </div>
        )
      })}

      <div className="spacer" />
      <button className="btn secondary" onClick={() => setAdding(true)}>
        <IconPlus /> Добавить упражнение
      </button>
      <button
        className="btn danger"
        onClick={async () => {
          if (!started || (await confirm('Отменить тренировку? Отмеченные подходы не сохранятся.')))
            actions.cancelWorkout()
        }}
      >
        {started ? 'Отменить тренировку' : 'Не сегодня'}
      </button>
      {adding && (
        <AddExerciseSheet
          exclude={workout.exercises.map((e) => e.name)}
          onPick={(name) => {
            actions.addExercise(name)
            setAdding(false)
            haptic.tap()
            setTimeout(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }), 50)
          }}
          onClose={() => setAdding(false)}
        />
      )}

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

function AddExerciseSheet({
  exclude,
  onPick,
  onClose,
}: {
  exclude: string[]
  onPick: (name: string) => void
  onClose: () => void
}) {
  const { programId } = useStore()
  const [q, setQ] = useState('')
  const query = q.trim().toLowerCase()
  const names = programExerciseNames(getProgram(programId)).filter((n) => !exclude.includes(n))
  const filtered = names.filter((n) => n.includes(query))
  const exact = names.includes(query) || exclude.includes(query)
  return (
    <Sheet onClose={onClose}>
      <h1 style={{ fontSize: 22 }}>Добавить упражнение</h1>
      <div className="spacer" />
      <input
        className="search"
        autoFocus
        placeholder="Найти или вписать своё"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="spacer" />
      <div className="list">
        {query && !exact && (
          <button className="row" onClick={() => onPick(query)}>
            <div className="grow" style={{ color: 'var(--link)', fontWeight: 500 }}>
              Добавить «{q.trim()}»
            </div>
          </button>
        )}
        {filtered.map((n) => (
          <button className="row" key={n} onClick={() => onPick(n)}>
            <div className="grow title">{capitalize(n)}</div>
            <IconPlus />
          </button>
        ))}
        {!filtered.length && !query && <div className="empty">Все упражнения программы уже в тренировке</div>}
      </div>
    </Sheet>
  )
}
