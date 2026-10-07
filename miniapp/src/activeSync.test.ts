// Active workout sync: which changes of the workout in progress are PUT to the server, and in what shape.
import { describe, expect, it } from 'vitest'
import { activePayload, isStarted, isUnsupported, shouldPushActive } from './activeSync'
import type { SetEntry, Workout } from './store'

const set = (weight: number | null, reps: number | null = null, done = false): SetEntry => ({ weight, reps, done })

function workout(sets: SetEntry[][] = [[set(80), set(80)], [set(40), set(40)]], id = 'w1'): Workout {
  return {
    id,
    programId: 'p',
    week: 2,
    weekday: 3,
    startedAt: '2026-10-07T08:00:00.000Z',
    finishedAt: null,
    exercises: sets.map((s, i) => ({ name: ['Жим лёжа', 'Тяга'][i] ?? `Ex ${i}`, target: '3х8-10', dropset: false, sets: s })),
  }
}

/** Immutable edit of one set, as store.updateSet does. */
function edit(w: Workout, ex: number, s: number, patch: Partial<SetEntry>): Workout {
  return {
    ...w,
    exercises: w.exercises.map((e, i) =>
      i === ex ? { ...e, sets: e.sets.map((x, j) => (j === s ? { ...x, ...patch } : x)) } : e,
    ),
  }
}

const started = () => edit(workout(), 0, 0, { reps: 8, done: true })

describe('isStarted', () => {
  it('needs a ticked set', () => {
    expect(isStarted(workout())).toBe(false)
    expect(isStarted(started())).toBe(true)
  })
})

describe('shouldPushActive', () => {
  it('never PUTs when there is no workout after the change (finish POSTs, cancel DELETEs)', () => {
    expect(shouldPushActive(started(), null)).toBe(false)
    expect(shouldPushActive(null, null)).toBe(false)
  })

  it('ignores the same object', () => {
    const w = started()
    expect(shouldPushActive(w, w)).toBe(false)
  })

  it('keeps a prepared workout local', () => {
    expect(shouldPushActive(null, workout())).toBe(false)
    // Weight edits, plan rebuilds and refills before the first tick.
    expect(shouldPushActive(workout(), edit(workout(), 0, 0, { weight: 85 }))).toBe(false)
    expect(shouldPushActive(workout(), workout([[set(70)]]))).toBe(false)
  })

  it('pushes the first ticked set', () => {
    expect(shouldPushActive(workout(), started())).toBe(true)
  })

  it('pushes ticking and unticking, including the last done set', () => {
    const w = started()
    expect(shouldPushActive(w, edit(w, 1, 0, { reps: 10, done: true }))).toBe(true)
    expect(shouldPushActive(w, edit(w, 0, 0, { done: false }))).toBe(true)
  })

  it('pushes weight and reps edits of a done set', () => {
    const w = started()
    expect(shouldPushActive(w, edit(w, 0, 0, { weight: 82.5 }))).toBe(true)
    expect(shouldPushActive(w, edit(w, 0, 0, { reps: 9 }))).toBe(true)
  })

  it('ignores edits of sets not done yet (app refills and suggestions)', () => {
    const w = started()
    expect(shouldPushActive(w, edit(w, 0, 1, { weight: 85 }))).toBe(false)
    expect(shouldPushActive(w, edit(w, 1, 1, { weight: 45, reps: 12 }))).toBe(false)
  })

  it('pushes added and removed exercises once started', () => {
    const w = started()
    const added = { ...w, exercises: [...w.exercises, { name: 'Подъём на бицепс', target: '', dropset: false, sets: [set(20)] }] }
    expect(shouldPushActive(w, added)).toBe(true)
    expect(shouldPushActive(w, { ...w, exercises: w.exercises.slice(0, 1) })).toBe(true)
    // Before the first tick it is still a prepared workout.
    const prepared = workout()
    expect(shouldPushActive(prepared, { ...prepared, exercises: prepared.exercises.slice(0, 1) })).toBe(false)
  })

  it('pushes another workout only when it is started', () => {
    const w = started()
    expect(shouldPushActive(w, { ...started(), id: 'w2' })).toBe(true)
    expect(shouldPushActive(w, workout(undefined, 'w2'))).toBe(false)
    expect(shouldPushActive(null, started())).toBe(true)
  })
})

describe('activePayload', () => {
  it('is the POST /api/workouts shape with every set and its done flag', () => {
    const w = { ...edit(started(), 0, 1, { weight: null }), clientId: 'x', finishedAt: '2026-10-07T09:00:00.000Z' }
    const extra = { ...w, exercises: w.exercises.map((e) => ({ ...e, sets: e.sets.map((s) => ({ ...s, note: 'x' })) })) }
    expect(activePayload(extra)).toEqual({
      id: 'w1',
      programId: 'p',
      week: 2,
      weekday: 3,
      startedAt: '2026-10-07T08:00:00.000Z',
      finishedAt: null,
      exercises: [
        {
          name: 'Жим лёжа',
          target: '3х8-10',
          dropset: false,
          sets: [
            { weight: 80, reps: 8, done: true },
            { weight: null, reps: null, done: false },
          ],
        },
        {
          name: 'Тяга',
          target: '3х8-10',
          dropset: false,
          sets: [
            { weight: 40, reps: null, done: false },
            { weight: 40, reps: null, done: false },
          ],
        },
      ],
    })
  })

  it('does not share objects with the store state', () => {
    const w = started()
    const p = activePayload(w)
    expect(p.exercises[0]).not.toBe(w.exercises[0])
    expect(p.exercises[0].sets[0]).not.toBe(w.exercises[0].sets[0])
  })
})

describe('isUnsupported', () => {
  it('turns the sync off only for a missing endpoint', () => {
    expect(isUnsupported(404)).toBe(true)
    expect(isUnsupported(405)).toBe(true)
    for (const s of [400, 401, 403, 422, 500, 502]) expect(isUnsupported(s)).toBe(false)
  })
})
