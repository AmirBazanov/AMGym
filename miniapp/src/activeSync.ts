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
