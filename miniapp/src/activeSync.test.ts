// Active workout sync: which changes of the workout in progress are PUT to the server, and in what shape.
import { describe, expect, it } from 'vitest'
import {
  KEEPALIVE_MAX_BYTES,
  MAX_ENDED,
  MAX_PENDING_DELETES,
  activeFingerprint,
  activePayload,
  afterDelete,
  deleteOutcome,
  endWorkoutId,
  fitsKeepalive,
  isStarted,
  isUnsupported,
  needsPut,
  queueDelete,
  restoreActive,
  shouldPushActive,
  type RestoreSkip,
} from './activeSync'
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

describe('needsPut', () => {
  const sent = (w: Workout) => ({ id: w.id, fp: activeFingerprint(w) })

  it('sends a workout the server has not accepted yet', () => {
    expect(needsPut(started(), null)).toBe(true)
  })
  it('skips a repeat of the last accepted payload (a forgotten workout stays stale for the bot)', () => {
    const w = started()
    expect(needsPut(w, sent(w))).toBe(false)
    // Suggested weights of sets not done are not part of the payload's meaning.
    expect(needsPut(edit(w, 1, 0, { weight: 45 }), sent(w))).toBe(false)
  })
  it('sends a real change or another workout', () => {
    const w = started()
    expect(needsPut(edit(w, 0, 1, { reps: 8, done: true }), sent(w))).toBe(true)
    expect(needsPut(edit(w, 0, 0, { weight: 82.5 }), sent(w))).toBe(true)
    expect(needsPut(edit(w, 0, 0, { done: false }), sent(w))).toBe(true)
    const other = edit(workout(undefined, 'w2'), 0, 0, { reps: 8, done: true })
    expect(needsPut(other, sent(w))).toBe(true)
  })
})

describe('fitsKeepalive', () => {
  it('limits the body to the keepalive quota in bytes', () => {
    expect(fitsKeepalive(JSON.stringify(activePayload(started())))).toBe(true)
    expect(fitsKeepalive('x'.repeat(KEEPALIVE_MAX_BYTES))).toBe(true)
    expect(fitsKeepalive('x'.repeat(KEEPALIVE_MAX_BYTES + 1))).toBe(false)
    // Cyrillic is two bytes per letter in UTF-8.
    expect(fitsKeepalive('ж'.repeat(KEEPALIVE_MAX_BYTES / 2 + 1))).toBe(false)
  })
})

describe('delete retry queue', () => {
  it('queues an id once, newest last, and keeps a bounded tail', () => {
    expect(queueDelete([], 'a')).toEqual(['a'])
    expect(queueDelete(['a', 'b'], 'a')).toEqual(['b', 'a'])
    const many = Array.from({ length: MAX_PENDING_DELETES }, (_, i) => `w${i}`)
    const q = queueDelete(many, 'new')
    expect(q).toHaveLength(MAX_PENDING_DELETES)
    expect(q[0]).toBe('w1')
    expect(q[q.length - 1]).toBe('new')
  })

  it('keeps the id on network errors, timeouts, login problems and server errors', () => {
    for (const s of [undefined, 401, 403, 408, 429, 500, 502, 503]) {
      expect(deleteOutcome(s)).toBe('retry')
      expect(afterDelete(['a', 'b'], 'a', s)).toEqual(['a', 'b'])
    }
  })

  it('drops the id once the server answered for good', () => {
    // 204 done; 404/405/422 an older server without the endpoint; 400 will not change on retry.
    for (const s of [200, 204, 400, 404, 405, 422]) {
      expect(deleteOutcome(s)).toBe('done')
      expect(afterDelete(['a', 'b'], 'a', s)).toEqual(['b'])
    }
  })

  it('cancel offline, then two syncs: the id stays until the server confirms', () => {
    let q = queueDelete([], 'w1') // cancelWorkout while offline
    q = afterDelete(q, 'w1', undefined) // first attempt: network error
    expect(q).toEqual(['w1'])
    q = afterDelete(q, 'w1', 502) // retry on the next sync: proxy error
    expect(q).toEqual(['w1'])
    q = afterDelete(q, 'w1', 204) // back online
    expect(q).toEqual([])
  })
})

describe('restoreActive', () => {
  const none: RestoreSkip = { deletes: [], ended: [], finished: new Set() }
  const serverCopy = () => ({ ...edit(workout(undefined, 'srv'), 0, 0, { reps: 8, done: true }), updatedAt: '2026-10-07T09:00:00Z' })

  it('nothing on the server: keep the local state', () => {
    expect(restoreActive(started(), null, none)).toBeNull()
    expect(restoreActive(null, undefined, none)).toBeNull()
  })

  it('the app lost its workout (storage wiped, another device): take the server copy back', () => {
    const r = restoreActive(null, serverCopy(), none)
    expect(r?.id).toBe('srv')
    expect(r?.exercises[0].sets[0]).toEqual({ weight: 80, reps: 8, done: true })
    expect(r?.finishedAt).toBeNull()
    expect(r && 'updatedAt' in r).toBe(false)
  })

  it('replaces a freshly prepared, untouched workout under another id', () => {
    expect(restoreActive(workout(), serverCopy(), none)?.id).toBe('srv')
  })

  it('never overwrites a started local workout', () => {
    expect(restoreActive(started(), serverCopy(), none)).toBeNull()
  })

  it('keeps the local copy of the same workout, even with no set done', () => {
    const local = workout(undefined, 'srv')
    expect(restoreActive(local, serverCopy(), none)).toBeNull()
  })

  it('a workout cancelled here (delete still queued) does not come back', () => {
    expect(restoreActive(null, serverCopy(), { deletes: ['srv'], ended: [], finished: new Set() })).toBeNull()
    expect(restoreActive(workout(), serverCopy(), { deletes: ['other', 'srv'], ended: [], finished: new Set() })).toBeNull()
  })

  it('a finished workout (in history, pending or rejected) does not come back', () => {
    expect(restoreActive(null, serverCopy(), { deletes: [], ended: [], finished: new Set(['srv']) })).toBeNull()
  })

  it('a stale snapshot of a workout ended here does not come back after the delete went through', () => {
    // GET /state left the server before the finish POST / cancel DELETE: history lacks it, queue empty.
    expect(restoreActive(null, serverCopy(), { deletes: [], ended: ['srv'], finished: new Set() })).toBeNull()
  })

  it('remembers a bounded list of ended ids', () => {
    expect(endWorkoutId(['a', 'b'], 'a')).toEqual(['b', 'a'])
    const many = Array.from({ length: MAX_ENDED }, (_, i) => `w${i}`)
    const e = endWorkoutId(many, 'new')
    expect(e).toHaveLength(MAX_ENDED)
    expect(e[e.length - 1]).toBe('new')
    expect(e).not.toContain('w0')
  })

  it('ignores a snapshot marked finished', () => {
    expect(restoreActive(null, { ...serverCopy(), finishedAt: '2026-10-07T10:00:00Z' }, none)).toBeNull()
  })
})
