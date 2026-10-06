// Day weight suggestions: the record (estimated 1RM) and double progression, the higher one wins.
// Pure functions: history and the program exercise come in as arguments, nothing is read from the store.
// History sets of a dropset are already the first set of each dropset: the server folds the drops
// (drop_index > 0) into their main set, so every SetEntry here is a working set.
import type { Intensity, Prescription, ProgramExercise } from './program'
import type { SetEntry, Workout } from './store'
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

/** Weight for today's plan: max of the record-based weight and double progression, with a reason in Russian. */
export function suggestWeight(history: Workout[], exercise: ProgramExercise): Suggestion | null {
  const { name, prescription: p, intensity } = exercise
  const last = lastSameSession(history, name)
  const record = bestE1rm(history, name)
  if (!last && !record) return null
  const step = equipmentStep(name)

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
    const reps = p.drop_reps?.[0] ?? p.reps_max ?? p.reps_min ?? null
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
