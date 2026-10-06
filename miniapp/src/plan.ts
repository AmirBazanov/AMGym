// Adaptive day plan: applies the server's corrections to a program day.
// Pure and node-safe: type-only imports from api/store (api.ts reads window), the rest comes in as arguments.
import type { DayPlan, DayPlanExercise } from './api'
import { formatPrescription, isDropset, type Prescription, type ProgramDay, type ProgramExercise } from './program'
import { equipmentStep, roundToStep, suggestWeight } from './progression'
import type { Workout } from './store'

export type PlanMode = 'adjusted' | 'program'

export interface AdjustedExercise {
  /** What to do today: the name may be replaced, sets and reps adjusted; `prescription.raw` is `target`. */
  exercise: ProgramExercise
  /** The program's exercise as written. */
  original: ProgramExercise
  replaced: boolean
  /** Suggested weight × factor, on the equipment step; null without history. */
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
 * The plan belongs to `day` on `today`: it is adjusted, dated today, and every exercise it names
 * is in that day. The app may have prepared another day of the week than the server planned for.
 */
export function planFitsDay(day: ProgramDay | undefined, plan: DayPlan | null | undefined, today: string): boolean {
  if (!day || !plan || !plan.adjusted || plan.date !== today || !plan.exercises.length) return false
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
 * Weight × factor on the equipment step. A lighter factor rounds down and a heavier one up, so the
 * result never lands back on the base weight by rounding. Factor 1 keeps the base as is (it may be
 * off-step, e.g. 13.5 kg dumbbells from history).
 */
export function scaleWeight(base: number | null, factor: number, step: number): number | null {
  if (base == null || factor === 1) return base
  const x = (base * factor) / step
  const steps = factor < 1 ? Math.floor(x + 1e-9) : Math.ceil(x - 1e-9)
  return Math.max(0, roundToStep(steps * step, step))
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

function unchanged(e: ProgramExercise, history: Workout[]): AdjustedExercise {
  const weight = suggestWeight(history, e)?.weight ?? null
  const target = formatPrescription(e.prescription)
  return { exercise: e, original: e, replaced: false, weight, baseWeight: weight, factor: 1, reason: null, target, changed: false }
}

/**
 * The day as it should be trained: plan corrections applied by exercise name (sets, reps, weight
 * factor, skip, replacement). Without a fitting plan, or when it would skip everything, the program
 * day as written (`applied` false), so the user always has something to train.
 */
export function applyPlan(day: ProgramDay, plan: DayPlan | null, history: Workout[], today: string): AppliedPlan {
  const plain = (): AppliedPlan => ({ exercises: day.exercises.map((e) => unchanged(e, history)), skipped: [], applied: false })
  if (!plan || !planFitsDay(day, plan, today)) return plain()

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
    // A replacement has its own history: its base weight and step come from its own name.
    const baseWeight = suggestWeight(history, { ...exercise, prescription })?.weight ?? null
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
