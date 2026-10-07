// Pure parts of the active-workout sync: the store PUTs the workout in progress to the server
// (PUT /api/workouts/active) so the bot can see the sets already done before «Завершить».
// Type-only imports: this module stays free of window/fetch so tests run in node.
import type { Workout } from './store'

/** True once at least one set of the workout is ticked, i.e. the user really started training. */
export function isStarted(w: Workout): boolean {
  return w.exercises.some((e) => e.sets.some((s) => s.done))
}

/**
 * What the server should know about a workout in progress: its id, the exercise list and the done
 * sets with their numbers. Weights the app suggests for sets not done yet are left out, so refills
 * and edits of the plan do not cause a PUT.
 */
export function activeFingerprint(w: Workout): string {
  return JSON.stringify([
    w.id,
    w.exercises.map((e) => [e.name, e.sets.map((s) => (s.done ? [s.weight, s.reps] : 0))]),
  ])
}

/**
 * Whether a change of `state.active` from `prev` to `next` must reach the server: a set ticked or
 * unticked, a done set's weight or reps edited, an exercise added or removed, another workout. A
 * prepared workout nobody started stays local; once something was done, unticking the last set is
 * still sent so the server does not keep a stale set. `next` null is a finish (the POST clears the
 * server's copy) or a cancel (an explicit DELETE), never a PUT.
 */
export function shouldPushActive(prev: Workout | null, next: Workout | null): boolean {
  if (!next || prev === next) return false
  const nextStarted = isStarted(next)
  if (!prev || prev.id !== next.id) return nextStarted
  if (!nextStarted && !isStarted(prev)) return false
  return activeFingerprint(prev) !== activeFingerprint(next)
}

/** Body of PUT /api/workouts/active: the POST /api/workouts shape, all sets with their done flags. */
export function activePayload(w: Workout): Workout {
  return {
    id: w.id,
    programId: w.programId,
    week: w.week,
    weekday: w.weekday,
    startedAt: w.startedAt,
    finishedAt: null,
    exercises: w.exercises.map((e) => ({
      name: e.name,
      target: e.target,
      dropset: e.dropset,
      sets: e.sets.map((s) => ({ weight: s.weight, reps: s.reps, done: s.done })),
    })),
  }
}

/**
 * PUT answered by an older server without the endpoint (there only DELETE /workouts/{id} exists):
 * stop trying for this session. DELETE errors never switch the sync off.
 */
export function isUnsupported(status: number): boolean {
  return status === 404 || status === 405
}

/** What the server holds after our last successful PUT: the workout id and its activeFingerprint. */
export interface ActiveSent {
  id: string
  fp: string
}

/**
 * Whether a PUT of `w` tells the server something new. A repeat of the last accepted payload is
 * skipped, so a workout forgotten open on the phone does not look fresh to the bot on every sync.
 */
export function needsPut(w: Workout, sent: ActiveSent | null): boolean {
  return !sent || sent.id !== w.id || sent.fp !== activeFingerprint(w)
}

/** Keepalive requests (sent while the page is being hidden) are limited to 64 KB of body in total. */
export const KEEPALIVE_MAX_BYTES = 60_000

export function fitsKeepalive(body: string): boolean {
  return new TextEncoder().encode(body).length <= KEEPALIVE_MAX_BYTES
}

// ---- Deletes of the server's copy that have not gone through yet (cancel while offline) ----

/** At most this many ids wait for a retry; the server holds one copy, older ids rarely matter. */
export const MAX_PENDING_DELETES = 5

/** Adds `id` to the retry queue (once, newest last). */
export function queueDelete(queue: readonly string[], id: string): string[] {
  return [...queue.filter((x) => x !== id), id].slice(-MAX_PENDING_DELETES)
}

/**
 * What a DELETE /api/workouts/active answer means for the queue: 'done' drops the id, 'retry' keeps
 * it for the next sync. `status` undefined is a network error or timeout. 404/405/422 come from an
 * older server without the endpoint and other 4xx will not change on retry: both are 'done'.
 * Login problems (401/403), 408, 429 and server errors are retried.
 */
export function deleteOutcome(status: number | undefined): 'done' | 'retry' {
  if (status === undefined) return 'retry'
  if (status < 400) return 'done'
  if (status === 401 || status === 403 || status === 408 || status === 429 || status >= 500) return 'retry'
  return 'done'
}

/** The queue after a DELETE of `id` answered with `status` (see deleteOutcome). */
export function afterDelete(queue: readonly string[], id: string, status: number | undefined): string[] {
  return deleteOutcome(status) === 'retry' ? [...queue] : queue.filter((x) => x !== id)
}

// ---- Restoring the workout in progress from the server (GET /api/state `activeWorkout`) ----

/**
 * How many ended workout ids are remembered. A /state answer that left the server before a finish or
 * cancel still carries the workout as active; the tombstone keeps it from coming back. Ids are unique.
 */
export const MAX_ENDED = 10

/** Adds `id` to the ended list (once, newest last, bounded). */
export function endWorkoutId(ended: readonly string[], id: string): string[] {
  return [...ended.filter((x) => x !== id), id].slice(-MAX_ENDED)
}

/** Ids the server's snapshot must not come back under: cancelled (delete queued) or already finished. */
export interface RestoreSkip {
  deletes: readonly string[]
  /** Workouts this device finished or cancelled (endWorkoutId), kept even after the delete went through. */
  ended: readonly string[]
  /** Ids and client ids of history, pending and rejected workouts. */
  finished: ReadonlySet<string>
}

/**
 * The server's workout in progress to put back into the app, or null to keep the local one. It comes
 * back when the app lost its copy (storage wiped, another device) or holds only a prepared workout
 * nobody started under another id. A started local workout always wins (its next PUT replaces the
 * server's copy), as does the local copy of the same workout. A workout cancelled here (delete still
 * queued) or already finished never comes back.
 */
export function restoreActive(
  local: Workout | null,
  server: Workout | null | undefined,
  skip: RestoreSkip,
): Workout | null {
  if (!server || server.finishedAt) return null
  if (skip.deletes.includes(server.id) || skip.ended.includes(server.id) || skip.finished.has(server.id)) return null
  if (local && (isStarted(local) || local.id === server.id)) return null
  // Only the Workout fields: drop server extras (updatedAt, clientId).
  return { ...activePayload(server), finishedAt: null }
}
