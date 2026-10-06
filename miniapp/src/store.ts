// Local-only state until the stage 2 API exists: everything lives in localStorage.
// Shapes mirror the future server models (Workout -> exercises -> sets) so the swap is mechanical.
import { useSyncExternalStore } from 'react'
import { getDay, getProgram, isDropset, programPosition, type ProgramExercise } from './program'
import { api, ApiError, EMPTY_PROFILE, EMPTY_TARGETS, inTelegram, type Profile, type Targets } from './api'
import { buildDemoHistory, demoStartDate } from './mock'
import { lastSameSession, suggestWeight } from './progression'

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
  clientId?: string | null // set on workouts returned by the server: the id this app generated
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
  // 'server': history lives on the bot's server (opened from Telegram); 'demo': local-only preview.
  mode: 'server' | 'demo'
  // Finished workouts not yet accepted by the server (offline in the gym); retried on every sync.
  pending: Workout[]
  // Workouts the server refused (e.g. invalid values); kept so nothing is silently lost.
  rejected: Workout[]
  // Daily nutrition targets (a setting like restSeconds). Food entries themselves are never cached here.
  targets: Targets
  // Profile for the bot's AI advice (weight, height, goal...). Same server-first rules as targets.
  profile: Profile
}

interface ServerState {
  programId: string
  startDate: string
  restSeconds: number
  history: Workout[]
  targets?: Targets // absent on servers older than the nutrition API
  profile?: Partial<Profile> // absent on servers older than the profile API
}

type SettingsPatch = Partial<Pick<State, 'programId' | 'startDate' | 'restSeconds' | 'targets'>> & {
  profile?: Partial<Profile> // partial update: a missing key is kept, null clears it
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
    // Inside Telegram the real history comes from the server; never show demo data there.
    history: inTelegram ? [] : buildDemoHistory(program, startDate),
    mode: inTelegram ? 'server' : 'demo',
    pending: [],
    rejected: [],
    targets: EMPTY_TARGETS,
    profile: EMPTY_PROFILE,
  }
}

function load(): State {
  try {
    const raw = localStorage.getItem(KEY)
    if (raw) {
      const saved = JSON.parse(raw) as Partial<State>
      // Nested merge: storage written before the profile existed (or with fewer keys) still loads.
      return { ...initialState(), ...saved, profile: { ...EMPTY_PROFILE, ...saved.profile } }
    }
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
  return lastSameSession(history, name)
}

function plannedLog(ex: ProgramExercise): ExerciseLog {
  // Record-based weight or double progression, whichever is higher (see progression.ts).
  const weight = suggestWeight(state.history, ex)?.weight ?? null
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
    const exercises = a.exercises.map((ex, i) => {
      if (i !== exIdx) return ex
      const old = ex.sets[setIdx]
      const sets = ex.sets.map((s, j) => {
        if (j === setIdx) {
          const next = { ...s, ...patch }
          // Ticking a set without a weight reuses the previous set's weight.
          if (patch.done && next.weight == null) next.weight = ex.sets[j - 1]?.weight ?? null
          return next
        }
        // A new weight carries over to the following sets that still had the old one.
        if ('weight' in patch && j > setIdx && !s.done && s.weight === old.weight) return { ...s, weight: patch.weight! }
        return s
      })
      return { ...ex, sets }
    })
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
    if (!exercises.length) {
      commit({ ...state, active: null })
      return
    }
    const done: Workout = { ...a, exercises, finishedAt: new Date().toISOString() }
    const server = state.mode === 'server'
    commit({
      ...state,
      active: null,
      history: [...state.history, done],
      pending: server ? [...state.pending, done] : state.pending,
    })
    if (server) void flushPending()
  },

  cancelWorkout() {
    commit({ ...state, active: null, skipAutoStart: localDate() })
  },

  deleteWorkout(id: string) {
    const wasPending = state.pending.some((w) => w.id === id)
    commit({
      ...state,
      history: state.history.filter((w) => w.id !== id),
      pending: state.pending.filter((w) => w.id !== id),
    })
    if (state.mode === 'server' && !wasPending) {
      api(`/workouts/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => void syncFromServer())
    }
  },

  setProgram(programId: string, startDate: string) {
    commit({ ...state, programId, startDate: toMonday(startDate) })
    void pushSettings({ programId, startDate: toMonday(startDate) })
  },

  setRestSeconds(restSeconds: number) {
    commit({ ...state, restSeconds })
    void pushSettings({ restSeconds })
  },

  /**
   * Save nutrition targets. All four values are always sent, so null explicitly clears a target.
   * Resolves to 'local' in the demo, 'saved' once the server accepted them, 'failed' otherwise.
   * In server mode nothing is stored until the server answers: applyServer takes the targets from
   * its response, so a failed save leaves the previous (server) targets and the edit stays unsaved.
   */
  async setTargets(targets: Targets): Promise<'local' | 'saved' | 'failed'> {
    if (state.mode !== 'server') {
      commit({ ...state, targets })
      return 'local'
    }
    const res = await pushSettings({ targets })
    return res?.targets ? 'saved' : 'failed'
  },

  /**
   * Save the changed profile fields (partial update). Same contract as setTargets: in server mode
   * the store only changes from the server's answer, so a failed save keeps the previous profile.
   */
  async setProfile(patch: Partial<Profile>): Promise<'local' | 'saved' | 'failed'> {
    if (state.mode !== 'server') {
      commit({ ...state, profile: { ...state.profile, ...patch } })
      return 'local'
    }
    const res = await pushSettings({ profile: patch })
    return res?.profile ? 'saved' : 'failed'
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

// ---- Server sync (when opened from Telegram) ----

function applyServer(server: ServerState) {
  const known = new Set(server.history.map((w) => w.clientId).filter(Boolean))
  const pending = state.pending.filter((w) => !known.has(w.id))
  commit({
    ...state,
    mode: 'server',
    programId: server.programId,
    startDate: server.startDate,
    restSeconds: server.restSeconds,
    targets: server.targets ?? state.targets,
    profile: server.profile ? { ...EMPTY_PROFILE, ...server.profile } : state.profile,
    history: [...server.history, ...pending, ...state.rejected].sort((a, b) => a.startedAt.localeCompare(b.startedAt)),
    pending,
  })
}

let flushing = false

/** Upload finished workouts the server has not accepted yet. Each POST is idempotent by client id. */
export async function flushPending(): Promise<void> {
  if (flushing) return
  flushing = true
  try {
    for (const w of [...state.pending]) {
      try {
        const saved = await api<Workout>('/workouts', { method: 'POST', body: JSON.stringify(w) })
        commit({
          ...state,
          history: state.history.map((h) => (h.id === w.id ? saved : h)),
          pending: state.pending.filter((p) => p.id !== w.id),
        })
      } catch (e) {
        // The server looked at it and said no (bad values): retrying won't help, set it aside.
        if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 401 && e.status !== 403) {
          commit({ ...state, pending: state.pending.filter((p) => p.id !== w.id), rejected: [...state.rejected, w] })
          continue
        }
        throw e
      }
    }
  } catch {
    // Offline, server down or login expired: keep them pending, the next sync retries.
  } finally {
    flushing = false
  }
}

/** Resolves to the server state after the update, or null when not in server mode or the request failed. */
function pushSettings(patch: SettingsPatch): Promise<ServerState | null> {
  if (state.mode !== 'server') return Promise.resolve(null)
  return api<ServerState>('/settings', { method: 'PUT', body: JSON.stringify(patch) })
    .then((server) => {
      applyServer(server)
      return server
    })
    .catch(() => null)
}

/**
 * Load settings and history from the server. Outside Telegram the server may still answer
 * (local dev with DEV_USER_ID); otherwise the app stays a local demo.
 */
export async function syncFromServer(): Promise<void> {
  try {
    while (flushing) await new Promise((r) => setTimeout(r, 100))
    await flushPending()
    applyServer(await api<ServerState>('/state'))
  } catch {
    if (!inTelegram && state.mode !== 'demo') commit({ ...state, mode: 'demo' })
  }
}
