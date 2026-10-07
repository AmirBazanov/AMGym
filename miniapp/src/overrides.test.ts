// Weight overrides: today's working weight the owner set in the bot chat («поставь сегодня жим 85»).
import { describe, expect, it } from 'vitest'
import type { DayPlan, DayPlanExercise } from './api'
import {
  applyPlan,
  overridesForWorkout,
  planNote,
  refillSuggestions,
  scaleWeight,
  suggestionsOf,
  todaysProgramDay,
} from './plan'
import type { Intensity, Prescription, Program, ProgramDay, ProgramExercise } from './program'
import { findOverride, mergeOverrides, normalizeOverrides, suggestWeight } from './progression'
import type { Baseline, ExerciseLog, WeightOverride, Workout } from './store'

const TODAY = '2026-10-07'
const YESTERDAY = '2026-10-06'
const BENCH = 'жим лёжа'
const CURL = 'сгибания с гантелями'
const PRESS = 'жим гантелей сидя'
const RANGE: Prescription = { sets: 4, reps_min: 8, reps_max: 12, drop_reps: null, raw: '4х8-12' }

function ex(name: string, order: number, intensity: Intensity | null = null): ProgramExercise {
  return { name, intensity, prescription: { ...RANGE }, order }
}

const DAY: ProgramDay = { weekday: 3, title: 'День', exercises: [ex(BENCH, 1, 'heavy'), ex(CURL, 2, 'medium')] }

/** Finished workout with one exercise; sets are [weight, reps] pairs, all done. */
function workout(date: string, name: string, sets: [number, number][]): Workout {
  return {
    id: `w-${date}-${name}`,
    programId: 'p',
    week: 1,
    weekday: 1,
    startedAt: date,
    finishedAt: date,
    exercises: [{ name, target: '', dropset: false, sets: sets.map(([weight, reps]) => ({ weight, reps, done: true })) }],
  }
}

// 67.5 × 10 in all sets misses 12 reps: the history suggests 67.5 again (the record gives less).
const HISTORY = [workout('2026-10-01T10:00:00Z', BENCH, [[67.5, 10], [67.5, 10], [67.5, 10]])]
const B90: Baseline[] = [{ exercise: BENCH, weightKg: 90, reps: 8, factId: 1 }] // heavy 12 reps -> 82.5
const O85: WeightOverride[] = [{ exercise: BENCH, weightKg: 85, date: TODAY }]

function pe(name: string, over: Partial<DayPlanExercise> = {}): DayPlanExercise {
  return { name, sets: null, repsMin: null, repsMax: null, weightFactor: null, skip: false, replaceWith: null, reason: null, ...over }
}

function plan(exercises: DayPlanExercise[]): DayPlan {
  return { date: TODAY, adjusted: true, readiness: 'normal', summary: null, exercises }
}

describe('normalizeOverrides / mergeOverrides', () => {
  it('an absent field (older server) keeps the current list; a list replaces it', () => {
    const cur = [...O85]
    expect(mergeOverrides(undefined, cur)).toBe(cur)
    expect(mergeOverrides([], cur)).toEqual([])
    expect(mergeOverrides(null, cur)).toEqual([])
    expect(mergeOverrides([{ exercise: CURL, weightKg: 14, date: TODAY }], cur)).toEqual([
      { exercise: CURL, weightKg: 14, date: TODAY },
    ])
  })

  it('drops entries without a name, a positive weight or a YYYY-MM-DD date', () => {
    expect(
      normalizeOverrides([
        { exercise: ' ', weightKg: 85, date: TODAY },
        { exercise: BENCH, weightKg: 0, date: TODAY },
        { exercise: BENCH, weightKg: Number.NaN, date: TODAY },
        { exercise: BENCH, weightKg: '85', date: TODAY },
        { exercise: BENCH, weightKg: 85, date: '07.10.2026' },
        { exercise: BENCH, weightKg: 85 },
        null,
        { exercise: BENCH, weightKg: 85, date: TODAY, extra: 1 },
      ]),
    ).toEqual(O85)
    expect(normalizeOverrides({})).toEqual([])
  })
})

describe('findOverride', () => {
  it('folds case, spaces and ё/е like baselines', () => {
    const list = [{ exercise: ' Жим  лежа ', weightKg: 85, date: TODAY }]
    expect(findOverride(list, BENCH, TODAY)?.weightKg).toBe(85)
    expect(findOverride([{ exercise: BENCH, weightKg: 85, date: TODAY }], 'ЖИМ ЛЕЖА', TODAY)?.weightKg).toBe(85)
  })

  it('ignores another date and another exercise', () => {
    expect(findOverride(O85, BENCH, YESTERDAY)).toBeNull()
    expect(findOverride(O85, CURL, TODAY)).toBeNull()
  })

  it('the last one for the same day wins', () => {
    const list = [
      { exercise: BENCH, weightKg: 85, date: TODAY },
      { exercise: BENCH, weightKg: 80, date: YESTERDAY },
      { exercise: BENCH, weightKg: 87.5, date: TODAY },
    ]
    expect(findOverride(list, BENCH, TODAY)?.weightKg).toBe(87.5)
  })
})

describe('suggestWeight with overrides', () => {
  const bench = ex(BENCH, 1, 'heavy')

  it('priority: override for today > history > baseline', () => {
    expect(suggestWeight([], bench, B90)?.weight).toBe(82.5)
    expect(suggestWeight(HISTORY, bench, B90)?.weight).toBe(67.5)
    const s = suggestWeight(HISTORY, bench, B90, O85, TODAY)
    expect(s).toEqual({ weight: 85, reason: 'ты поставил на сегодня 85 кг', override: O85[0] })
    expect(suggestWeight([], bench, B90, O85, TODAY)?.weight).toBe(85)
  })

  it('an override for another day, or no day given, is ignored', () => {
    expect(suggestWeight(HISTORY, bench, B90, O85, YESTERDAY)?.weight).toBe(67.5)
    expect(suggestWeight(HISTORY, bench, B90, O85)?.weight).toBe(67.5)
  })

  it('takes the number as is, off the equipment step too', () => {
    const s = suggestWeight([], bench, [], [{ exercise: BENCH, weightKg: 83, date: TODAY }], TODAY)
    expect(s?.weight).toBe(83)
    expect(s?.reason).toBe('ты поставил на сегодня 83 кг')
  })
})

describe('the workout trained today', () => {
  // Monday start; Monday and Wednesday training days. 2026-10-05 Mon, 10-07 Wed, 10-08 Thu.
  const MON: ProgramDay = { ...DAY, weekday: 1 }
  const P: Program = {
    id: 'p',
    name: 'P',
    source: '',
    weeks: [1, 2].map((number) => ({ number, days: [MON, DAY] })),
  }
  const START = '2026-10-05'
  const THU = new Date(2026, 9, 8, 18)
  const THU_KEY = '2026-10-08'
  const doneMonday = { ...workout('2026-10-05T10:00:00', BENCH, [[60, 10]]), week: 1, weekday: 1 }

  it('todaysProgramDay: the first day of the week not logged yet, so a missed one comes later', () => {
    expect(todaysProgramDay(P, START, [], new Date(2026, 9, 7, 18))).toEqual({ week: 1, weekday: 1 })
    expect(todaysProgramDay(P, START, [doneMonday], THU)).toEqual({ week: 1, weekday: 3 }) // Wednesday on Thursday
    const doneToday = { ...doneMonday, startedAt: new Date(2026, 9, 8, 9).toISOString() }
    expect(todaysProgramDay(P, START, [doneToday], THU)).toBeNull()
    expect(todaysProgramDay(P, '2026-10-12', [], THU)).toBeNull() // not started
    expect(todaysProgramDay(P, START, [], new Date(2026, 9, 21))).toBeNull() // finished
  })

  it('Wednesday trained on Thursday gets the 85 set on Thursday, in the preview and in the workout', () => {
    const thu85 = [{ exercise: BENCH, weightKg: 85, date: THU_KEY }]
    const pick = todaysProgramDay(P, START, [doneMonday], THU)!
    expect(pick).toEqual({ week: 1, weekday: DAY.weekday })
    const built = applyPlan(DAY, null, HISTORY, THU_KEY, pick.week, B90, thu85)
    expect(built.exercises[0].weight).toBe(85)
    // A prepared (not started) Wednesday refilled on Thursday gets it too.
    const res = applyPlan(DAY, null, HISTORY, THU_KEY, 1, B90)
    const w: Workout = {
      id: 'a',
      programId: 'p',
      week: 1,
      weekday: DAY.weekday,
      startedAt: new Date(2026, 9, 7, 8).toISOString(), // prepared on Wednesday morning
      finishedAt: null,
      exercises: res.exercises.map((a) => ({
        name: a.exercise.name,
        target: a.target,
        dropset: false,
        sets: [{ weight: a.weight, reps: null, done: false }],
      })),
    }
    const overrides = overridesForWorkout(thu85, w, THU)
    expect(overrides).toBe(thu85)
    const r = refillSuggestions(w, suggestionsOf(res.exercises), DAY, HISTORY, B90, overrides, THU_KEY)!
    expect(r.workout.exercises[0].sets[0].weight).toBe(85)
  })

  it('overridesForWorkout: a session started today counts, one started yesterday does not', () => {
    const base = { exercises: [{ name: BENCH, target: '', dropset: false, sets: [{ weight: 80, reps: 8, done: true }] }] }
    expect(overridesForWorkout(O85, { ...base, startedAt: new Date(2026, 9, 8, 10).toISOString() }, THU)).toBe(O85)
    expect(overridesForWorkout(O85, { ...base, startedAt: new Date(2026, 9, 7, 23, 30).toISOString() }, THU)).toEqual([])
  })
})

describe('applyPlan with overrides', () => {
  it('the plain day takes the override', () => {
    const r = applyPlan(DAY, null, HISTORY, TODAY, undefined, B90, O85)
    expect(r.exercises[0].weight).toBe(85)
    expect(r.exercises[0].override).toEqual(O85[0])
    expect(r.exercises[1].override).toBeNull()
  })

  it('an override for another date is ignored', () => {
    const r = applyPlan(DAY, null, HISTORY, TODAY, undefined, B90, [{ exercise: BENCH, weightKg: 85, date: YESTERDAY }])
    expect(r.exercises[0].weight).toBe(67.5)
    expect(r.exercises[0].override).toBeNull()
  })

  it('the plan weight factor is not applied to the override and not shown', () => {
    const lighter = plan([pe(BENCH, { weightFactor: 0.9 })])
    expect(applyPlan(DAY, lighter, HISTORY, TODAY, undefined, B90).exercises[0].weight).toBe(scaleWeight(67.5, 0.9, 2.5))
    const a = applyPlan(DAY, lighter, HISTORY, TODAY, undefined, B90, O85).exercises[0]
    expect(a.weight).toBe(85)
    expect(a.factor).toBe(0.9) // kept for the refill snapshot
    expect(a.changed).toBe(false)
    expect(planNote(a)).toBeNull()
    const withReason = plan([pe(BENCH, { weightFactor: 0.9, reason: 'мало сна' })])
    expect(planNote(applyPlan(DAY, withReason, HISTORY, TODAY, undefined, B90, O85).exercises[0])).toBe('мало сна')
  })

  it('looks the override up by the name trained: a replacement does not inherit it', () => {
    const swap = plan([pe(BENCH, { replaceWith: PRESS })])
    expect(applyPlan(DAY, swap, [], TODAY, undefined, [], O85).exercises[0].weight).toBeNull()
    const forPress = [{ exercise: PRESS, weightKg: 24, date: TODAY }]
    expect(applyPlan(DAY, swap, [], TODAY, undefined, [], forPress).exercises[0].weight).toBe(24)
  })
})

describe('refillSuggestions with overrides', () => {
  /** The prepared workout as store.startWorkout / applyDayPlan build it. */
  function prepared(p: DayPlan | null, baselines: Baseline[], overrides: WeightOverride[] = []) {
    const res = applyPlan(DAY, p, HISTORY, TODAY, undefined, baselines, overrides)
    const exercises: ExerciseLog[] = res.exercises.map((a) => ({
      name: a.exercise.name,
      target: a.target,
      dropset: false,
      sets: Array.from({ length: a.exercise.prescription.sets }, () => ({ weight: a.weight, reps: null, done: false })),
    }))
    const w: Workout = { id: 'a', programId: 'p', week: 1, weekday: 3, startedAt: TODAY, finishedAt: null, exercises }
    return { w, suggested: suggestionsOf(res.exercises) }
  }

  const weights = (w: Workout, i = 0) => w.exercises[i].sets.map((s) => s.weight)
  const refill = (w: Workout, s: ReturnType<typeof prepared>['suggested'], o: WeightOverride[], b: Baseline[] = []) =>
    refillSuggestions(w, s, DAY, HISTORY, b, o, TODAY)

  function patchSet(w: Workout, i: number, j: number, patch: Partial<Workout['exercises'][number]['sets'][number]>): Workout {
    return {
      ...w,
      exercises: w.exercises.map((e, k) =>
        k === i ? { ...e, sets: e.sets.map((s, n) => (n === j ? { ...s, ...patch } : s)) } : e,
      ),
    }
  }

  it('prepared workout: replaces suggested and empty weights, keeps the user edit; then idempotent', () => {
    const { w, suggested } = prepared(null, [])
    expect(weights(w)).toEqual([67.5, 67.5, 67.5, 67.5])
    const edited = patchSet(patchSet(w, 0, 1, { weight: 70 }), 0, 3, { weight: null })
    const r = refill(edited, suggested, O85)!
    expect(weights(r.workout)).toEqual([85, 70, 85, 85])
    expect(r.suggested[BENCH]).toMatchObject({ weight: 85, overrideDate: TODAY })
    expect(weights(r.workout, 1)).toEqual(weights(w, 1)) // no override for curls
    expect(refill(r.workout, r.suggested, O85)).toBeNull()
  })

  it('does not apply the plan factor the workout was built with', () => {
    const { w, suggested } = prepared(plan([pe(BENCH, { weightFactor: 0.9 })]), [])
    expect(weights(w)).toEqual([60, 60, 60, 60])
    const r = refill(w, suggested, O85)!
    expect(weights(r.workout)).toEqual([85, 85, 85, 85])
    // The override gone: back to the computed weight with the factor.
    expect(weights(refill(r.workout, r.suggested, [])!.workout)).toEqual([60, 60, 60, 60])
  })

  it('mid-workout: the remaining sets get the override, done and edited sets stay', () => {
    const { w, suggested } = prepared(null, [])
    // Set 1 done at 80 (typed before ticking), set 2 typed 72.5 by hand, sets 3-4 still the suggestion.
    const started = patchSet(patchSet(w, 0, 0, { weight: 80, reps: 10, done: true }), 0, 1, { weight: 72.5 })
    const r = refill(started, suggested, O85)!
    expect(r.workout.exercises[0].sets).toEqual([
      { weight: 80, reps: 10, done: true },
      { weight: 72.5, reps: null, done: false },
      { weight: 85, reps: null, done: false },
      { weight: 85, reps: null, done: false },
    ])
    expect(refill(r.workout, r.suggested, O85)).toBeNull()
  })

  it('mid-workout: a set done at the suggested weight is not changed', () => {
    const { w, suggested } = prepared(null, [])
    const started = patchSet(w, 0, 0, { reps: 10, done: true })
    const r = refill(started, suggested, O85)!
    expect(weights(r.workout)).toEqual([67.5, 85, 85, 85])
    expect(r.workout.exercises[0].sets[0].done).toBe(true)
  })

  it('mid-workout: all remaining sets edited by hand -> weights unchanged', () => {
    const { w, suggested } = prepared(null, [])
    let started = patchSet(w, 0, 0, { weight: 80, reps: 10, done: true })
    for (const j of [1, 2, 3]) started = patchSet(started, 0, j, { weight: 82.5 })
    const r = refill(started, suggested, O85)!
    expect(weights(r.workout)).toEqual([80, 82.5, 82.5, 82.5])
    // Only the snapshot moves, so a later change of the override is compared with 85.
    expect(r.suggested[BENCH]).toMatchObject({ weight: 85, overrideDate: TODAY })
    expect(refill(r.workout, r.suggested, O85)).toBeNull()
  })

  it('mid-workout: an override removed in the chat brings the remaining sets back', () => {
    const { w, suggested } = prepared(null, [], O85)
    expect(weights(w)).toEqual([85, 85, 85, 85])
    const started = patchSet(w, 0, 0, { reps: 8, done: true })
    const r = refill(started, suggested, [])!
    expect(weights(r.workout)).toEqual([85, 67.5, 67.5, 67.5])
    expect(r.suggested[BENCH]).toMatchObject({ weight: 67.5, overrideDate: null })
  })

  it('mid-workout past midnight: yesterday\'s override (no longer sent) is kept in the remaining sets', () => {
    const { w, suggested } = prepared(null, [], O85)
    const started = patchSet(w, 0, 0, { reps: 8, done: true })
    const tomorrow = '2026-10-08'
    expect(refillSuggestions(started, suggested, DAY, HISTORY, [], [], tomorrow)).toBeNull()
    // Not started yet (prepared yesterday, trained today): yesterday's number no longer counts.
    expect(weights(refillSuggestions(w, suggested, DAY, HISTORY, [], [], tomorrow)!.workout)).toEqual([67.5, 67.5, 67.5, 67.5])
  })

  it('a started workout still ignores baseline changes without an override', () => {
    const { w, suggested } = prepared(null, [])
    const started = patchSet(w, 0, 0, { reps: 10, done: true })
    const curlBaseline = [{ exercise: CURL, weightKg: 20, reps: 8, factId: 3 }]
    expect(refillSuggestions(started, suggested, DAY, HISTORY, curlBaseline, [], TODAY)).toBeNull()
    // Not started, the same baseline fills the curls (no history for them).
    expect(refillSuggestions(w, suggested, DAY, HISTORY, curlBaseline, [], TODAY)).not.toBeNull()
  })

  it('override for an exercise not in the workout, or for another date, changes nothing', () => {
    const { w, suggested } = prepared(null, [])
    expect(refill(w, suggested, [{ exercise: 'присед', weightKg: 140, date: TODAY }])).toBeNull()
    expect(refill(w, suggested, [{ exercise: BENCH, weightKg: 85, date: YESTERDAY }])).toBeNull()
  })

  it('folds ё/е in the override name', () => {
    const { w, suggested } = prepared(null, [])
    const r = refill(w, suggested, [{ exercise: 'Жим лежа', weightKg: 85, date: TODAY }])!
    expect(weights(r.workout)).toEqual([85, 85, 85, 85])
  })

  it('override and baseline on an exercise without history do not fight across syncs', () => {
    const day: ProgramDay = { ...DAY }
    const { w, suggested } = (() => {
      const res = applyPlan(day, null, [], TODAY, undefined, B90, O85)
      const exercises: ExerciseLog[] = res.exercises.map((a) => ({
        name: a.exercise.name,
        target: a.target,
        dropset: false,
        sets: [{ weight: a.weight, reps: null, done: false }],
      }))
      return {
        w: { id: 'b', programId: 'p', week: 1, weekday: 3, startedAt: TODAY, finishedAt: null, exercises } as Workout,
        suggested: suggestionsOf(res.exercises),
      }
    })()
    expect(weights(w)).toEqual([85])
    expect(refillSuggestions(w, suggested, day, [], B90, O85, TODAY)).toBeNull()
    const B100 = [{ exercise: BENCH, weightKg: 100, reps: 8, factId: 2 }]
    expect(refillSuggestions(w, suggested, day, [], B100, O85, TODAY)).toBeNull()
    // Override gone: the baseline weight comes back.
    expect(weights(refillSuggestions(w, suggested, day, [], B90, [], TODAY)!.workout)).toEqual([82.5])
  })
})
