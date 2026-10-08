import { useEffect, useState } from 'react'
import { ApiError, getTodayPlan, regenerateTodayPlan, type DayPlan } from '../api'
import { useScrollActive } from '../components/useScrollActive'
import { DropBadge, IntensityBadge } from '../components/Badges'
import { ExerciseSheet } from '../components/ExerciseSheet'
import { IconCheck, IconChevron, IconPlus } from '../components/icons'
import { NumField } from '../components/NumField'
import { RecordToast } from '../components/RecordToast'
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
  type ProgramDay,
} from '../program'
import {
  applyPlan,
  planKey,
  planNote,
  planTitle,
  todaysProgramDay,
  type AdjustedExercise,
  type AppliedPlan,
  type PlanMode,
} from '../plan'
import { overrideReason, perHand, shortReason, type Suggestion } from '../progression'
import { actions, currentRun, isStarted, lastSetsFor, overridesFor, planMode, useStore, type Workout } from '../store'
import { formatKg } from '../stats'
import type { Topic } from '../liveCore'
import { useRemote } from '../useRemote'
import { confirm, haptic } from '../telegram'
import { entriesCount, formatWellbeing, groupByDate, localISODate } from '../wellbeing'

/** "15 кг на руку" for dumbbells (the number is one dumbbell), else "60 кг". */
function kgText(kg: number, hand: boolean): string {
  return `${formatKg(kg)} кг${hand ? ' на руку' : ''}`
}

/**
 * Why the day's weight is what it is, under the exercise name. Without a number (no history, a cable or
 * machine hint, nothing transferable) the line says how to pick the weight instead.
 */
function SuggestHint({ s }: { s: Suggestion }) {
  return (
    <div className="ex-suggest num">
      {/* The step is already visible in the weight, keep the line short on narrow screens. */}
      {s.weight != null ? `предложено ${kgText(s.weight, s.perHand)}: ${shortReason(s)}` : shortReason(s)}
    </div>
  )
}

/** "3 × 8–12" and "(по плану 5 × 8–12)" as two unbreakable pieces, so a range never splits at 360 px. */
function PlanTarget({ adj }: { adj: AdjustedExercise }) {
  const now = formatPrescription(adj.exercise.prescription)
  const was = formatPrescription(adj.original.prescription)
  return (
    <>
      <span className="ex-target num">{now}</span>
      {now !== was && <span className="ex-was num">(по плану {was})</span>}
    </>
  )
}

/**
 * Weight hint for a day exercise: the owner's own number for today, else with a plan factor the
 * corrected weight, else why the suggestion is what it is.
 */
function PlanHint({ adj }: { adj: AdjustedExercise }) {
  const note = planNote(adj)
  const hand = perHand(adj.exercise.name)
  return (
    <>
      {adj.override && adj.weight != null ? (
        <div className="ex-suggest num">
          {overrideReason(adj.weight)}
          {hand ? ' на руку' : ''}
        </div>
      ) : adj.factor !== 1 && adj.weight != null && adj.baseWeight != null ? (
        <div className="ex-suggest num">
          предложено {kgText(adj.weight, hand)}: {Math.round(adj.factor * 100)} % от {formatKg(adj.baseWeight)} кг
        </div>
      ) : (
        adj.suggestion && <SuggestHint s={adj.suggestion} />
      )}
      {note && <div className="ex-plan-note">по плану: {note}</div>}
    </>
  )
}

interface DayPlanState {
  /** Last good answer: undefined = unknown (loading, offline, 401), null = no plan today (404). */
  plan: DayPlan | null | undefined
  busy: boolean
  failed: boolean
  regenerate: () => void
}

// The server's plan depends on the history, settings and wellbeing notes too.
const PLAN_TOPICS: readonly Topic[] = ['plan', 'workouts', 'state', 'wellbeing']

/** Today's adaptive plan. Loaded on open and on return to the app; a failed refetch keeps the last answer. */
function useDayPlan(): DayPlanState {
  const r = useRemote<DayPlan>('plan:today', getTodayPlan, PLAN_TOPICS)
  const [plan, setPlan] = useState<DayPlan | null | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (r.data) setPlan(r.data)
    else if (r.error instanceof ApiError && r.error.status === 404) setPlan(null)
  }, [r.data, r.error])

  async function regenerate() {
    if (busy) return
    haptic.tap()
    setBusy(true)
    setFailed(false)
    try {
      setPlan(await regenerateTodayPlan())
      haptic.success()
    } catch (err) {
      haptic.error()
      if (err instanceof ApiError && err.status === 404) setPlan(null)
      else setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  return { plan, busy, failed, regenerate }
}

/** The plan to show and apply: adjusted and dated today (server TIMEZONE = phone time zone, as elsewhere). */
function visiblePlan(dp: DayPlanState): DayPlan | null {
  const p = dp.plan
  return p && p.adjusted && p.date === localISODate() ? p : null
}

function PlanBanner({
  dp,
  mode,
  started,
  applied,
}: {
  dp: DayPlanState
  mode: PlanMode
  started: boolean
  applied: AppliedPlan | null
}) {
  const plan = visiblePlan(dp)
  if (!plan) return null
  const rest = plan.readiness === 'rest'
  return (
    <div className={`card plan-banner ${rest ? 'plan-rest' : ''}`}>
      <div className="plan-title">{planTitle(plan)}</div>
      {mode === 'adjusted' && applied?.applied && applied.skipped.length > 0 && (
        <div className="hint plan-skipped">Сегодня без: {applied.skipped.map((x) => x.name).join(', ')}</div>
      )}
      <div className="segmented plan-toggle" role="group" aria-label="План на сегодня">
        {(
          [
            ['program', 'Как в программе'],
            ['adjusted', 'С поправкой'],
          ] as const
        ).map(([m, label]) => (
          <button
            type="button"
            key={m}
            className={mode === m ? 'active' : ''}
            aria-pressed={mode === m}
            onClick={() => {
              if (mode === m) return
              haptic.select()
              actions.setPlanMode(m)
            }}
          >
            {label}
          </button>
        ))}
      </div>
      {started && <div className="hint plan-started">Тренировка уже идёт: переключатель не меняет её подходы.</div>}
      <button type="button" className="btn ghost plan-regen" disabled={dp.busy} onClick={dp.regenerate}>
        {dp.busy ? 'Пересчитываю…' : 'Пересчитать'}
      </button>
      {dp.failed && <div className="hint plan-error">Не удалось пересчитать. Проверь интернет и попробуй ещё раз.</div>}
    </div>
  )
}

export function Today() {
  const state = useStore()
  const dp = useDayPlan()
  const mode = planMode(state)
  // Rebuild the prepared (not started) workout when the plan's content or the mode changes; never on
  // an unknown answer (offline, 401), so a failed refetch does not undo the correction.
  const planContent = dp.plan === undefined ? null : planKey(visiblePlan(dp))
  // New baselines need no rebuild here: store.applyServer refills the prepared workout in place.
  const activeId = state.active?.id
  useEffect(() => {
    if (planContent != null) actions.applyDayPlan(visiblePlan(dp))
    // `dp` is rebuilt every render; planContent describes it fully.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planContent, mode, activeId])
  return (
    <>
      <RecordToast />
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
      {state.active ? (
        <ActiveWorkout workout={state.active} dp={dp} />
      ) : (
        // Remount on a program or start date change (also from the chat), so the picked week/day follows.
        <DayPreview key={`${state.programId}:${state.startDate}`} dp={dp} />
      )}
    </>
  )
}

function DayPreview({ dp }: { dp: DayPlanState }) {
  const state = useStore()
  const { programId, startDate, history, baselines } = state
  const run = currentRun(state)
  const program = getProgram(programId)
  const pos = programPosition(program, startDate)
  const suggested = nextTrainingDay(program, pos.week, pos.weekday)
  const [week, setWeek] = useState(suggested.week)
  const chipsRef = useScrollActive<HTMLDivElement>(week)
  const [weekday, setWeekday] = useState(suggested.weekday)
  const [sheet, setSheet] = useState<string | null>(null)
  const day = getDay(program, week, weekday)
  const mode = planMode(state)
  const isToday = !pos.finished && !pos.notStarted && week === pos.week && weekday === pos.weekday
  // Today's overrides show on the day today's workout would be (what prepareToday picks), not on others.
  const pick = todaysProgramDay(program, startDate, run)
  const overrides = pick && pick.week === week && pick.weekday === weekday ? state.weightOverrides : []
  const applied = day
    ? applyPlan(day, mode === 'adjusted' ? visiblePlan(dp) : null, history, localISODate(), week, baselines, overrides)
    : null
  const weekDays = program.weeks.find((w) => w.number === week)?.days ?? []
  const doneHere = run.some((w) => w.week === week && w.weekday === weekday)
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
            <span>{plural(applied!.exercises.length, ['упражнение', 'упражнения', 'упражнений'])}</span>
            <span>
              {plural(
                applied!.exercises.reduce((n, a) => n + a.exercise.prescription.sets, 0),
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
      <PlanBanner dp={dp} mode={mode} started={false} applied={applied} />

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
        {applied?.exercises.map((a) => {
          const e = a.exercise
          const last = lastSetsFor(e.name, history)
          const lastTop = last ? last.reduce((x, y) => ((y.weight ?? 0) > (x.weight ?? 0) ? y : x)) : null
          return (
            <button className="row" key={a.original.order} onClick={() => setSheet(e.name)}>
              <div className="ex-num">{e.order}</div>
              <div className="grow">
                <div className="title">{capitalize(e.name)}</div>
                <div className="ex-meta">
                  <PlanTarget adj={a} />
                  <IntensityBadge value={e.intensity} />
                  {isDropset(e.prescription) && <DropBadge />}
                  {lastTop && (
                    <span className="num">
                      прошлый раз {formatKg(lastTop.weight ?? 0)} кг × {lastTop.reps}
                    </span>
                  )}
                </div>
                <PlanHint adj={a} />
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

/** The day exercise behind a logged one: from the list the workout was built from, else the program as written. */
function lookupExercise(built: AppliedPlan | null, day: ProgramDay | undefined, name: string): AdjustedExercise | undefined {
  const hit = built?.exercises.find((a) => a.exercise.name === name)
  if (hit) return hit
  const e = day?.exercises.find((x) => x.name === name)
  if (!e) return undefined
  const target = formatPrescription(e.prescription)
  return {
    exercise: e,
    original: e,
    replaced: false,
    weight: null,
    baseWeight: null,
    factor: 1,
    override: null,
    suggestion: null,
    reason: null,
    target,
    changed: false,
  }
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

function ActiveWorkout({ workout, dp }: { workout: Workout; dp: DayPlanState }) {
  const state = useStore()
  const { restSeconds, restEnd, history, baselines } = state
  const weightOverrides = overridesFor(workout, state)
  const setRestEnd = actions.setRestEnd
  const [sheet, setSheet] = useState<string | null>(null)
  const now = useNow(true)
  const program = getProgram(workout.programId)
  const day = getDay(program, workout.week, workout.weekday)
  const mode = planMode(state)
  const plan = visiblePlan(dp)
  // Look exercises up in what the workout was built from, so replaced ones keep their prescription.
  const builtFromPlan = plan != null && state.activePlanKey === planKey(plan)
  const today = localISODate()
  const built = day
    ? applyPlan(day, builtFromPlan ? plan : null, history, today, workout.week, baselines, weightOverrides)
    : null
  const shown = day
    ? applyPlan(day, mode === 'adjusted' ? plan : null, history, today, workout.week, baselines, weightOverrides)
    : null
  const lookup = (name: string) => lookupExercise(built, day, name)

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
    const p = lookup(workout.exercises[ei].name)?.exercise.prescription
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
      <PlanBanner dp={dp} mode={mode} started={started} applied={shown} />

      {workout.exercises.map((ex, ei) => {
        const adj = lookup(ex.name)
        const pe = adj?.exercise
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
                  {adj && <PlanTarget adj={adj} />}
                  {pe && <IntensityBadge value={pe.intensity} />}
                  {ex.dropset && <DropBadge />}
                </div>
                {adj && <PlanHint adj={adj} />}
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
