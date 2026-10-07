import { describe, expect, it } from 'vitest'
import type { DayPlan, DayPlanExercise } from './api'
import {
  applyPlan,
  planFitsDay,
  planKey,
  planNote,
  planTitle,
  refillFromBaselines,
  safeFactor,
  scaleWeight,
  suggestionsOf,
  type PreparedSuggestions,
} from './plan'
import { formatPrescription, type Intensity, type Prescription, type ProgramDay, type ProgramExercise } from './program'
import { roundToStep, suggestWeight } from './progression'
import type { Baseline, ExerciseLog, Workout } from './store'

const TODAY = '2026-10-07'

/** Workout with one exercise; sets are [weight, reps] pairs, all done. */
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

const RANGE: Prescription = { sets: 4, reps_min: 8, reps_max: 12, drop_reps: null, raw: '4х8-12' }
const DROPSET: Prescription = { sets: 3, reps_min: null, reps_max: null, drop_reps: [12, 6, 6], raw: 'дропсет 3х12-6-6' }

function ex(name: string, order: number, intensity: Intensity | null = null, p: Prescription = RANGE): ProgramExercise {
  return { name, intensity, prescription: { ...p }, order }
}

function day(...exercises: ProgramExercise[]): ProgramDay {
  return { weekday: 3, title: 'День', exercises }
}

function pe(name: string, over: Partial<DayPlanExercise> = {}): DayPlanExercise {
  return { name, sets: null, repsMin: null, repsMax: null, weightFactor: null, skip: false, replaceWith: null, reason: null, ...over }
}

function plan(exercises: DayPlanExercise[], over: Partial<DayPlan> = {}): DayPlan {
  return { date: TODAY, adjusted: true, readiness: 'normal', summary: null, exercises, ...over }
}

const BENCH = 'жим лёжа'
const CURL = 'сгибания с гантелями'
const PRESS = 'жим гантелей сидя'
const DAY = day(ex(BENCH, 1, 'heavy'), ex(CURL, 2, 'medium'), ex('французский жим', 3, 'light'))

describe('planFitsDay', () => {
  it('is false without a day', () => {
    expect(planFitsDay(undefined, plan([pe(BENCH)]), TODAY)).toBe(false)
  })

  it('is false for a null or undefined plan', () => {
    expect(planFitsDay(DAY, null, TODAY)).toBe(false)
    expect(planFitsDay(DAY, undefined, TODAY)).toBe(false)
  })

  it('is false when the plan is not adjusted', () => {
    expect(planFitsDay(DAY, plan([pe(BENCH)], { adjusted: false }), TODAY)).toBe(false)
  })

  it('is false for another date', () => {
    expect(planFitsDay(DAY, plan([pe(BENCH)], { date: '2026-10-06' }), TODAY)).toBe(false)
  })

  it('is false for an empty exercise list', () => {
    expect(planFitsDay(DAY, plan([]), TODAY)).toBe(false)
  })

  it('is false when a plan name is not in the day', () => {
    expect(planFitsDay(DAY, plan([pe(BENCH), pe('присед')]), TODAY)).toBe(false)
  })

  it('is true when every plan name is in the day', () => {
    expect(planFitsDay(DAY, plan([pe(BENCH), pe(CURL)]), TODAY)).toBe(true)
  })

  it('checks week and weekday when the server sends them', () => {
    const p = plan([pe(BENCH)], { week: 4, weekday: 3 })
    expect(planFitsDay(DAY, p, TODAY, 4)).toBe(true)
    expect(planFitsDay(DAY, p, TODAY, 5)).toBe(false) // same exercises, another week (DayPreview chips)
    expect(planFitsDay({ ...DAY, weekday: 5 }, p, TODAY, 4)).toBe(false) // same exercises, another day
  })

  it('falls back to the name check for answers without week and weekday', () => {
    expect(planFitsDay(DAY, plan([pe(BENCH)]), TODAY, 5)).toBe(true)
    expect(planFitsDay({ ...DAY, weekday: 5 }, plan([pe(BENCH)]), TODAY)).toBe(true)
  })

  it('applyPlan leaves another week as written', () => {
    const p = plan([pe(BENCH, { sets: 2 })], { week: 4, weekday: 3 })
    expect(applyPlan(DAY, p, [], TODAY, 4).applied).toBe(true)
    const other = applyPlan(DAY, p, [], TODAY, 5)
    expect(other.applied).toBe(false)
    expect(other.exercises[0].exercise.prescription.sets).toBe(4)
  })
})

describe('planKey', () => {
  it("is 'program' for null", () => {
    expect(planKey(null)).toBe('program')
  })

  it('is equal for two different objects with the same content', () => {
    const a = plan([pe(BENCH, { sets: 3, weightFactor: 0.9 })])
    const b = plan([pe(BENCH, { sets: 3, weightFactor: 0.9 })])
    expect(a).not.toBe(b)
    expect(planKey(a)).toBe(planKey(b))
  })

  it('changes with weightFactor', () => {
    expect(planKey(plan([pe(BENCH, { weightFactor: 0.9 })]))).not.toBe(planKey(plan([pe(BENCH, { weightFactor: 0.8 })])))
  })

  it('changes with skip', () => {
    expect(planKey(plan([pe(BENCH)]))).not.toBe(planKey(plan([pe(BENCH, { skip: true })])))
  })

  it('changes with replaceWith', () => {
    expect(planKey(plan([pe(BENCH)]))).not.toBe(planKey(plan([pe(BENCH, { replaceWith: PRESS })])))
  })

  it('does not depend on summary or reason', () => {
    const a = plan([pe(BENCH, { reason: 'a' })], { summary: 'x' })
    const b = plan([pe(BENCH, { reason: 'b' })], { summary: 'y' })
    expect(planKey(a)).toBe(planKey(b))
  })
})

describe('safeFactor', () => {
  it.each([null, undefined, NaN, Infinity, -Infinity, 0, 0.2, 1.6])('treats %s as no change', (f) => {
    expect(safeFactor(f)).toBe(1)
  })

  it('keeps a sane factor', () => {
    expect(safeFactor(0.9)).toBe(0.9)
  })

  it('accepts the bounds 0.3 and 1.5', () => {
    expect(safeFactor(0.3)).toBe(0.3)
    expect(safeFactor(1.5)).toBe(1.5)
  })
})

describe('scaleWeight', () => {
  it('passes null through', () => {
    expect(scaleWeight(null, 0.9, 2.5)).toBeNull()
  })

  it('keeps the base as is for factor 1, even off-step', () => {
    expect(scaleWeight(13.5, 1, 1)).toBe(13.5)
  })

  it('rounds to the nearest step: 67.5 x 0.9 = 60.75 -> 60, 60 x 0.9 = 54 -> 55, 20 x 0.85 = 17 -> 17.5', () => {
    expect(scaleWeight(67.5, 0.9, 2.5)).toBe(60)
    expect(scaleWeight(60, 0.9, 2.5)).toBe(55)
    expect(scaleWeight(20, 0.85, 2.5)).toBe(17.5)
  })

  it('never goes below one step: 2.5 x 0.9 stays 2.5', () => {
    expect(scaleWeight(2.5, 0.9, 2.5)).toBe(2.5)
    expect(scaleWeight(2.5, 0.5, 2.5)).toBe(2.5)
    expect(scaleWeight(5, 0.3, 2.5)).toBe(2.5)
  })

  it('does not step down by much more than asked: 5 x 0.9 stays 5 instead of 2.5 (-50 %)', () => {
    expect(scaleWeight(5, 0.9, 2.5)).toBe(5)
    expect(scaleWeight(5, 0.75, 2.5)).toBe(2.5)
  })

  it('does not round back to the base: 10 x 0.95 on step 1 -> 9', () => {
    expect(scaleWeight(10, 0.95, 1)).toBe(9)
  })

  it('rounds a heavier weight to the nearest step too: 60 x 1.1 = 66 -> 65', () => {
    expect(scaleWeight(60, 1.1, 2.5)).toBe(65)
  })

  it('steps up from the base only when the step is at most twice the asked increase', () => {
    expect(scaleWeight(10, 1.05, 1)).toBe(11)
    expect(scaleWeight(20, 1.05, 2.5)).toBe(20)
  })

  it('returns a value on the step without float noise', () => {
    for (const step of [1, 2.5]) {
      for (const base of [10, 12.5, 17.5, 22.5, 37.5, 60, 62.5, 67.5, 82.5, 100]) {
        for (const factor of [0.5, 0.8, 0.85, 0.9, 0.95, 1.05, 1.1, 1.15]) {
          const r = scaleWeight(base, factor, step)!
          expect(r).toBe(roundToStep(r, step))
          expect(Math.abs(r / step - Math.round(r / step))).toBeLessThan(1e-9)
        }
      }
    }
  })

  it('keeps a base below one step as is', () => {
    expect(scaleWeight(1, 0.3, 2.5)).toBe(1)
  })

  it('never moves against the factor', () => {
    for (const step of [1, 2.5]) {
      for (const base of [2.5, 5, 7.5, 10, 13.5, 20, 60]) {
        for (const factor of [0.5, 0.85, 0.9, 0.95]) expect(scaleWeight(base, factor, step)!).toBeLessThanOrEqual(base)
        for (const factor of [1.05, 1.1]) expect(scaleWeight(base, factor, step)!).toBeGreaterThanOrEqual(base)
      }
    }
  })
})

describe('applyPlan', () => {
  const history = [workout('2026-10-01T10:00:00Z', BENCH, [[67.5, 10], [67.5, 10], [67.5, 10]])]

  function expectPlain(r: ReturnType<typeof applyPlan>, d: ProgramDay) {
    expect(r.applied).toBe(false)
    expect(r.skipped).toEqual([])
    expect(r.exercises.map((a) => a.exercise.name)).toEqual(d.exercises.map((e) => e.name))
    r.exercises.forEach((a, i) => {
      const e = d.exercises[i]
      expect(a.changed).toBe(false)
      expect(a.replaced).toBe(false)
      expect(a.factor).toBe(1)
      expect(a.target).toBe(formatPrescription(e.prescription))
      expect(a.weight).toBe(suggestWeight(history, e)?.weight ?? null)
    })
  }

  it('returns the plain day without a plan', () => {
    expectPlain(applyPlan(DAY, null, history, TODAY), DAY)
  })

  it('returns the plain day for a plan that does not fit', () => {
    expectPlain(applyPlan(DAY, plan([pe(BENCH, { sets: 2 })], { date: '2026-10-06' }), history, TODAY), DAY)
    expectPlain(applyPlan(DAY, plan([pe(BENCH, { sets: 2 })], { adjusted: false }), history, TODAY), DAY)
    expectPlain(applyPlan(DAY, plan([pe('присед', { sets: 2 })]), history, TODAY), DAY)
  })

  it('suggests a weight from history in the plain day', () => {
    const r = applyPlan(DAY, null, history, TODAY)
    expect(r.exercises[0].weight).toBe(67.5)
    expect(r.exercises[1].weight).toBeNull()
  })

  it('applies sets and reps to the prescription and marks the program values', () => {
    const r = applyPlan(DAY, plan([pe(BENCH, { sets: 3, repsMin: 6, repsMax: 10 })]), history, TODAY)
    expect(r.applied).toBe(true)
    const a = r.exercises[0]
    expect(a.target).toBe('3 × 6–10 (по плану 4 × 8–12)')
    expect(a.exercise.prescription.raw).toBe(a.target)
    expect(a.exercise.prescription).toMatchObject({ sets: 3, reps_min: 6, reps_max: 10 })
    expect(a.original.prescription).toEqual(RANGE)
    expect(a.changed).toBe(true)
  })

  it('changes only the sets', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { sets: 3 })]), history, TODAY).exercises[0]
    expect(a.target).toBe('3 × 8–12 (по плану 4 × 8–12)')
    expect(a.exercise.prescription).toMatchObject({ sets: 3, reps_min: 8, reps_max: 12 })
  })

  it('rounds fractional sets', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { sets: 2.6 })]), history, TODAY).exercises[0]
    expect(a.exercise.prescription.sets).toBe(3)
  })

  it('has no "по плану" part and is unchanged when the numbers match the program', () => {
    const r = applyPlan(DAY, plan([pe(BENCH, { sets: 4, repsMin: 8, repsMax: 12 })]), history, TODAY)
    expect(r.applied).toBe(true)
    const a = r.exercises[0]
    expect(a.target).toBe('4 × 8–12')
    expect(a.target).not.toContain('по плану')
    expect(a.changed).toBe(false)
    expect(a.exercise.prescription.raw).toBe('4 × 8–12')
  })

  it('keeps drop_reps of a dropset and changes only the sets', () => {
    const d = day(ex('сгибания', 1, 'medium', DROPSET), ex(BENCH, 2))
    const a = applyPlan(d, plan([pe('сгибания', { sets: 2, repsMin: 5, repsMax: 7 })]), [], TODAY).exercises[0]
    expect(a.exercise.prescription.drop_reps).toEqual([12, 6, 6])
    expect(a.exercise.prescription.sets).toBe(2)
    expect(a.exercise.prescription.reps_min).toBeNull()
    expect(a.exercise.prescription.reps_max).toBeNull()
    expect(a.target).toBe('2 × дропсет 12-6-6 (по плану 3 × дропсет 12-6-6)')
    expect(a.changed).toBe(true)
  })

  it('ignores sets below 1 and null sets', () => {
    for (const sets of [0, -2, null]) {
      const a = applyPlan(DAY, plan([pe(BENCH, { sets })]), history, TODAY).exercises[0]
      expect(a.exercise.prescription.sets).toBe(4)
      expect(a.changed).toBe(false)
    }
  })

  it('lifts repsMax to repsMin when repsMax < repsMin', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { repsMin: 10, repsMax: 6 })]), history, TODAY).exercises[0]
    expect(a.exercise.prescription).toMatchObject({ reps_min: 10, reps_max: 10 })
    expect(a.target).toBe('4 × 10 (по плану 4 × 8–12)')
  })

  it('skips an exercise, keeps the order of the rest and records the reason', () => {
    const r = applyPlan(DAY, plan([pe(BENCH, { skip: true, reason: '  болит плечо ' }), pe(CURL)]), history, TODAY)
    expect(r.applied).toBe(true)
    expect(r.exercises.map((a) => a.exercise.name)).toEqual([CURL, 'французский жим'])
    expect(r.skipped).toEqual([{ name: BENCH, reason: 'болит плечо' }])
  })

  it('skipped reason is null when missing or blank', () => {
    const r = applyPlan(DAY, plan([pe(BENCH, { skip: true }), pe(CURL, { skip: true, reason: '  ' })]), history, TODAY)
    expect(r.skipped).toEqual([
      { name: BENCH, reason: null },
      { name: CURL, reason: null },
    ])
  })

  it('falls back to the full unchanged day when everything is skipped', () => {
    const all = plan(DAY.exercises.map((e) => pe(e.name, { skip: true, reason: 'устал' })))
    expectPlain(applyPlan(DAY, all, history, TODAY), DAY)
  })

  it('replaces the exercise: normalized name, original kept, intensity and order preserved', () => {
    const r = applyPlan(DAY, plan([pe(BENCH, { replaceWith: `  ${PRESS.toUpperCase()} ` })]), [], TODAY)
    const a = r.exercises[0]
    expect(a.exercise.name).toBe(PRESS)
    expect(a.original.name).toBe(BENCH)
    expect(a.replaced).toBe(true)
    expect(a.changed).toBe(true)
    expect(a.exercise.intensity).toBe('heavy')
    expect(a.exercise.order).toBe(1)
  })

  it('does not count a replacement with the same name as replaced', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { replaceWith: ' Жим Лёжа ' })]), history, TODAY).exercises[0]
    expect(a.replaced).toBe(false)
    expect(a.exercise.name).toBe(BENCH)
    expect(a.changed).toBe(false)
  })

  it('takes the weight of a replacement from its own history', () => {
    const h = [...history, workout('2026-10-02T10:00:00Z', PRESS, [[30, 10], [30, 10], [30, 10]])]
    const a = applyPlan(DAY, plan([pe(BENCH, { replaceWith: PRESS })]), h, TODAY).exercises[0]
    expect(a.weight).toBe(30)
    expect(a.baseWeight).toBe(30)
  })

  it('has a null weight when the replacement has no history', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { replaceWith: PRESS })]), history, TODAY).exercises[0]
    expect(a.weight).toBeNull()
    expect(a.baseWeight).toBeNull()
  })

  it('scales the weight by weightFactor on the equipment step', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { weightFactor: 0.9 })]), history, TODAY).exercises[0]
    expect(a.baseWeight).toBe(67.5)
    expect(a.weight).toBe(scaleWeight(67.5, 0.9, 2.5))
    expect(a.weight).toBe(60)
    expect(a.factor).toBe(0.9)
    expect(a.changed).toBe(true)
  })

  it('uses a 1 kg step for dumbbell exercises', () => {
    const h = [workout('2026-10-01T10:00:00Z', CURL, [[15, 10], [15, 10]])]
    const a = applyPlan(DAY, plan([pe(CURL, { weightFactor: 0.9 })]), h, TODAY).exercises[1]
    expect(a.baseWeight).toBe(15)
    expect(a.weight).toBe(13)
  })

  it('ignores an insane weightFactor', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { weightFactor: 5 })]), history, TODAY).exercises[0]
    expect(a.factor).toBe(1)
    expect(a.weight).toBe(67.5)
    expect(a.changed).toBe(false)
  })

  it('leaves day exercises without a plan entry as they are', () => {
    const r = applyPlan(DAY, plan([pe(BENCH, { sets: 3 })]), history, TODAY)
    expect(r.exercises.map((a) => a.exercise.name)).toEqual(DAY.exercises.map((e) => e.name))
    const rest = r.exercises[1]
    expect(rest.original).toBe(DAY.exercises[1])
    expect(rest.exercise).toMatchObject({ name: CURL, intensity: 'medium', order: 2 })
    expect(rest.exercise.prescription).toMatchObject({ sets: 4, reps_min: 8, reps_max: 12 })
    expect(rest.changed).toBe(false)
    expect(rest.target).toBe('4 × 8–12')
    expect(r.exercises[2].reason).toBeNull()
  })
})

describe('applyPlan with baselines', () => {
  const BASE: Baseline[] = [
    { exercise: BENCH, weightKg: 90, reps: 8, factId: 1 },
    { exercise: PRESS, weightKg: 30, reps: null, factId: 2 },
  ]

  it('takes the weight from baselines in the plain day', () => {
    const r = applyPlan(DAY, null, [], TODAY, undefined, BASE)
    // 90 × 8 -> 1RM 114, heavy for 12 reps -> 82.5
    expect(r.exercises[0].weight).toBe(82.5)
    expect(r.exercises[0].baseWeight).toBe(82.5)
    expect(r.exercises[1].weight).toBeNull()
  })

  it('without baselines (older server) the day has no weights', () => {
    expect(applyPlan(DAY, null, [], TODAY).exercises[0].weight).toBeNull()
  })

  it('applies the plan weightFactor on top of the baseline weight', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { weightFactor: 0.9 })]), [], TODAY, undefined, BASE).exercises[0]
    expect(a.baseWeight).toBe(82.5)
    expect(a.weight).toBe(scaleWeight(82.5, 0.9, 2.5))
    expect(a.weight).toBe(75)
    expect(a.factor).toBe(0.9)
  })

  it('a replacement takes its own baseline', () => {
    const a = applyPlan(DAY, plan([pe(BENCH, { replaceWith: PRESS })]), [], TODAY, undefined, BASE).exercises[0]
    // ~30 kg without reps counts as the max: heavy for 12 reps 30 / 1.4 = 21.4 -> 21 on 1 kg dumbbell steps
    expect(a.baseWeight).toBe(21)
    expect(a.weight).toBe(21)
  })

  it('history wins over the baseline', () => {
    const h = [workout('2026-10-01T10:00:00Z', BENCH, [[67.5, 10], [67.5, 10], [67.5, 10]])]
    expect(applyPlan(DAY, null, h, TODAY, undefined, BASE).exercises[0].weight).toBe(67.5)
  })
})

describe('refillFromBaselines', () => {
  const B90 = [{ exercise: BENCH, weightKg: 90, reps: 8, factId: 1 }] // heavy 12 reps -> 82.5
  const B100 = [{ exercise: BENCH, weightKg: 100, reps: 8, factId: 2 }] // 126.7 / 1.4 = 90.5 -> 90

  /** The prepared workout as store.startWorkout / applyDayPlan build it. */
  function prepared(p: DayPlan | null, baselines: Baseline[], history: Workout[] = []) {
    const res = applyPlan(DAY, p, history, TODAY, undefined, baselines)
    const exercises: ExerciseLog[] = res.exercises.map((a) => ({
      name: a.exercise.name,
      target: a.target,
      dropset: false,
      sets: Array.from({ length: a.exercise.prescription.sets }, () => ({ weight: a.weight, reps: null, done: false })),
    }))
    const w: Workout = { id: 'a', programId: 'p', week: 1, weekday: 3, startedAt: TODAY, finishedAt: null, exercises }
    return { w, suggested: suggestionsOf(res.exercises) }
  }

  const weights = (w: Workout, i: number) => w.exercises[i].sets.map((s) => s.weight)

  function setWeight(w: Workout, i: number, j: number, weight: number | null): Workout {
    return {
      ...w,
      exercises: w.exercises.map((e, k) =>
        k === i ? { ...e, sets: e.sets.map((s, n) => (n === j ? { ...s, weight } : s)) } : e,
      ),
    }
  }

  it('first load: built without baselines, the new list fills the empty weights only', () => {
    const { w, suggested } = prepared(null, [])
    expect(weights(w, 0)).toEqual([null, null, null, null])
    const edited = setWeight(w, 0, 0, 60)
    const r = refillFromBaselines(edited, suggested, DAY, [], B90)
    expect(weights(r!.workout, 0)).toEqual([60, 82.5, 82.5, 82.5])
    expect(r!.suggested[BENCH].weight).toBe(82.5)
    // Other exercises have no baseline: still empty.
    expect(weights(r!.workout, 1)).toEqual([null, null, null, null])
  })

  it('updates weights still equal to the old suggestion and keeps the edited one', () => {
    const { w, suggested } = prepared(null, B90)
    expect(weights(w, 0)).toEqual([82.5, 82.5, 82.5, 82.5])
    const edited = setWeight(w, 0, 1, 70)
    const r = refillFromBaselines(edited, suggested, DAY, [], B100)
    expect(weights(r!.workout, 0)).toEqual([90, 70, 90, 90])
  })

  it('a baseline for an exercise not in the workout changes nothing', () => {
    const { w, suggested } = prepared(null, B90)
    const more = [...B90, { exercise: 'присед', weightKg: 140, reps: 1, factId: 5 }]
    expect(refillFromBaselines(w, suggested, DAY, [], more)).toBeNull()
  })

  it('never touches a started workout', () => {
    const { w, suggested } = prepared(null, [])
    const started: Workout = {
      ...w,
      exercises: w.exercises.map((e, i) => (i === 2 ? { ...e, sets: [{ weight: 20, reps: 10, done: true }, ...e.sets.slice(1)] } : e)),
    }
    expect(refillFromBaselines(started, suggested, DAY, [], B90)).toBeNull()
  })

  it('leaves exercises with history alone, even with a baseline', () => {
    const h = [workout('2026-10-01T10:00:00Z', BENCH, [[50, 10]])]
    const { w, suggested } = prepared(null, [], h)
    const edited = setWeight(w, 0, 0, null)
    expect(refillFromBaselines(edited, suggested, DAY, h, B90)).toBeNull()
  })

  it('keeps added and removed exercises; a user-added one is not filled', () => {
    const { w, suggested } = prepared(null, [])
    const custom: ExerciseLog = { name: 'присед', target: '', dropset: false, sets: [{ weight: null, reps: null, done: false }] }
    const changed: Workout = { ...w, exercises: [w.exercises[0], custom] } // CURL and the French press removed
    const r = refillFromBaselines(changed, suggested, DAY, [], [...B90, { exercise: 'присед', weightKg: 140, reps: 1, factId: 5 }])
    expect(r!.workout.exercises.map((e) => e.name)).toEqual([BENCH, 'присед'])
    expect(weights(r!.workout, 0)).toEqual([82.5, 82.5, 82.5, 82.5])
    expect(weights(r!.workout, 1)).toEqual([null])
  })

  it('applies the plan factor the workout was built with', () => {
    const { w, suggested } = prepared(plan([pe(BENCH, { weightFactor: 0.9 })]), [])
    const r = refillFromBaselines(w, suggested, DAY, [], B90)
    expect(weights(r!.workout, 0)).toEqual([75, 75, 75, 75]) // 82.5 × 0.9 on 2.5 kg steps
  })

  it('a deactivated fact clears the weights it had suggested, not the edited ones', () => {
    const { w, suggested } = prepared(null, B90)
    const edited = setWeight(w, 0, 3, 80)
    const r = refillFromBaselines(edited, suggested, DAY, [], [])
    expect(weights(r!.workout, 0)).toEqual([null, null, null, 80])
  })

  it('without a snapshot (prepared by an older version) fills empty weights from the day', () => {
    const { w } = prepared(null, [])
    const r = refillFromBaselines(setWeight(w, 0, 2, 50), {} satisfies PreparedSuggestions, DAY, [], B90)
    expect(weights(r!.workout, 0)).toEqual([82.5, 82.5, 50, 82.5])
  })

  it('is idempotent: a second sync with the same list changes nothing', () => {
    const { w, suggested } = prepared(null, [])
    const r = refillFromBaselines(w, suggested, DAY, [], B90)!
    expect(refillFromBaselines(r.workout, r.suggested, DAY, [], B90)).toBeNull()
  })
})

describe('planNote', () => {
  const day1 = day(ex(BENCH, 1, 'heavy'))
  const h = [workout('2026-10-01T10:00:00Z', BENCH, [[67.5, 10]])]
  const first = (p: DayPlanExercise) => applyPlan(day1, plan([p]), h, TODAY).exercises[0]

  it('is null for an unchanged exercise without a reason', () => {
    expect(planNote(first(pe(BENCH)))).toBeNull()
  })

  it('is just the reason when nothing changed', () => {
    expect(planNote(first(pe(BENCH, { reason: 'мало сна' })))).toBe('мало сна')
  })

  it('joins reason, weight factor and replacement', () => {
    const a = first(pe(BENCH, { replaceWith: PRESS, weightFactor: 0.9, reason: 'мало сна' }))
    expect(planNote(a)).toBe('мало сна · вес −10 % · вместо «жим лёжа»')
  })

  it('shows a heavier factor with a plus sign', () => {
    expect(planNote(first(pe(BENCH, { weightFactor: 1.05 })))).toBe('вес +5 %')
  })

  it('shows only the sets change as null when there is no reason', () => {
    expect(planNote(first(pe(BENCH, { sets: 3 })))).toBeNull()
  })
})

describe('planTitle', () => {
  it('uses the rest wording for rest days', () => {
    expect(planTitle({ readiness: 'rest', summary: 'болезнь' })).toBe('Сегодня лучше отдохнуть: болезнь')
  })

  it.each(['normal', 'light'] as const)('uses the adjusted wording for %s', (readiness) => {
    expect(planTitle({ readiness, summary: 'мало сна' })).toBe('План скорректирован: мало сна')
  })

  it('has no colon without a summary', () => {
    expect(planTitle({ readiness: 'light', summary: null })).toBe('План скорректирован')
    expect(planTitle({ readiness: 'light', summary: '   ' })).toBe('План скорректирован')
    expect(planTitle({ readiness: 'rest', summary: '' })).toBe('Сегодня лучше отдохнуть')
  })

  it('trims the summary', () => {
    expect(planTitle({ readiness: 'normal', summary: '  мало сна ' })).toBe('План скорректирован: мало сна')
  })
})
