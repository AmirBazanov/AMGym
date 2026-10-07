// Local-only state until the stage 2 API exists: everything lives in localStorage.
// Shapes mirror the future server models (Workout -> exercises -> sets) so the swap is mechanical.
import { useSyncExternalStore } from 'react'
import { getDay, getProgram, isDropset } from './program'
import {
  api,
  ApiError,
  EMPTY_PROFILE,
  EMPTY_TARGETS,
  inTelegram,
  timeoutSignal,
  type DayPlan,
  type Profile,
  type Targets,
} from './api'
import { buildDemoHistory, demoStartDate } from './mock'
import {
  applyPlan,
  overridesForWorkout,
  planKey,
  todaysProgramDay,
  refillSuggestions,
  suggestionsOf,
  type AdjustedExercise,
  type PlanMode,
  type PreparedSuggestions,
} from './plan'
import { findOverride, lastSameSession, mergeOverrides, normalizeBaselines } from './progression'
import {
  activeFingerprint,
  activePayload,
  afterDelete,
  fitsKeepalive,
  isStarted,
  isUnsupported,
  needsPut,
  endWorkoutId,
  queueDelete,
  restoreActive,
  shouldPushActive,
  type ActiveSent,
} from './activeSync'
import { onBackground } from './telegram'

export { isStarted }

export interface SetEntry {
  weight: number | null // kg
  reps: number | null
  done: boolean
}

/** The owner's own words about a lift ("жим 90 на 8"), newest per exercise; from GET /api/state. */
export interface Baseline {
  exercise: string // exact program/catalog exercise name
  weightKg: number
  reps: number | null // null: weight without reps ("~100 смогу"), counted as the max
  factId: number
}

/** Today's working weight the owner set in the bot chat («поставь сегодня жим 85»); from GET /api/state. */
export interface WeightOverride {
  exercise: string // exact program exercise name
  weightKg: number
  date: string // YYYY-MM-DD, the server's local day (server TIMEZONE = phone time zone, as for the day plan)
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
  // "Как в программе" / "С поправкой" for the adaptive day plan; only counts on `date` (local).
  planChoice: { date: string; mode: PlanMode } | null
  // planKey() the not-yet-started active workout was built from ('program' = as written).
  activePlanKey: string | null
  // Starting weights from the owner's words (server data; none in the demo).
  baselines: Baseline[]
  // What the app filled into the active workout, so new baselines and overrides refill it in place.
  activeSuggested: PreparedSuggestions
  // Weights the owner set for a day in the chat (server data; none in the demo). Only today's count.
  weightOverrides: WeightOverride[]
  // The workout in progress as the server last accepted it (PUT /api/workouts/active); null: none sent.
  activeSent: ActiveSent | null
  // Workouts whose server copy must still be deleted (cancelled offline); retried on every sync.
  pendingActiveDeletes: string[]
  // Ids of workouts finished or cancelled here, so a stale server snapshot never restores them.
  endedActive: string[]
}

interface ServerState {
  programId: string
  startDate: string
  restSeconds: number
  history: Workout[]
  targets?: Targets // absent on servers older than the nutrition API
  profile?: Partial<Profile> // absent on servers older than the profile API
  baselines?: Baseline[] // absent on servers older than the baselines
  weightOverrides?: WeightOverride[] // absent on servers older than the weight overrides
  // The workout in progress as last PUT from any device (fresh and unfinished only); absent on older servers.
  activeWorkout?: (Workout & { updatedAt?: string }) | null
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
    planChoice: null,
    activePlanKey: null,
    baselines: [],
    activeSuggested: {},
    weightOverrides: [],
    activeSent: null,
    pendingActiveDeletes: [],
    endedActive: [],
  }
}

function load(): State {
  try {
    const raw = localStorage.getItem(KEY)
    if (raw) {
      const saved = JSON.parse(raw) as Partial<State>
      // A browser preview (demo mode) may share this origin's storage with Telegram Web. Inside
      // Telegram the history is the server's: never restore demo workouts or a demo session.
      if (inTelegram && saved.mode === 'demo') {
        delete saved.history
        delete saved.pending
        delete saved.rejected
        delete saved.active
        delete saved.mode
        delete saved.baselines
        delete saved.activeSuggested
        delete saved.weightOverrides
        delete saved.activeSent
        delete saved.pendingActiveDeletes
        delete saved.endedActive
      }
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
  const prev = state
  state = next
  if (next.mode === 'server' && shouldPushActive(prev.active, next.active)) scheduleActivePush()
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

function plannedLog(adj: AdjustedExercise): ExerciseLog {
  // Record-based weight or double progression, whichever is higher (see progression.ts), × the plan factor.
  const ex = adj.exercise
  return {
    name: ex.name,
    target: adj.changed ? adj.target : adj.original.prescription.raw,
    dropset: isDropset(ex.prescription),
    sets: Array.from({ length: ex.prescription.sets }, () => ({ weight: adj.weight, reps: null, done: false })),
  }
}

/** Today's choice for the adaptive plan; a new day starts with the correction on. */
export function planMode(s: State = state): PlanMode {
  return s.planChoice?.date === localDate() ? s.planChoice.mode : 'adjusted'
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

/** Today's overrides for the active workout (plan.overridesForWorkout); Today uses it for the hints. */
export function overridesFor(w: Workout, s: State = state): readonly WeightOverride[] {
  return overridesForWorkout(s.weightOverrides, w)
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
    const next = todaysProgramDay(getProgram(state.programId), state.startDate, currentRun())
    if (next) actions.startWorkout(next.week, next.weekday)
  },

  startWorkout(week: number, weekday: number) {
    const program = getProgram(state.programId)
    const day = getDay(program, week, weekday)
    if (!day) return
    // Started now, so trained today: today's overrides count whatever program day it is.
    const res = applyPlan(day, null, state.history, localDate(), week, state.baselines, state.weightOverrides)
    commit({
      ...state,
      active: {
        id: crypto.randomUUID(),
        programId: program.id,
        week,
        weekday,
        startedAt: new Date().toISOString(),
        finishedAt: null,
        exercises: res.exercises.map(plannedLog),
      },
      activePlanKey: 'program',
      activeSuggested: suggestionsOf(res.exercises),
    })
  },

  setPlanMode(mode: PlanMode) {
    commit({ ...state, planChoice: { date: localDate(), mode } })
  },

  /**
   * Rebuilds the prepared workout from the adaptive plan (or back from the program) while no set is
   * ticked. prepareToday runs before the plan is fetched, so Today calls this whenever the plan or the
   * mode changes. Idempotent on the plan's content: a refetch of the same plan keeps weight edits.
   * A started workout is never touched. New baselines and overrides do not rebuild it: applyServer
   * refills it in place.
   */
  applyDayPlan(plan: DayPlan | null) {
    const a = state.active
    if (!a || isStarted(a)) return
    const day = getDay(getProgram(a.programId), a.week, a.weekday)
    if (!day) return
    const res = applyPlan(
      day,
      planMode() === 'adjusted' ? plan : null,
      state.history,
      localDate(),
      a.week,
      state.baselines,
      overridesFor(a),
    )
    const key = res.applied ? planKey(plan) : 'program'
    if ((state.activePlanKey ?? 'program') === key) return
    commit({
      ...state,
      activePlanKey: key,
      active: { ...a, exercises: res.exercises.map(plannedLog) },
      activeSuggested: suggestionsOf(res.exercises),
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
    // The owner's number for today first, else last time's top weight.
    const last = lastSetsFor(name)
    const override = findOverride(overridesFor(a), name, localDate())
    const weight = override ? override.weightKg : last ? Math.max(...last.map((s) => s.weight ?? 0)) : null
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
    cancelActivePush()
    const endedActive = endWorkoutId(state.endedActive, a.id)
    if (!exercises.length) {
      commit({ ...state, active: null, activeSuggested: {}, endedActive })
      // Everything was unticked: the server may still hold an earlier PUT of this workout.
      if (state.mode === 'server') requestActiveDelete(a.id)
      return
    }
    const done: Workout = { ...a, exercises, finishedAt: new Date().toISOString() }
    const server = state.mode === 'server'
    commit({
      ...state,
      active: null,
      activeSuggested: {},
      endedActive,
      history: [...state.history, done],
      pending: server ? [...state.pending, done] : state.pending,
    })
    if (server) void flushPending()
  },

  cancelWorkout() {
    const id = state.active?.id
    cancelActivePush()
    commit({
      ...state,
      active: null,
      activeSuggested: {},
      skipAutoStart: localDate(),
      endedActive: id ? endWorkoutId(state.endedActive, id) : state.endedActive,
    })
    if (state.mode === 'server' && id) requestActiveDelete(id)
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
  // Absent field (older server): keep what we have, so the refill below has nothing to clear.
  const baselines = server.baselines === undefined ? state.baselines : normalizeBaselines(server.baselines)
  const next: State = {
    ...state,
    mode: 'server',
    programId: server.programId,
    startDate: server.startDate,
    restSeconds: server.restSeconds,
    targets: server.targets ?? state.targets,
    profile: server.profile ? { ...EMPTY_PROFILE, ...server.profile } : state.profile,
    history: [...server.history, ...pending, ...state.rejected].sort((a, b) => a.startedAt.localeCompare(b.startedAt)),
    pending,
    baselines,
    weightOverrides: mergeOverrides(server.weightOverrides, state.weightOverrides),
  }
  // The app lost the workout in progress (storage wiped, another device) or prepared a fresh empty one:
  // take the server's copy back with its ticked sets. Its weights stay; the refill below fills only empty ones.
  const finished = new Set(next.history.flatMap((w) => (w.clientId ? [w.id, w.clientId] : [w.id])))
  const restored = restoreActive(next.active, server.activeWorkout, {
    deletes: next.pendingActiveDeletes,
    ended: next.endedActive,
    finished,
  })
  if (restored) {
    next.active = restored
    next.activeSuggested = {}
    next.activePlanKey = null
    // The server holds exactly this: no PUT until something changes.
    next.activeSent = { id: restored.id, fp: activeFingerprint(restored) }
  } else if (next.active && known.has(next.active.id)) {
    // Finished on another device: the server already has it in the history. Keeping it here would show a
    // workout in progress forever, and sets ticked here would be dropped by the idempotent POST.
    next.active = null
    next.activeSuggested = {}
    next.activePlanKey = null
    next.activeSent = null
  }
  // New baselines reach the prepared workout, and today's overrides also a started one, in place (empty
  // or app-suggested weights of sets not done only), whether or not the day plan request succeeds.
  // Idempotent, so every sync may run it.
  const a = next.active
  const refill = a
    ? refillSuggestions(
        a,
        next.activeSuggested,
        getDay(getProgram(a.programId), a.week, a.weekday),
        next.history,
        baselines,
        overridesFor(a, next),
        localDate(),
      )
    : null
  commit(refill ? { ...next, active: refill.workout, activeSuggested: refill.suggested } : next)
}

let flushing = false

/** Upload finished workouts the server has not accepted yet. Each POST is idempotent by client id. */
export async function flushPending(): Promise<void> {
  if (flushing) return
  flushing = true
  try {
    // A PUT of the workout in progress still on its way must not land after the POST that clears it.
    // Each request times out by itself; the race only guards against a chain that never settles.
    await Promise.race([activeInflight, new Promise((r) => setTimeout(r, ACTIVE_TIMEOUT_MS))])
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
          // The refused POST did not clear the server's copy of the workout in progress: drop it.
          requestActiveDelete(w.id)
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
    // Deletes that failed earlier (cancelled offline), then the workout in progress if an earlier PUT
    // was lost. An unchanged one is not sent again (needsPut), so live-update syncs do not loop.
    retryActiveDeletes()
    if (activeOnServer(state)) void pushActive()
  } catch {
    if (!inTelegram && state.mode !== 'demo') commit({ ...state, mode: 'demo' })
  }
}

// ---- Workout in progress on the server (PUT/DELETE /api/workouts/active), so the bot sees it ----
// Fire-and-forget and last-write-wins: the timer sends whatever `state.active` is when it fires,
// errors are dropped and the next change or foreground sync sends the latest again.

const ACTIVE_DEBOUNCE_MS = 1500
/** PUT/DELETE of the workout in progress give up after this, so a hung request never blocks a finish. */
const ACTIVE_TIMEOUT_MS = 10_000
let activeTimer: ReturnType<typeof setTimeout> | null = null
let activeInflight: Promise<void> = Promise.resolve()
let activeUnsupported = false // an older server without the endpoints: off for this session

/** The active workout should be on the server: started, or already sent (then unticked). */
function activeOnServer(s: State): boolean {
  return s.mode === 'server' && !!s.active && (isStarted(s.active) || s.active.id === s.activeSent?.id)
}

function scheduleActivePush() {
  if (activeUnsupported) return
  if (activeTimer) clearTimeout(activeTimer)
  activeTimer = setTimeout(() => {
    activeTimer = null
    void pushActive()
  }, ACTIVE_DEBOUNCE_MS)
}

function cancelActivePush() {
  if (activeTimer) clearTimeout(activeTimer)
  activeTimer = null
}

/**
 * Requests are chained, so a DELETE or the finish POST never overtakes an earlier PUT. A payload the
 * server already accepted is not sent again. `keepalive`: the page is being hidden, let the request
 * outlive it (bodies over the keepalive limit go as a normal request).
 */
function pushActive({ keepalive = false }: { keepalive?: boolean } = {}): Promise<void> {
  activeInflight = activeInflight.then(async () => {
    const a = state.active
    if (activeUnsupported || !a || !activeOnServer(state) || !needsPut(a, state.activeSent)) return
    const body = JSON.stringify(activePayload(a))
    const fp = activeFingerprint(a)
    try {
      await api<void>('/workouts/active', {
        method: 'PUT',
        body,
        keepalive: keepalive && fitsKeepalive(body),
        signal: timeoutSignal(ACTIVE_TIMEOUT_MS),
      })
      commit({ ...state, activeSent: { id: a.id, fp } })
    } catch (e) {
      if (e instanceof ApiError && isUnsupported(e.status)) activeUnsupported = true
    }
  })
  return activeInflight
}

/** Cancel or finish with nothing done: remember the delete until the server confirms it, then try it. */
function requestActiveDelete(id: string) {
  commit({ ...state, pendingActiveDeletes: queueDelete(state.pendingActiveDeletes, id) })
  deleteActive(id)
}

/** Deletes still waiting from an earlier session or a failed attempt (offline cancel). */
function retryActiveDeletes() {
  if (state.mode !== 'server') return
  state.pendingActiveDeletes.forEach(deleteActive)
}

/**
 * `id`: the server deletes only its copy of this workout, so a late cancel never wipes a newer one.
 * The id stays queued in `pendingActiveDeletes` until the server answers (see deleteOutcome).
 */
function deleteActive(id: string) {
  activeInflight = activeInflight.then(async () => {
    if (!state.pendingActiveDeletes.includes(id)) return // done by an earlier attempt in the chain
    let status: number | undefined
    if (activeUnsupported) status = 404 // an older server never got a PUT: nothing to delete
    else try {
      await api<void>(`/workouts/active?clientId=${encodeURIComponent(id)}`, {
        method: 'DELETE',
        signal: timeoutSignal(ACTIVE_TIMEOUT_MS),
      })
      status = 204
    } catch (e) {
      status = e instanceof ApiError ? e.status : undefined
    }
    const pendingActiveDeletes = afterDelete(state.pendingActiveDeletes, id, status)
    if (pendingActiveDeletes.length === state.pendingActiveDeletes.length) return
    commit({
      ...state,
      pendingActiveDeletes,
      activeSent: state.activeSent?.id === id ? null : state.activeSent,
    })
  })
}

// Telegram may suspend a minimized Mini App before the debounce fires; the owner then asks the bot
// about the sets just done. Send a waiting change right away when the page is hidden or minimized.
if (typeof document !== 'undefined') {
  onBackground(() => {
    if (!activeTimer) return
    cancelActivePush()
    void pushActive({ keepalive: true })
  })
}
