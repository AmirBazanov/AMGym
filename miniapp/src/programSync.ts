// Pure parts of serving programs from the server (GET /api/programs/{slug}): the wire format to Program,
// the offline cache by slug and version, building and rebuilding the prepared workout, exercise lists.
// Node-safe: type-only imports from api/store (api.ts reads window); store.ts wires it to the requests.
import type { CatalogExercise, ProgramOut } from './api'
import { applyPlan, suggestionsOf, todaysProgramDay, type AdjustedExercise, type PreparedSuggestions } from './plan'
import {
  findProgram,
  getDay,
  isDropset,
  programExerciseNames,
  type Program,
  type ProgramDay,
} from './program'
import type { Baseline, ExerciseLog, WeightOverride, Workout } from './store'

/** Server programs by slug, each with its `version`: what store.ts keeps in localStorage for the gym offline. */
export type ProgramCache = Readonly<Record<string, Program>>

/** GET /api/programs/{slug} as a Program; only known keys are kept, so the cache holds nothing extra. */
export function programFromServer(out: ProgramOut): Program {
  return {
    id: out.id,
    name: out.name,
    source: out.source ?? '',
    version: out.version,
    editable: out.editable,
    basedOn: out.basedOn ?? null,
    weeks: out.weeks.map((w) => ({
      number: w.number,
      days: w.days.map((d) => ({
        id: d.id,
        weekday: d.weekday,
        title: d.title,
        focus: d.focus ?? null,
        exercises: d.exercises.map((e) => ({
          id: e.id,
          name: e.name,
          intensity: e.intensity ?? null,
          order: e.order,
          prescription: {
            sets: e.prescription.sets,
            reps_min: e.prescription.reps_min ?? null,
            reps_max: e.prescription.reps_max ?? null,
            drop_reps: e.prescription.drop_reps ?? null,
            raw: e.prescription.raw,
          },
        })),
      })),
    })),
  }
}

/**
 * Whether GET /api/programs/{slug} is needed: the cache lacks the slug or holds another version than
 * /api/state reported. `version` null: an older server without the programs API, nothing to fetch (the
 * bundled program is used).
 */
export function needsProgramFetch(cache: ProgramCache, slug: string, version: number | null | undefined): boolean {
  if (version == null) return false
  const cached = Object.prototype.hasOwnProperty.call(cache, slug) ? cache[slug] : undefined
  return !cached || cached.version !== version
}

/**
 * The cache with `program` in it (replacing the same slug) and only the slugs still needed (`keep`:
 * the active program, the workout in progress and the offline queue), so old copies do not pile up.
 */
export function cacheProgram(
  cache: ProgramCache,
  program: Program,
  keep: readonly (string | null | undefined)[],
): Record<string, Program> {
  const wanted = new Set([program.id, ...keep.filter((k): k is string => !!k)])
  const out: Record<string, Program> = {}
  for (const [slug, p] of Object.entries(cache)) if (wanted.has(slug)) out[slug] = p
  out[program.id] = program
  return out
}

/** A prepared workout's exercise from the plan (store.plannedLog): every set gets the suggested weight. */
export function plannedLog(adj: AdjustedExercise): ExerciseLog {
  const ex = adj.exercise
  return {
    name: ex.name,
    target: adj.changed ? adj.target : adj.original.prescription.raw,
    dropset: isDropset(ex.prescription),
    sets: Array.from({ length: ex.prescription.sets }, () => ({ weight: adj.weight, reps: null, done: false })),
  }
}

/** What prepareToday needs to know about the active program; inputs of the pure decision below. */
export interface PrepareInputs {
  programId: string
  startDate: string
  run: readonly Workout[] // workouts of the current run (store.currentRun)
  now?: Date
}

/**
 * The program and day today's workout is prepared from, or null. Null also when the active program is
 * not known yet (a copy not loaded on a cold start): never fall back to another program, the workout
 * would be logged against it. findProgram reads the registry (server cache, then bundled).
 */
export function preparePick(inputs: PrepareInputs): { program: Program; day: ProgramDay; week: number; weekday: number } | null {
  const program = findProgram(inputs.programId)
  if (!program) return null
  const pos = todaysProgramDay(program, inputs.startDate, inputs.run, inputs.now)
  if (!pos) return null
  const day = getDay(program, pos.week, pos.weekday)
  return day ? { program, day, ...pos } : null
}

export interface BuildInputs {
  history: Workout[]
  today: string // local YYYY-MM-DD
  baselines: readonly Baseline[]
  overrides: readonly WeightOverride[]
}

/**
 * A fresh prepared workout of `day` as written (the day plan is applied later by applyDayPlan).
 * `programId` is the active program's slug from the state, never the id of a fallback program.
 */
export function buildPrepared(
  id: string,
  programId: string,
  week: number,
  day: ProgramDay,
  startedAt: string,
  inputs: BuildInputs,
): { workout: Workout; suggested: PreparedSuggestions } {
  const res = applyPlan(day, null, inputs.history, inputs.today, week, inputs.baselines, inputs.overrides)
  return {
    workout: {
      id,
      programId,
      programDayId: day.id ?? null,
      week,
      weekday: day.weekday,
      startedAt,
      finishedAt: null,
      exercises: res.exercises.map(plannedLog),
    },
    suggested: suggestionsOf(res.exercises),
  }
}

/** Same exercises with the same prescriptions in the same order: a program reload changed nothing to train. */
export function sameDayContent(a: ProgramDay | undefined, b: ProgramDay | undefined): boolean {
  if (!a || !b) return a === b
  const content = (d: ProgramDay) =>
    JSON.stringify(
      d.exercises.map((e) => [
        e.name,
        e.intensity,
        e.order,
        e.prescription.sets,
        e.prescription.reps_min,
        e.prescription.reps_max,
        e.prescription.drop_reps,
        e.prescription.raw,
      ]),
    )
  return content(a) === content(b)
}

/**
 * What a reload of the active workout's program does to it (store.rebuildPrepared):
 * - 'keep': started (a session in progress is never touched), another program, or nothing changed;
 * - 'link': only the day's server id is new (bundled program replaced by the server's): set programDayId
 *   in place, so the weights the owner already edited stay;
 * - 'rebuild': the day's exercises changed: build the prepared workout again (drop the plan key, so the
 *   day plan is applied again);
 * - 'drop': the day is gone from the program: drop the prepared workout, prepareToday picks again.
 */
export function rebuildDecision(
  active: Workout | null,
  slug: string,
  oldDay: ProgramDay | undefined,
  newDay: ProgramDay | undefined,
): 'keep' | 'link' | 'rebuild' | 'drop' {
  if (!active || active.programId !== slug) return 'keep'
  if (active.exercises.some((e) => e.sets.some((s) => s.done))) return 'keep'
  if (!newDay) return 'drop'
  if (!sameDayContent(oldDay, newDay)) return 'rebuild'
  return (active.programDayId ?? null) === (newDay.id ?? null) ? 'keep' : 'link'
}

/**
 * Exercises to list on Progress and in the program's exercise tab: the program's first (its order), then
 * those only in the history, newest first. A replaced exercise keeps its chart and history.
 */
export function exerciseNamesWithHistory(program: Program, history: readonly Workout[]): string[] {
  const names = programExerciseNames(program)
  const seen = new Set(names)
  for (let i = history.length - 1; i >= 0; i--) {
    for (const e of history[i].exercises) {
      if (!seen.has(e.name)) {
        seen.add(e.name)
        names.push(e.name)
      }
    }
  }
  return names
}

/**
 * Names for «Добавить упражнение»: the program's exercises first (as before), then the rest of the
 * catalog in its order (most logged sets first). Without the catalog (demo, offline): the program's only.
 */
export function pickerNames(
  catalog: readonly CatalogExercise[] | null | undefined,
  program: Program,
  exclude: readonly string[],
): string[] {
  const names = programExerciseNames(program)
  const seen = new Set(names)
  for (const c of catalog ?? []) {
    if (!seen.has(c.name)) {
      seen.add(c.name)
      names.push(c.name)
    }
  }
  return names.filter((n) => !exclude.includes(n))
}
