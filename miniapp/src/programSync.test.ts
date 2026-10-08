import { afterEach, describe, expect, it } from 'vitest'
import type { CatalogExercise, ProgramOut } from './api'
import {
  buildPrepared,
  cacheProgram,
  exerciseNamesWithHistory,
  needsProgramFetch,
  pickerNames,
  preparePick,
  programFromServer,
  rebuildDecision,
  sameDayContent,
} from './programSync'
import { PROGRAMS, setServerPrograms, type Program, type ProgramDay, type ProgramExercise } from './program'
import type { Workout } from './store'

afterEach(() => setServerPrograms({}))

const BUNDLED = PROGRAMS[0]
const SLUG = BUNDLED.id
const COPY_SLUG = 'arms_specialization_8w-u1'

/** The bundled program as the server would send it: fake ids on days and exercises. */
function toServer(p: Program, over: Partial<ProgramOut> = {}): ProgramOut {
  let id = 100
  return {
    id: p.id,
    name: p.name,
    source: p.source,
    version: 1,
    editable: false,
    basedOn: null,
    weeks: p.weeks.map((w) => ({
      number: w.number,
      days: w.days.map((d) => ({
        id: ++id,
        weekday: d.weekday,
        title: d.title,
        focus: d.focus ?? null,
        exercises: d.exercises.map((e) => ({
          id: ++id,
          name: e.name,
          intensity: e.intensity,
          order: e.order,
          prescription: { ...e.prescription },
        })),
      })),
    })),
    ...over,
  }
}

function stripServerFields(p: Program): unknown {
  return {
    id: p.id,
    name: p.name,
    source: p.source,
    weeks: p.weeks.map((w) => ({
      number: w.number,
      days: w.days.map((d) => {
        const { id: _dayId, ...day } = d
        void _dayId
        return {
          ...day,
          exercises: d.exercises.map((e) => {
            const { id: _exId, ...ex } = e
            void _exId
            return ex
          }),
        }
      }),
    })),
  }
}

describe('programFromServer', () => {
  it('keeps ids, focus, version, editable, basedOn', () => {
    const out = toServer(BUNDLED, { id: COPY_SLUG, version: 3, editable: true, basedOn: SLUG })
    const p = programFromServer(out)
    expect(p.id).toBe(COPY_SLUG)
    expect(p.version).toBe(3)
    expect(p.editable).toBe(true)
    expect(p.basedOn).toBe(SLUG)
    const d0 = p.weeks[0].days[0]
    expect(d0.id).toBe(out.weeks[0].days[0].id)
    expect(d0.focus).toBe('Руки и плечи')
    expect(d0.exercises[0].id).toBe(out.weeks[0].days[0].exercises[0].id)
  })

  it('maps a null source to an empty string and keeps a null basedOn', () => {
    const p = programFromServer(toServer(BUNDLED, { source: null }))
    expect(p.source).toBe('')
    expect(p.basedOn).toBeNull()
  })

  it('keeps a null day focus as null', () => {
    const out = toServer(BUNDLED)
    out.weeks[0].days[0].focus = null
    expect(programFromServer(out).weeks[0].days[0].focus).toBeNull()
  })

  it('drops unknown extra keys', () => {
    const out = toServer(BUNDLED) as unknown as Record<string, unknown>
    out.secret = 'x'
    const weeks = out.weeks as { extraW?: number; days: { extraD?: number; exercises: { extraE?: number; prescription: { extraP?: number } }[] }[] }[]
    weeks[0].extraW = 1
    weeks[0].days[0].extraD = 2
    weeks[0].days[0].exercises[0].extraE = 3
    weeks[0].days[0].exercises[0].prescription.extraP = 4
    const p = programFromServer(out as unknown as ProgramOut)
    const text = JSON.stringify(p)
    expect(p).not.toHaveProperty('secret')
    expect(text).not.toContain('secret')
    expect(text).not.toContain('extraW')
    expect(text).not.toContain('extraD')
    expect(text).not.toContain('extraE')
    expect(text).not.toContain('extraP')
  })

  it('round trip: a server program with the bundled content equals the bundled program', () => {
    const p = programFromServer(toServer(BUNDLED))
    expect(p.weeks.length).toBe(BUNDLED.weeks.length)
    expect(stripServerFields(p)).toEqual(stripServerFields(BUNDLED))
    // Every day and exercise got an id on the way.
    for (const w of p.weeks)
      for (const d of w.days) {
        expect(typeof d.id).toBe('number')
        for (const e of d.exercises) expect(typeof e.id).toBe('number')
      }
  })

  it('round trip trains identically: same day content and same prepared workout', () => {
    const p = programFromServer(toServer(BUNDLED))
    for (let wi = 0; wi < BUNDLED.weeks.length; wi++)
      for (let di = 0; di < BUNDLED.weeks[wi].days.length; di++)
        expect(sameDayContent(p.weeks[wi].days[di], BUNDLED.weeks[wi].days[di])).toBe(true)
  })
})

describe('needsProgramFetch', () => {
  const cached = (version?: number): Program => ({ ...BUNDLED, id: 'a', ...(version === undefined ? {} : { version }) })

  it('is false when the server reported no version', () => {
    expect(needsProgramFetch({}, 'a', null)).toBe(false)
    expect(needsProgramFetch({}, 'a', undefined)).toBe(false)
    expect(needsProgramFetch({ a: cached(1) }, 'a', null)).toBe(false)
  })

  it('is true when the slug is not cached', () => {
    expect(needsProgramFetch({}, 'a', 1)).toBe(true)
    expect(needsProgramFetch({ b: cached(1) }, 'a', 1)).toBe(true)
  })

  it('is false for the same version, true for another', () => {
    expect(needsProgramFetch({ a: cached(2) }, 'a', 2)).toBe(false)
    expect(needsProgramFetch({ a: cached(2) }, 'a', 3)).toBe(true)
    expect(needsProgramFetch({ a: cached(3) }, 'a', 2)).toBe(true)
  })

  it('is true for a cached program without a version', () => {
    expect(needsProgramFetch({ a: cached() }, 'a', 1)).toBe(true)
  })

  it('treats prototype keys as missing', () => {
    expect(needsProgramFetch({}, 'toString', 1)).toBe(true)
    expect(needsProgramFetch({}, '__proto__', 1)).toBe(true)
    expect(needsProgramFetch({}, 'constructor', 1)).toBe(true)
  })
})

describe('cacheProgram', () => {
  const prog = (id: string, version = 1): Program => ({ ...BUNDLED, id, version })

  it('adds a program to an empty cache', () => {
    const p = prog('a')
    expect(cacheProgram({}, p, [])).toEqual({ a: p })
  })

  it('replaces the same slug', () => {
    const out = cacheProgram({ a: prog('a', 1) }, prog('a', 2), ['a'])
    expect(Object.keys(out)).toEqual(['a'])
    expect(out.a.version).toBe(2)
  })

  it('drops slugs that are not in keep and keeps those that are', () => {
    const cache = { a: prog('a'), b: prog('b'), c: prog('c') }
    const out = cacheProgram(cache, prog('d'), ['b'])
    expect(Object.keys(out).sort()).toEqual(['b', 'd'])
    expect(out.b).toBe(cache.b)
  })

  it('ignores null and undefined in keep', () => {
    const cache = { a: prog('a'), b: prog('b') }
    const out = cacheProgram(cache, prog('c'), [null, undefined, 'a'])
    expect(Object.keys(out).sort()).toEqual(['a', 'c'])
  })

  it('does not mutate the input', () => {
    const cache = { a: prog('a'), b: prog('b') }
    const snapshot = { ...cache }
    const out = cacheProgram(cache, prog('c'), ['a'])
    expect(cache).toEqual(snapshot)
    expect(Object.keys(cache).sort()).toEqual(['a', 'b'])
    expect(out).not.toBe(cache)
  })
})

/** The copy of the bundled program the user made: new slug, day ids, a renamed first exercise of the first day. */
function userCopy(): Program {
  const p = programFromServer(toServer(BUNDLED, { id: COPY_SLUG, version: 2, editable: true, basedOn: SLUG }))
  p.weeks[0].days[0].exercises[0] = { ...p.weeks[0].days[0].exercises[0], name: 'сгибания молотком' }
  return p
}

const MONDAY_NOON = new Date(2026, 9, 5, 12)

describe('preparePick', () => {
  it('is null on a cold start when the copy is not loaded, even on a training day', () => {
    setServerPrograms({})
    expect(preparePick({ programId: COPY_SLUG, startDate: '2026-10-05', run: [], now: MONDAY_NOON })).toBeNull()
  })

  it('still picks the bundled template when it is the active program', () => {
    setServerPrograms({})
    const pick = preparePick({ programId: SLUG, startDate: '2026-10-05', run: [], now: MONDAY_NOON })
    expect(pick?.program).toBe(BUNDLED)
    expect(pick).toMatchObject({ week: 1, weekday: 1 })
  })

  it('picks the loaded copy and its day', () => {
    const copy = userCopy()
    setServerPrograms({ [COPY_SLUG]: copy })
    const pick = preparePick({ programId: COPY_SLUG, startDate: '2026-10-05', run: [], now: MONDAY_NOON })
    expect(pick).not.toBeNull()
    expect(pick!.program).toBe(copy)
    expect(pick!.week).toBe(1)
    expect(pick!.weekday).toBe(1)
    expect(pick!.day).toBe(copy.weeks[0].days[0])
    expect(pick!.day.id).toBe(copy.weeks[0].days[0].id)
    expect(pick!.day.exercises[0].name).toBe('сгибания молотком')
  })

  it('picks the first day of the current week not yet in the run, a missed day is trained later', () => {
    const copy = userCopy()
    setServerPrograms({ [COPY_SLUG]: copy })
    // Wednesday of week 2 (2026-10-14) with nothing logged in this week: the first plan day is picked.
    const pick = preparePick({ programId: COPY_SLUG, startDate: '2026-10-05', run: [], now: new Date(2026, 9, 14, 12) })
    expect(pick).toMatchObject({ week: 2, weekday: 1 })
  })

  it('is null when a workout of the run was already started today', () => {
    const copy = userCopy()
    setServerPrograms({ [COPY_SLUG]: copy })
    const started = wk('w1', { startedAt: new Date(2026, 9, 5, 9).toISOString(), programId: COPY_SLUG })
    expect(preparePick({ programId: COPY_SLUG, startDate: '2026-10-05', run: [started], now: MONDAY_NOON })).toBeNull()
  })

  it('is null before the program starts', () => {
    const copy = userCopy()
    setServerPrograms({ [COPY_SLUG]: copy })
    expect(preparePick({ programId: COPY_SLUG, startDate: '2026-10-12', run: [], now: MONDAY_NOON })).toBeNull()
  })
})

function ex(name: string, over: Partial<ProgramExercise> = {}): ProgramExercise {
  return {
    name,
    intensity: null,
    order: 1,
    prescription: { sets: 3, reps_min: 8, reps_max: 12, drop_reps: null, raw: '3х8-12' },
    ...over,
  }
}

function mkDay(exercises: ProgramExercise[], over: Partial<ProgramDay> = {}): ProgramDay {
  return { weekday: 1, title: 'понедельник', exercises, ...over }
}

function wk(id: string, over: Partial<Workout> = {}, names: string[] = []): Workout {
  return {
    id,
    programId: 'p',
    week: 1,
    weekday: 1,
    startedAt: '2026-10-01T10:00:00.000Z',
    finishedAt: '2026-10-01T11:00:00.000Z',
    exercises: names.map((name) => ({ name, target: '', dropset: false, sets: [{ weight: 10, reps: 10, done: true }] })),
    ...over,
  }
}

describe('buildPrepared', () => {
  const drop = ex('жим гантелей сидя', {
    order: 2,
    prescription: { sets: 3, reps_min: null, reps_max: null, drop_reps: [12, 6, 6], raw: 'дропсет 3х 12-6-6' },
  })
  const day = mkDay([ex('сгибания', { prescription: { sets: 4, reps_min: 8, reps_max: 12, drop_reps: null, raw: '4х8-12' } }), drop], {
    id: 17,
    weekday: 3,
  })
  const inputs = { history: [], today: '2026-10-07', baselines: [], overrides: [] }

  it('builds the workout with the given program id, day id and weekday', () => {
    const { workout } = buildPrepared('w-1', 'my-copy', 2, day, '2026-10-07T08:00:00.000Z', inputs)
    expect(workout.id).toBe('w-1')
    expect(workout.programId).toBe('my-copy')
    expect(workout.programDayId).toBe(17)
    expect(workout.week).toBe(2)
    expect(workout.weekday).toBe(3)
    expect(workout.startedAt).toBe('2026-10-07T08:00:00.000Z')
    expect(workout.finishedAt).toBeNull()
  })

  it('programDayId is null for a day without an id', () => {
    const { workout } = buildPrepared('w-1', 'p', 1, mkDay([ex('a')]), '2026-10-07T08:00:00.000Z', inputs)
    expect(workout.programDayId).toBeNull()
  })

  it('lists exercises in day order with target, dropset flag and not-done sets', () => {
    const { workout } = buildPrepared('w-1', 'p', 1, day, '2026-10-07T08:00:00.000Z', inputs)
    expect(workout.exercises.map((e) => e.name)).toEqual(['сгибания', 'жим гантелей сидя'])
    expect(workout.exercises.map((e) => e.target)).toEqual(['4х8-12', 'дропсет 3х 12-6-6'])
    expect(workout.exercises.map((e) => e.dropset)).toEqual([false, true])
    expect(workout.exercises.map((e) => e.sets.length)).toEqual([4, 3])
    for (const e of workout.exercises) for (const s of e.sets) expect(s.done).toBe(false)
  })

  it('has a suggestion for every exercise name', () => {
    const { suggested } = buildPrepared('w-1', 'p', 1, day, '2026-10-07T08:00:00.000Z', inputs)
    expect(Object.keys(suggested).sort()).toEqual(['жим гантелей сидя', 'сгибания'])
  })

  it('builds a bundled day: set counts follow the prescription', () => {
    const d = BUNDLED.weeks[0].days[0]
    const { workout } = buildPrepared('w', SLUG, 1, d, '2026-10-05T08:00:00.000Z', inputs)
    expect(workout.exercises.map((e) => e.sets.length)).toEqual(d.exercises.map((e) => e.prescription.sets))
    expect(workout.exercises.map((e) => e.target)).toEqual(d.exercises.map((e) => e.prescription.raw))
  })
})

describe('sameDayContent', () => {
  const base = () => mkDay([ex('a', { order: 1 }), ex('b', { order: 2, intensity: 'heavy' })])

  it('is true for the same content with different ids', () => {
    const a = base()
    const b = base()
    a.id = 1
    b.id = 2
    a.exercises[0].id = 10
    b.exercises[0].id = 20
    expect(sameDayContent(a, b)).toBe(true)
  })

  it('is true for the same content with different title/focus (not trained content)', () => {
    expect(sameDayContent(base(), { ...base(), focus: 'Ноги' })).toBe(true)
  })

  it('is false when anything trained changes', () => {
    const changed: ((d: ProgramDay) => void)[] = [
      (d) => (d.exercises[0].name = 'x'),
      (d) => (d.exercises[0].prescription.sets = 5),
      (d) => (d.exercises[0].prescription.reps_min = 6),
      (d) => (d.exercises[0].prescription.reps_max = 15),
      (d) => (d.exercises[0].prescription.drop_reps = [12, 6]),
      (d) => (d.exercises[0].prescription.raw = '3х8-12 '),
      (d) => (d.exercises[1].intensity = 'light'),
      (d) => (d.exercises[0].order = 3),
      (d) => d.exercises.reverse(),
      (d) => d.exercises.pop(),
      (d) => d.exercises.push(ex('c', { order: 3 })),
    ]
    for (const mutate of changed) {
      const b = base()
      mutate(b)
      expect(sameDayContent(base(), b)).toBe(false)
    }
  })

  it('handles undefined days', () => {
    expect(sameDayContent(undefined, undefined)).toBe(true)
    expect(sameDayContent(base(), undefined)).toBe(false)
    expect(sameDayContent(undefined, base())).toBe(false)
  })
})

describe('rebuildDecision', () => {
  const oldDay = mkDay([ex('a'), ex('b', { order: 2 })])
  const same = (): ProgramDay => ({ ...mkDay([ex('a'), ex('b', { order: 2 })]) })
  const prepared = (over: Partial<Workout> = {}, programDayId?: number | null): Workout => ({
    ...wk('w', { finishedAt: null, programId: 'slug', ...over }, ['a', 'b']),
    programDayId,
    exercises: [
      { name: 'a', target: '', dropset: false, sets: [{ weight: null, reps: null, done: false }] },
      { name: 'b', target: '', dropset: false, sets: [{ weight: null, reps: null, done: false }] },
    ],
  })

  it('keeps when there is no active workout', () => {
    expect(rebuildDecision(null, 'slug', oldDay, same())).toBe('keep')
    expect(rebuildDecision(null, 'slug', oldDay, undefined)).toBe('keep')
  })

  it('keeps a workout of another program', () => {
    const newDay = mkDay([ex('z')])
    expect(rebuildDecision(prepared({ programId: 'other' }), 'slug', oldDay, newDay)).toBe('keep')
    expect(rebuildDecision(prepared({ programId: 'other' }), 'slug', oldDay, undefined)).toBe('keep')
  })

  it('keeps a started workout even if the content changed or the day is gone', () => {
    const started = prepared()
    started.exercises[1].sets[0].done = true
    expect(rebuildDecision(started, 'slug', oldDay, mkDay([ex('z')]))).toBe('keep')
    expect(rebuildDecision(started, 'slug', oldDay, undefined)).toBe('keep')
  })

  it('drops when the day is gone', () => {
    expect(rebuildDecision(prepared(), 'slug', oldDay, undefined)).toBe('drop')
  })

  it('rebuilds when the content changed', () => {
    expect(rebuildDecision(prepared(), 'slug', oldDay, mkDay([ex('a'), ex('c', { order: 2 })]))).toBe('rebuild')
    expect(rebuildDecision(prepared(), 'slug', undefined, same())).toBe('rebuild')
  })

  it('links when only the day id is new', () => {
    const newDay = { ...same(), id: 17 }
    expect(rebuildDecision(prepared(), 'slug', oldDay, newDay)).toBe('link')
    expect(rebuildDecision(prepared({}, null), 'slug', oldDay, newDay)).toBe('link')
    expect(rebuildDecision(prepared({}, 5), 'slug', oldDay, newDay)).toBe('link')
  })

  it('keeps when nothing changed and the day id matches', () => {
    expect(rebuildDecision(prepared({}, 17), 'slug', oldDay, { ...same(), id: 17 })).toBe('keep')
    expect(rebuildDecision(prepared({}, null), 'slug', oldDay, same())).toBe('keep')
    expect(rebuildDecision(prepared(), 'slug', oldDay, same())).toBe('keep')
  })
})

describe('exerciseNamesWithHistory', () => {
  const program: Program = {
    id: 'p',
    name: 'p',
    source: '',
    weeks: [
      { number: 1, days: [mkDay([ex('b'), ex('a', { order: 2 })])] },
      { number: 2, days: [mkDay([ex('b'), ex('c', { order: 2 })])] },
    ],
  }

  it('lists program names in program order without duplicates', () => {
    expect(exerciseNamesWithHistory(program, [])).toEqual(['b', 'a', 'c'])
  })

  it('appends history-only names, newest workout first, no duplicates', () => {
    const history = [
      wk('old', {}, ['a', 'old-only', 'shared']),
      wk('mid', {}, ['shared', 'mid-only']),
      wk('new', {}, ['b', 'new-only', 'new-only']),
    ]
    expect(exerciseNamesWithHistory(program, history)).toEqual(['b', 'a', 'c', 'new-only', 'shared', 'mid-only', 'old-only'])
  })

  it('keeps a replaced exercise that left the program but is in the history', () => {
    const replaced: Program = {
      ...program,
      weeks: [{ number: 1, days: [mkDay([ex('hammer curl')])] }],
    }
    const history = [wk('w1', {}, ['barbell curl'])]
    expect(exerciseNamesWithHistory(replaced, history)).toEqual(['hammer curl', 'barbell curl'])
  })

  it('does not mutate the program or the history', () => {
    const history = [wk('w1', {}, ['x'])]
    const snapshot = JSON.stringify([program, history])
    exerciseNamesWithHistory(program, history)
    expect(JSON.stringify([program, history])).toBe(snapshot)
  })
})

describe('pickerNames', () => {
  const program: Program = {
    id: 'p',
    name: 'p',
    source: '',
    weeks: [{ number: 1, days: [mkDay([ex('b'), ex('a', { order: 2 })])] }],
  }
  const catalog: CatalogExercise[] = [
    { name: 'z', sets: 50 },
    { name: 'a', sets: 40 },
    { name: 'y', sets: 30 },
  ]

  it('program names first, then catalog-only names in catalog order', () => {
    expect(pickerNames(catalog, program, [])).toEqual(['b', 'a', 'z', 'y'])
  })

  it('without a catalog only the program names', () => {
    expect(pickerNames(null, program, [])).toEqual(['b', 'a'])
    expect(pickerNames(undefined, program, [])).toEqual(['b', 'a'])
    expect(pickerNames([], program, [])).toEqual(['b', 'a'])
  })

  it('exclude removes from both parts', () => {
    expect(pickerNames(catalog, program, ['a', 'y'])).toEqual(['b', 'z'])
    expect(pickerNames(null, program, ['b'])).toEqual(['a'])
  })

  it('does not repeat a catalog name twice', () => {
    expect(pickerNames([...catalog, { name: 'z', sets: 1 }], program, [])).toEqual(['b', 'a', 'z', 'y'])
  })
})
