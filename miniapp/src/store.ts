// Local-only state until the stage 2 API exists: everything lives in localStorage.
// Shapes mirror the future server models (Workout -> exercises -> sets) so the swap is mechanical.
import { useSyncExternalStore } from 'react'
import { getDay, getProgram, isDropset, programPosition, type ProgramExercise } from './program'
import { buildDemoHistory, demoStartDate } from './mock'

export interface SetEntry {
  weight: number | null // kg
  reps: number | null
  done: boolean
}

export interface ExerciseLog {
  name: string
  target: string // prescription.raw at the time of the workout
  dropset: boolean
  sets: SetEntry[]
}

export interface Workout {
  id: string
  programId: string
  week: number
  weekday: number
  startedAt: string // ISO, UTC
  finishedAt: string | null
  exercises: ExerciseLog[]
}

export interface State {
  programId: string
  startDate: string // YYYY-MM-DD, a Monday
  restSeconds: number
  restEnd: number | null // epoch ms when the current rest ends
  active: Workout | null
  skipAutoStart: string | null // local date the user cancelled today's prepared workout
  history: Workout[]
}

const KEY = 'gymapp.v1'

function initialState(): State {
  const program = getProgram(null)
  const startDate = demoStartDate()
  return {
    programId: program.id,
    startDate,
    restSeconds: 90,
    restEnd: null,
    skipAutoStart: null,
    active: null,
    history: buildDemoHistory(program, startDate),
  }
}

function load(): State {
  try {
    const raw = localStorage.getItem(KEY)
    if (raw) return { ...initialState(), ...(JSON.parse(raw) as State) }
  } catch {
    // Storage blocked or corrupted: start from the demo state.
  }
  return initialState()
}

let state: State = load()
const listeners = new Set<() => void>()

function commit(next: State) {
  state = next
  try {
    localStorage.setItem(KEY, JSON.stringify(state))
  } catch {
    // Ignore quota/private-mode errors; the in-memory state still works.
  }
  listeners.forEach((l) => l())
}

export function useStore(): State {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    () => state,
  )
}

export function getState() {
  return state
}

/** Last completed sets for an exercise, newest workout first. */
export function lastSetsFor(name: string, history = state.history): SetEntry[] | null {
  for (let i = history.length - 1; i >= 0; i--) {
    const ex = history[i].exercises.find((e) => e.name === name)
    const done = ex?.sets.filter((s) => s.done && s.weight != null)
    if (done?.length) return done
  }
  return null
}

function plannedLog(ex: ProgramExercise): ExerciseLog {
  const last = lastSetsFor(ex.name)
  const weight = last ? Math.max(...last.map((s) => s.weight ?? 0)) : null
  return {
    name: ex.name,
    target: ex.prescription.raw,
    dropset: isDropset(ex.prescription),
    sets: Array.from({ length: ex.prescription.sets }, () => ({ weight, reps: null, done: false })),
  }
}

/** Program weeks are counted Monday to Sunday, so the start date snaps to its Monday. */
function toMonday(iso: string): string {
  const d = new Date(iso + 'T00:00:00')
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7))
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** Workouts of the current run of the active program (ignores other programs and earlier starts). */
export function currentRun(s: State = state): Workout[] {
  const since = new Date(s.startDate + 'T00:00:00').getTime()
  return s.history.filter((w) => w.programId === s.programId && new Date(w.startedAt).getTime() >= since)
}

function localDate(d = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** True once at least one set of the workout is ticked, i.e. the user really started training. */
export function isStarted(w: Workout): boolean {
  return w.exercises.some((e) => e.sets.some((s) => s.done))
}

export const actions = {
  /**
   * Opening the app prepares the next workout of the current program week that is not logged yet
   * (today's day if it is a training day, otherwise the next one, so a shifted schedule still works).
   * Skipped when something is already logged today or the user dismissed it.
   */
  prepareToday() {
    if (state.active) return
    const today = localDate()
    if (state.skipAutoStart === today) return
    const program = getProgram(state.programId)
    const pos = programPosition(program, state.startDate)
    if (pos.finished || pos.notStarted) return
    const run = currentRun()
    if (run.some((w) => localDate(new Date(w.startedAt)) === today)) return
    const days = program.weeks.find((w) => w.number === pos.week)?.days ?? []
    const next = days.find((d) => !run.some((w) => w.week === pos.week && w.weekday === d.weekday))
    if (next) actions.startWorkout(pos.week, next.weekday)
  },

  startWorkout(week: number, weekday: number) {
    const program = getProgram(state.programId)
    const day = getDay(program, week, weekday)
    if (!day) return
    commit({
      ...state,
      active: {
        id: crypto.randomUUID(),
        programId: program.id,
        week,
        weekday,
        startedAt: new Date().toISOString(),
        finishedAt: null,
        exercises: day.exercises.map(plannedLog),
      },
    })
  },

  updateSet(exIdx: number, setIdx: number, patch: Partial<SetEntry>) {
    const a = state.active
    if (!a) return
    const exercises = a.exercises.map((ex, i) =>
      i !== exIdx ? ex : { ...ex, sets: ex.sets.map((s, j) => (j === setIdx ? { ...s, ...patch } : s)) },
    )
    // The clock starts with the first ticked set, not when the template was prepared.
    const startedAt = patch.done && !isStarted(a) ? new Date().toISOString() : a.startedAt
    commit({ ...state, active: { ...a, startedAt, exercises } })
  },

  addExercise(name: string) {
    const a = state.active
    if (!a) return
    const last = lastSetsFor(name)
    const weight = last ? Math.max(...last.map((s) => s.weight ?? 0)) : null
    const log: ExerciseLog = {
      name,
      target: '',
      dropset: false,
      sets: Array.from({ length: 3 }, () => ({ weight, reps: null, done: false })),
    }
    commit({ ...state, active: { ...a, exercises: [...a.exercises, log] } })
  },

  removeExercise(exIdx: number) {
    const a = state.active
    if (!a) return
    commit({ ...state, active: { ...a, exercises: a.exercises.filter((_, i) => i !== exIdx) } })
  },

  addSet(exIdx: number) {
    const a = state.active
    if (!a) return
    const exercises = a.exercises.map((ex, i) => {
      if (i !== exIdx) return ex
      const prev = ex.sets[ex.sets.length - 1]
      return { ...ex, sets: [...ex.sets, { weight: prev?.weight ?? null, reps: null, done: false }] }
    })
    commit({ ...state, active: { ...a, exercises } })
  },

  finishWorkout() {
    const a = state.active
    if (!a) return
    const exercises = a.exercises
      .map((ex) => ({ ...ex, sets: ex.sets.filter((s) => s.done) }))
      .filter((ex) => ex.sets.length)
    const history = exercises.length
      ? [...state.history, { ...a, exercises, finishedAt: new Date().toISOString() }]
      : state.history
    commit({ ...state, active: null, history })
  },

  cancelWorkout() {
    commit({ ...state, active: null, skipAutoStart: localDate() })
  },

  deleteWorkout(id: string) {
    commit({ ...state, history: state.history.filter((w) => w.id !== id) })
  },

  setProgram(programId: string, startDate: string) {
    commit({ ...state, programId, startDate: toMonday(startDate) })
  },

  setRestSeconds(restSeconds: number) {
    commit({ ...state, restSeconds })
  },

  resetDemo() {
    const startDate = demoStartDate()
    commit({ ...state, startDate, history: buildDemoHistory(getProgram(state.programId), startDate) })
  },

  clearAll() {
    commit({ ...state, history: [] })
  },

  setRestEnd(restEnd: number | null) {
    commit({ ...state, restEnd })
  },
}
