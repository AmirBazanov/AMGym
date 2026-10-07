// Adaptive day plan: applies the server's corrections to a program day.
// Pure and node-safe: type-only imports from api/store (api.ts reads window), the rest comes in as arguments.
import type { DayPlan, DayPlanExercise } from './api'
import { formatPrescription, isDropset, type Prescription, type ProgramDay, type ProgramExercise } from './program'
import { equipmentStep, hasHistory, roundToStep, suggestWeight } from './progression'
import type { Baseline, ExerciseLog, Workout } from './store'

export type PlanMode = 'adjusted' | 'program'

export interface AdjustedExercise {
  /** What to do today: the name may be replaced, sets and reps adjusted; `prescription.raw` is `target`. */
  exercise: ProgramExercise
  /** The program's exercise as written. */
  original: ProgramExercise
  replaced: boolean
  /** Suggested weight × factor, on the equipment step; null without history and baselines. */
  weight: number | null
  /** Suggested weight before the factor. */
  baseWeight: number | null
  factor: number
  reason: string | null
  /** "3 × 8–12 (по плану 4 × 8–12)" when sets or reps differ, otherwise the usual prescription. */
  target: string
  changed: boolean
}

export interface AppliedPlan {
  exercises: AdjustedExercise[] // skipped ones are left out
  skipped: { name: string; reason: string | null }[]
  /** The plan was applied (false for no plan, a plan for another day, or everything skipped). */
  applied: boolean
}

/**
 * The plan belongs to `day` (of program `week`) on `today`: it is adjusted, dated today, its week and
 * weekday (when the server sends them) match, and every exercise it names is in that day. The app may
 * have prepared, or the user may be looking at, another day with the same exercises.
 */
export function planFitsDay(
  day: ProgramDay | undefined,
  plan: DayPlan | null | undefined,
  today: string,
  week?: number,
): boolean {
  if (!day || !plan || !plan.adjusted || plan.date !== today || !plan.exercises.length) return false
  // Newer servers name the program day; older answers without it fall back to the name check below.
  if (plan.weekday != null && plan.weekday !== day.weekday) return false
  if (plan.week != null && week != null && plan.week !== week) return false
  return plan.exercises.every((p) => day.exercises.some((e) => e.name === p.name))
}

/** Same content, same key: refetches return new objects, so identity cannot tell a real change. */
export function planKey(plan: DayPlan | null): string {
  if (!plan) return 'program'
  return JSON.stringify(
    plan.exercises.map((e) => [e.name, e.sets, e.repsMin, e.repsMax, e.weightFactor, e.skip, e.replaceWith]),
  )
}

/** Sane factor: missing, non-finite or out of 0.3..1.5 counts as "no change". */
export function safeFactor(f: number | null | undefined): number {
  return f != null && Number.isFinite(f) && f >= 0.3 && f <= 1.5 ? f : 1
}

/**
 * Weight × factor on the equipment step:
 * - rounded to the nearest step (67.5 × 0.9 = 60.75 -> 60, 20 × 0.85 = 17 -> 17.5), a half towards the factor;
 * - when that lands back on the base, one step further (10 × 0.95 on 1 kg steps -> 9), but only if the
 *   step changes the weight by at most twice the requested amount: 5 kg × 0.9 stays 5, not 2.5 (−50 %);
 * - never below one step (2.5 kg × 0.9 stays 2.5, never 0) and never across the base in the wrong direction.
 * Factor 1 keeps the base as is (it may be off-step, e.g. 13.5 kg dumbbells from history).
 */
export function scaleWeight(base: number | null, factor: number, step: number): number | null {
  if (base == null || factor === 1) return base
  if (base <= step && factor < 1) return base
  const target = base * factor
  // Nearest step; an exact half goes the way the factor asks (15 × 0.9 = 13.5 -> 13 on 1 kg steps).
  const x = target / step
  let r = roundToStep((factor < 1 ? Math.ceil(x - 0.5 - 1e-9) : Math.floor(x + 0.5 + 1e-9)) * step, step)
  if (factor < 1 && r >= base) {
    // Largest step multiple strictly below the base (the base itself may be off-step).
    const lower = roundToStep((Math.ceil(base / step - 1e-9) - 1) * step, step)
    r = base - lower <= 2 * (base - target) + 1e-9 ? lower : base
  } else if (factor > 1 && r <= base) {
    const upper = roundToStep((Math.floor(base / step + 1e-9) + 1) * step, step)
    r = upper - base <= 2 * (target - base) + 1e-9 ? upper : base
  }
  return factor < 1 ? Math.min(base, Math.max(step, r)) : Math.max(base, r)
}

function adjustPrescription(p: Prescription, a: DayPlanExercise | undefined): Prescription {
  if (!a) return p
  const sets = a.sets != null && a.sets >= 1 ? Math.round(a.sets) : p.sets
  // Dropsets keep their drops; only the number of sets changes.
  if (isDropset(p)) return { ...p, sets }
  const repsMin = a.repsMin != null && a.repsMin >= 1 ? a.repsMin : p.reps_min
  let repsMax = a.repsMax != null && a.repsMax >= 1 ? a.repsMax : p.reps_max
  if (repsMin != null && repsMax != null && repsMax < repsMin) repsMax = repsMin
  return { ...p, sets, reps_min: repsMin, reps_max: repsMax }
}

function unchanged(e: ProgramExercise, history: Workout[], baselines: readonly Baseline[]): AdjustedExercise {
  const weight = suggestWeight(history, e, baselines)?.weight ?? null
  const target = formatPrescription(e.prescription)
  return { exercise: e, original: e, replaced: false, weight, baseWeight: weight, factor: 1, reason: null, target, changed: false }
}

/**
 * The day as it should be trained: plan corrections applied by exercise name (sets, reps, weight
 * factor, skip, replacement). Without a fitting plan, or when it would skip everything, the program
 * day as written (`applied` false), so the user always has something to train.
 * Weights come from history, else from the owner's baselines; the plan factor applies on top of either.
 */
export function applyPlan(
  day: ProgramDay,
  plan: DayPlan | null,
  history: Workout[],
  today: string,
  week?: number,
  baselines: readonly Baseline[] = [],
): AppliedPlan {
  const plain = (): AppliedPlan => ({
    exercises: day.exercises.map((e) => unchanged(e, history, baselines)),
    skipped: [],
    applied: false,
  })
  if (!plan || !planFitsDay(day, plan, today, week)) return plain()

  const exercises: AdjustedExercise[] = []
  const skipped: AppliedPlan['skipped'] = []
  for (const original of day.exercises) {
    const a = plan.exercises.find((p) => p.name === original.name)
    if (a?.skip) {
      skipped.push({ name: original.name, reason: a.reason?.trim() || null })
      continue
    }
    const newName = a?.replaceWith?.trim().toLowerCase() || null
    const replaced = newName != null && newName !== original.name
    const prescription = adjustPrescription(original.prescription, a)
    const programTarget = formatPrescription(original.prescription)
    const newTarget = formatPrescription(prescription)
    const target = newTarget === programTarget ? programTarget : `${newTarget} (по плану ${programTarget})`
    const exercise: ProgramExercise = {
      ...original,
      name: replaced ? newName : original.name,
      prescription: { ...prescription, raw: target },
    }
    // A replacement has its own history and baseline: its base weight and step come from its own name.
    const baseWeight = suggestWeight(history, { ...exercise, prescription }, baselines)?.weight ?? null
    const factor = safeFactor(a?.weightFactor)
    const weight = scaleWeight(baseWeight, factor, equipmentStep(exercise.name))
    exercises.push({
      exercise,
      original,
      replaced,
      weight,
      baseWeight,
      factor,
      reason: a?.reason?.trim() || null,
      target,
      changed: replaced || target !== programTarget || factor !== 1,
    })
  }
  if (!exercises.length) return plain()
  return { exercises, skipped, applied: true }
}

/** Small line under an adjusted exercise: "мало сна · вес −10 % · вместо «жим лёжа»". Null when unchanged. */
export function planNote(a: AdjustedExercise): string | null {
  if (!a.changed && !a.reason) return null
  const parts: string[] = []
  if (a.reason) parts.push(a.reason)
  if (a.factor !== 1) {
    const pct = Math.round((a.factor - 1) * 100)
    if (pct) parts.push(`вес ${pct < 0 ? '−' : '+'}${Math.abs(pct)} %`)
  }
  if (a.replaced) parts.push(`вместо «${a.original.name}»`)
  return parts.length ? parts.join(' · ') : null
}

/** Banner title: rest days read differently from a lighter day. */
export function planTitle(plan: Pick<DayPlan, 'readiness' | 'summary'>): string {
  const summary = plan.summary?.trim()
  const head = plan.readiness === 'rest' ? 'Сегодня лучше отдохнуть' : 'План скорректирован'
  return summary ? `${head}: ${summary}` : head
}

/** What the app put into one exercise of the prepared workout, so new baselines can refill it later. */
export interface PreparedSuggestion {
  /** The exercise as applied: replacement name, adjusted prescription. */
  exercise: ProgramExercise
  factor: number
  /** The weight the app filled in (after the factor); null when it had none. */
  weight: number | null
}

/** By exercise name; the first one wins when a day repeats an exercise. */
export type PreparedSuggestions = Record<string, PreparedSuggestion>

/** Snapshot of the weights a freshly built prepared workout got (store.plannedLog puts `weight` in every set). */
export function suggestionsOf(exercises: readonly AdjustedExercise[]): PreparedSuggestions {
  const out: PreparedSuggestions = {}
  for (const a of exercises) {
    if (!out[a.exercise.name]) out[a.exercise.name] = { exercise: a.exercise, factor: a.factor, weight: a.weight }
  }
  return out
}

/**
 * New baselines applied to the prepared workout in place, without rebuilding it: added and removed
 * exercises and the user's own weights stay. Only exercises without any history are touched, and in them
 * only sets whose weight is empty or still the one the app suggested; the plan factor applies on top.
 * An exercise missing from `suggested` (workout prepared before the snapshot existed) falls back to the
 * day's exercise with factor 1, so only its empty weights are filled. User-added exercises (in neither)
 * are left alone. A started workout is never touched. Returns null when nothing changes.
 */
export function refillFromBaselines(
  workout: Workout,
  suggested: PreparedSuggestions,
  day: ProgramDay | undefined,
  history: Workout[],
  baselines: readonly Baseline[],
): { workout: Workout; suggested: PreparedSuggestions } | null {
  if (workout.exercises.some((e) => e.sets.some((s) => s.done))) return null
  const nextSuggested: PreparedSuggestions = { ...suggested }
  let changed = false
  const exercises = workout.exercises.map((log): ExerciseLog => {
    if (hasHistory(history, log.name)) return log
    const known = suggested[log.name]
    const fromDay = day?.exercises.find((e) => e.name === log.name)
    const prev: PreparedSuggestion | null = known ?? (fromDay ? { exercise: fromDay, factor: 1, weight: null } : null)
    if (!prev) return log
    const base = suggestWeight([], prev.exercise, baselines)?.weight ?? null
    const next = scaleWeight(base, prev.factor, equipmentStep(log.name))
    if (!known || next !== prev.weight) {
      nextSuggested[log.name] = { ...prev, weight: next }
      changed = true
    }
    let setsChanged = false
    const sets = log.sets.map((s) => {
      if (s.done || s.weight === next || (s.weight != null && s.weight !== prev.weight)) return s
      setsChanged = true
      return { ...s, weight: next }
    })
    if (!setsChanged) return log
    changed = true
    return { ...log, sets }
  })
  return changed ? { workout: { ...workout, exercises }, suggested: nextSuggested } : null
}
