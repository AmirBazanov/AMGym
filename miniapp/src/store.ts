// Local-only state until the stage 2 API exists: everything lives in localStorage.
// Shapes mirror the future server models (Workout -> exercises -> sets) so the swap is mechanical.
import { useSyncExternalStore } from 'react'
import { getDay, getProgram, isDropset, type ProgramExercise } from './program'
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
  active: Workout | null
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

export const actions = {
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
    commit({ ...state, active: { ...a, exercises } })
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
    commit({ ...state, active: null })
  },

  deleteWorkout(id: string) {
    commit({ ...state, history: state.history.filter((w) => w.id !== id) })
  },

  setProgram(programId: string, startDate: string) {
    commit({ ...state, programId, startDate })
  },

  setRestSeconds(restSeconds: number) {
    commit({ ...state, restSeconds })
  },

  resetDemo() {
    commit(initialState())
  },

  clearAll() {
    commit({ ...initialState(), history: [] })
  },
}
