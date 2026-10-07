// Day weight suggestions: the record (estimated 1RM) and double progression, the higher one wins.
// Pure functions: history and the program exercise come in as arguments, nothing is read from the store.
// Without any history the owner's own words (baselines from the bot chat) give a starting weight.
// History sets of a dropset are already the first set of each dropset: the server folds the drops
// (drop_index > 0) into their main set, so every SetEntry here is a working set.
import type { Intensity, Prescription, ProgramExercise } from './program'
import type { Baseline, SetEntry, WeightOverride, Workout } from './store'
import { e1rm, formatKg } from './stats'

export interface Record1rm {
  e1rm: number
  weight: number
  reps: number
  date: string // ISO, startedAt of the workout
}

export interface Suggestion {
  weight: number
  reason: string
  /** The owner set this weight for today himself: used as is, no plan factor, no rounding. */
  override?: WeightOverride
}

/** Genitive after "для": для 1 повтора, для 8 повторов. */
function repsWord(n: number): string {
  return `${n} ${n % 10 === 1 && n % 100 !== 11 ? 'повтора' : 'повторов'}`
}

/** Removes float noise (60 + 2.5 * 0.1 ...) so 62.5 is never printed as 62.4999. */
function clean(n: number): number {
  return Math.round(n * 100) / 100
}

function workingSets(history: Workout, name: string): SetEntry[] {
  const ex = history.exercises.find((e) => e.name === name)
  return ex?.sets.filter((s) => s.done && s.weight != null) ?? []
}

/** Best Epley 1RM over all completed weighted sets: the exercise record. Same rule as stats.exerciseSeries. */
export function bestE1rm(history: Workout[], name: string): Record1rm | null {
  let best: Record1rm | null = null
  for (const w of history) {
    for (const s of workingSets(w, name)) {
      if (s.weight! <= 0) continue
      const reps = s.reps ?? 1
      const value = e1rm(s.weight!, reps)
      if (!best || value > best.e1rm) best = { e1rm: value, weight: s.weight!, reps, date: w.startedAt }
    }
  }
  return best
}

/** Smallest weight increment: dumbbells go up by 1 kg, barbells and machines by 2.5 kg. */
export function equipmentStep(name: string): 1 | 2.5 {
  return name.toLowerCase().includes('гантел') ? 1 : 2.5
}

export function roundToStep(weight: number, step: number): number {
  return clean(Math.round(weight / step) * step)
}

/**
 * Share of 1RM for a working weight. Heavy = the weight that is a true max for that many reps
 * (inverse Epley), medium = 90 % of it, light = 80 %. No intensity counts as medium.
 */
export function percentOf1rm(repsMax: number | null, intensity: Intensity | null, repsMin: number | null = null): number {
  const reps = repsMax ?? repsMin ?? 10
  const heavy = 1 / (1 + reps / 30)
  if (intensity === 'heavy') return heavy
  if (intensity === 'light') return heavy * 0.8
  return heavy * 0.9
}

/** Completed weighted sets of the latest workout that has the exercise (same as store.lastSetsFor). */
export function lastSameSession(history: Workout[], name: string): SetEntry[] | null {
  for (let i = history.length - 1; i >= 0; i--) {
    const done = workingSets(history[i], name)
    if (done.length) return done
  }
  return null
}

/** Reps every working set must reach to earn the next step: first drop for dropsets, else the top of the range. */
export function progressionReps(p: Prescription): number | null {
  return p.drop_reps?.[0] ?? p.reps_max ?? p.reps_min
}

/**
 * Double progression: last time's top weight, plus one step when every working set was done at that
 * same weight with at least the top of the rep range. Missed reps keep the weight (never lowered).
 */
export function doubleProgression(last: SetEntry[] | null, prescription: Prescription, step: number): number | null {
  const sets = last?.filter((s) => s.weight != null) ?? []
  if (!sets.length) return null
  const top = Math.max(...sets.map((s) => s.weight!))
  const target = progressionReps(prescription)
  if (target == null) return top
  const allAtTop = sets.every((s) => s.weight === top && s.reps != null && s.reps >= target)
  return allAtTop ? clean(top + step) : top
}

/** Case, whitespace and ё/е do not matter for baseline names: "Жим  лёжа " is "жим лежа" (as on the server). */
function normName(name: string): string {
  return name.trim().replace(/\s+/g, ' ').toLowerCase().replace(/ё/g, 'е')
}

/**
 * Server baselines made safe: absent (older server) or not a list -> [], entries without a name or a
 * positive weight dropped, reps below 1 count as unknown.
 */
export function normalizeBaselines(raw: unknown): Baseline[] {
  if (!Array.isArray(raw)) return []
  const out: Baseline[] = []
  for (const b of raw as Partial<Baseline>[]) {
    if (!b || typeof b.exercise !== 'string' || !b.exercise.trim()) continue
    if (typeof b.weightKg !== 'number' || !Number.isFinite(b.weightKg) || b.weightKg <= 0) continue
    const reps = typeof b.reps === 'number' && Number.isFinite(b.reps) && b.reps >= 1 ? Math.round(b.reps) : null
    out.push({ exercise: b.exercise, weightKg: b.weightKg, reps, factId: typeof b.factId === 'number' ? b.factId : 0 })
  }
  return out
}

/** The owner's latest words about this exercise (highest factId when the server sends several). */
export function findBaseline(baselines: readonly Baseline[], name: string): Baseline | null {
  const key = normName(name)
  let best: Baseline | null = null
  for (const b of baselines) {
    if (normName(b.exercise) === key && (!best || b.factId > best.factId)) best = b
  }
  return best
}

/** Hint for a weight the owner set for today: «ты поставил на сегодня 85 кг». */
export function overrideReason(kg: number): string {
  return `ты поставил на сегодня ${formatKg(kg)} кг`
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/

/**
 * Server weight overrides made safe: absent or not a list -> [], entries without a name, a positive
 * weight or a YYYY-MM-DD date dropped.
 */
export function normalizeOverrides(raw: unknown): WeightOverride[] {
  if (!Array.isArray(raw)) return []
  const out: WeightOverride[] = []
  for (const o of raw as Partial<WeightOverride>[]) {
    if (!o || typeof o.exercise !== 'string' || !o.exercise.trim()) continue
    if (typeof o.weightKg !== 'number' || !Number.isFinite(o.weightKg) || o.weightKg <= 0) continue
    if (typeof o.date !== 'string' || !ISO_DAY.test(o.date)) continue
    out.push({ exercise: o.exercise, weightKg: o.weightKg, date: o.date })
  }
  return out
}

/** Overrides from a server answer: an absent field (older server) keeps what the app has, like baselines. */
export function mergeOverrides(raw: unknown, current: WeightOverride[]): WeightOverride[] {
  return raw === undefined ? current : normalizeOverrides(raw)
}

/**
 * The weight the owner set for this exercise on `today` (the app's local YYYY-MM-DD, the same key the day
 * plan uses). Names match like baselines (case, spaces, ё/е). Several for the same day: the last one wins.
 */
export function findOverride(overrides: readonly WeightOverride[], name: string, today: string): WeightOverride | null {
  const key = normName(name)
  let hit: WeightOverride | null = null
  for (const o of overrides) {
    if (o.date === today && normName(o.exercise) === key) hit = o
  }
  return hit
}

/** Reps the working weight must allow: the first drop for dropsets, else the top of the range. */
function targetReps(p: Prescription): number | null {
  return p.drop_reps?.[0] ?? p.reps_max ?? p.reps_min ?? null
}

/**
 * Starting weight from the owner's words, the same share of 1RM as the record branch. Known reps: Epley
 * 1RM (reps 1 = the max itself). Unknown reps ("~100 смогу"): the stated weight counts as the max.
 */
function fromBaseline(b: Baseline, exercise: ProgramExercise, step: number): Suggestion | null {
  const reps = targetReps(exercise.prescription)
  const pct = percentOf1rm(reps, exercise.intensity)
  const max = b.reps == null ? b.weightKg : e1rm(b.weightKg, b.reps)
  const weight = roundToStep(max * pct, step)
  if (weight <= 0) return null
  const share = `${Math.round(pct * 100)} % для ${repsWord(reps ?? 10)}`
  const kg = formatKg(b.weightKg)
  let reason: string
  if (b.reps == null) reason = `по твоим словам ~${kg} кг (как максимум), ${share}`
  else if (b.reps === 1) reason = `от твоего максимума ${kg} кг, ${share}`
  else reason = `по твоим словам ${kg} × ${b.reps} (1ПМ ≈ ${formatKg(Math.round(max))} кг), ${share}`
  return { weight, reason }
}

/** True when the history has a completed weighted set of the exercise: baselines no longer count. */
export function hasHistory(history: Workout[], name: string): boolean {
  return lastSameSession(history, name) != null || bestE1rm(history, name) != null
}

/**
 * Weight for today's plan, in priority order: the owner's override for `today` (as is), else the max of
 * the record-based weight and double progression, else the baseline (only while the history has nothing
 * for the exercise). The reason is in Russian. Without `today` overrides are not looked at.
 */
export function suggestWeight(
  history: Workout[],
  exercise: ProgramExercise,
  baselines: readonly Baseline[] = [],
  overrides: readonly WeightOverride[] = [],
  today?: string,
): Suggestion | null {
  const { name, prescription: p, intensity } = exercise
  const o = today ? findOverride(overrides, name, today) : null
  if (o) return { weight: o.weightKg, reason: overrideReason(o.weightKg), override: o }
  const last = lastSameSession(history, name)
  const record = bestE1rm(history, name)
  const step = equipmentStep(name)
  if (!last && !record) {
    const b = findBaseline(baselines, name)
    return b ? fromBaseline(b, exercise, step) : null
  }

  const progressed = doubleProgression(last, p, step)
  let best: Suggestion | null = null
  if (progressed != null) {
    const top = Math.max(...last!.map((s) => s.weight!))
    const target = progressionReps(p)
    best =
      progressed > top
        ? {
            weight: progressed,
            reason: `прошлый раз ${formatKg(top)} × ${target} во всех подходах, +${formatKg(step)} кг`,
          }
        : { weight: progressed, reason: `как в прошлый раз ${formatKg(top)} кг` }
  }

  if (record) {
    // Dropsets have no rep range: the first drop is what the weight must allow.
    const reps = targetReps(p)
    const pct = percentOf1rm(reps, intensity)
    const fromRecord = roundToStep(record.e1rm * pct, step)
    if (fromRecord > 0 && (!best || fromRecord > best.weight)) {
      best = {
        weight: fromRecord,
        reason: `от рекорда ${formatKg(Math.round(record.e1rm))} кг (1ПМ), ${Math.round(pct * 100)} % для ${repsWord(reps ?? 10)}`,
      }
    }
  }
  return best
}
