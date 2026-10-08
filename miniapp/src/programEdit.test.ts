import { describe, expect, it } from 'vitest'
import type { OpResult, ProgramOp, ProgramOut } from './api'
import {
  addItem,
  afterEdit,
  applyOps,
  cleanName,
  defaultDrops,
  defaultScope,
  draftFromDay,
  draftOps,
  editOutcome,
  formatWeeks,
  itemChange,
  moveItem,
  newTempId,
  normalizeName,
  opKinds,
  opLabel,
  opPrescription,
  prescriptionOf,
  rawOf,
  rebaseDraft,
  removeItem,
  replaceItem,
  retargetWorkouts,
  rxOf,
  rxValid,
  sameRx,
  scopeWeeks,
  setRx,
  summarize,
  validateDraft,
  validateName,
  validateRx,
  weeksParam,
  type Draft,
  type Rx,
} from './programEdit'
import type { Intensity, Program, ProgramDay, ProgramExercise } from './program'
import type { Workout } from './store'

// ---------- fixtures ----------

const rx = (over: Partial<Rx> = {}): Rx => ({ sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null, ...over })

function ex(id: number | undefined, name: string, order: number, sets: number, min: number | null, max: number | null, intensity: Intensity | null = null): ProgramExercise {
  const e: ProgramExercise = {
    name,
    intensity,
    order,
    prescription: { sets, reps_min: min, reps_max: max, drop_reps: null, raw: min == null ? `${sets}х` : max == null || max === min ? `${sets}х${min}` : `${sets}х${min}-${max}` },
  }
  if (id != null) e.id = id
  return e
}

function drop(id: number, name: string, order: number, sets: number, drops: number[]): ProgramExercise {
  return { id, name, intensity: null, order, prescription: { sets, reps_min: null, reps_max: null, drop_reps: drops, raw: `дропсет ${sets}х ${drops.join('-')}` } }
}

/** 4 weeks, Monday (3 exercises, ids week*100+1..3) and Wednesday (2 exercises, ids week*100+11..12). */
function buildProgram(): Program {
  return {
    id: 'tpl',
    name: 'Fixture',
    source: 'test',
    version: 1,
    weeks: [1, 2, 3, 4].map((w) => ({
      number: w,
      days: [
        {
          id: w * 10 + 1,
          weekday: 1,
          title: 'понедельник',
          focus: 'Руки и плечи',
          exercises: [
            ex(w * 100 + 1, w <= 2 ? 'жим лёжа' : 'жим под 30°', 1, 4, 8, 12, 'heavy'),
            ex(w * 100 + 2, 'тяга блока', 2, 3, 10, 12),
            drop(w * 100 + 3, 'отведения на дельты', 3, 3, [12, 6, 6]),
          ],
        },
        {
          id: w * 10 + 3,
          weekday: 3,
          title: 'среда',
          focus: 'База',
          exercises: [ex(w * 100 + 11, 'приседания', 1, 4, 6, 8), ex(w * 100 + 12, 'румынская тяга', 2, 3, 8, 10)],
        },
      ],
    })),
  }
}

const weekOf = (p: Program, n: number) => p.weeks.find((w) => w.number === n)!
const dayOf = (p: Program, week: number, weekday = 1): ProgramDay => weekOf(p, week).days.find((d) => d.weekday === weekday)!
const mon = (week = 1) => dayOf(buildProgram(), week)
const names = (d: ProgramDay) => d.exercises.map((e) => e.name)

function applied(program: Program, ops: ProgramOp[]) {
  const r = applyOps(program, ops)
  if (!r.ok) throw new Error(`applyOps failed at op ${r.op}: ${r.error}`)
  return r
}

const WEEKS = [1, 2, 3, 4]

// ---------- 1. names and prescriptions ----------

describe('normalizeName / cleanName', () => {
  it('normalizeName ignores case, ё/е and extra spaces', () => {
    expect(normalizeName('  Жим   ЛЁЖА ')).toBe('жим лежа')
    expect(normalizeName('жим лежа')).toBe(normalizeName('Жим Лёжа'))
  })

  it('cleanName keeps ё but lowers case and collapses spaces', () => {
    expect(cleanName('  Жим   ЛЁЖА ')).toBe('жим лёжа')
    expect(cleanName('\tтяга\nблока')).toBe('тяга блока')
    expect(cleanName('   ')).toBe('')
  })
})

describe('rxOf / opPrescription / sameRx', () => {
  it('rxOf: reps_max null falls back to reps_min', () => {
    expect(rxOf(ex(1, 'a', 1, 4, 10, null))).toEqual({ sets: 4, repsMin: 10, repsMax: 10, dropReps: null, intensity: null })
  })

  it('rxOf: a range and the intensity', () => {
    expect(rxOf(ex(1, 'a', 1, 4, 8, 12, 'heavy'))).toEqual({ sets: 4, repsMin: 8, repsMax: 12, dropReps: null, intensity: 'heavy' })
  })

  it('rxOf: a dropset has no reps and its own copy of the drops', () => {
    const e = drop(1, 'a', 1, 3, [12, 6, 6])
    const r = rxOf(e)
    expect(r).toEqual({ sets: 3, repsMin: null, repsMax: null, dropReps: [12, 6, 6], intensity: null })
    r.dropReps!.push(1)
    expect(e.prescription.drop_reps).toEqual([12, 6, 6])
  })

  it('opPrescription: repsMax defaults to repsMin', () => {
    expect(opPrescription(rx({ repsMin: 10, repsMax: null }))).toEqual({ sets: 3, repsMin: 10, repsMax: 10, dropReps: null, intensity: null })
  })

  it('opPrescription: a dropset sends no reps even if they are set', () => {
    expect(opPrescription(rx({ repsMin: 10, repsMax: 12, dropReps: [12, 6, 6], intensity: 'light' }))).toEqual({
      sets: 3,
      repsMin: null,
      repsMax: null,
      dropReps: [12, 6, 6],
      intensity: 'light',
    })
  })

  it('opPrescription: an empty dropReps array is a plain prescription', () => {
    expect(opPrescription(rx({ dropReps: [] })).dropReps).toBeNull()
  })

  it('sameRx: repsMax null equals repsMax = repsMin', () => {
    expect(sameRx(rx({ repsMin: 10, repsMax: null }), rx({ repsMin: 10, repsMax: 10 }))).toBe(true)
  })

  it('sameRx: reps are ignored in a dropset, drops and intensity are not', () => {
    expect(sameRx(rx({ dropReps: [12, 6, 6], repsMin: 1 }), rx({ dropReps: [12, 6, 6], repsMin: 9 }))).toBe(true)
    expect(sameRx(rx({ dropReps: [12, 6, 6] }), rx({ dropReps: [12, 6, 5] }))).toBe(false)
    expect(sameRx(rx({ dropReps: [12, 6, 6] }), rx())).toBe(false)
    expect(sameRx(rx({ intensity: 'heavy' }), rx({ intensity: null }))).toBe(false)
    expect(sameRx(rx({ sets: 4 }), rx())).toBe(false)
  })
})

describe('rawOf', () => {
  it('range: 6х8-12', () => {
    expect(rawOf(rx({ sets: 6, repsMin: 8, repsMax: 12 }))).toBe('6х8-12')
  })

  it('min == max: 4х10', () => {
    expect(rawOf(rx({ sets: 4, repsMin: 10, repsMax: 10 }))).toBe('4х10')
  })

  it('max null: 4х10', () => {
    expect(rawOf(rx({ sets: 4, repsMin: 10, repsMax: null }))).toBe('4х10')
  })

  it('dropset: "дропсет 3х 12-6-6"', () => {
    expect(rawOf(rx({ sets: 3, repsMin: null, repsMax: null, dropReps: [12, 6, 6] }))).toBe('дропсет 3х 12-6-6')
  })

  it('uses the Cyrillic х (U+0445), like the server', () => {
    expect(rawOf(rx({ sets: 4, repsMin: 10, repsMax: 10 })).charCodeAt(1)).toBe(0x445)
    expect(rawOf(rx({ dropReps: [12, 6] })).charCodeAt(9)).toBe(0x445)
  })

  it('matches the raw of the fixture exercises', () => {
    for (const e of mon().exercises) expect(rawOf(rxOf(e))).toBe(e.prescription.raw)
  })
})

describe('prescriptionOf', () => {
  it('plain: reps_max defaults, raw built', () => {
    expect(prescriptionOf(rx({ sets: 4, repsMin: 10, repsMax: null }))).toEqual({ sets: 4, reps_min: 10, reps_max: 10, drop_reps: null, raw: '4х10' })
  })

  it('dropset: reps null, drops kept', () => {
    expect(prescriptionOf(rx({ sets: 3, repsMin: null, repsMax: null, dropReps: [12, 6, 6] }))).toEqual({
      sets: 3,
      reps_min: null,
      reps_max: null,
      drop_reps: [12, 6, 6],
      raw: 'дропсет 3х 12-6-6',
    })
  })
})

describe('defaultDrops', () => {
  it('12 -> 12-6-6', () => expect(defaultDrops(rx({ repsMax: 12 }))).toEqual([12, 6, 6]))
  it('odd tops round the halves up: 9 -> 9-5-5', () => expect(defaultDrops(rx({ repsMax: 9 }))).toEqual([9, 5, 5]))
  it('without repsMax the repsMin is the top', () => expect(defaultDrops(rx({ repsMin: 10, repsMax: null }))).toEqual([10, 5, 5]))
  it('without any reps: 12', () => expect(defaultDrops(rx({ repsMin: null, repsMax: null }))).toEqual([12, 6, 6]))
  it('never drops below 1', () => expect(defaultDrops(rx({ repsMax: 1 }))).toEqual([1, 1, 1]))
})

// ---------- 2. draft mutations ----------

describe('draft mutations', () => {
  it('draftFromDay takes the exercises with ids in order, skipping those without', () => {
    const day: ProgramDay = {
      weekday: 1,
      title: 't',
      exercises: [ex(7, 'a', 1, 3, 10, 12), ex(undefined, 'bundled', 2, 3, 10, 12), ex(9, 'b', 3, 3, 10, 12)],
    }
    const d = draftFromDay(day)
    expect(d.map((i) => i.key)).toEqual([7, 9])
    expect(d[0]).toEqual({ key: 7, name: 'a', rx: rx() })
  })

  it('addItem hands out new1, new2 and cleans the name', () => {
    const base = draftFromDay(mon())
    const a = addItem(base, '  Подтягивания  ШИРОКИМ ')
    expect(a.key).toBe('new1')
    expect(a.draft[a.draft.length - 1].name).toBe('подтягивания широким')
    const b = addItem(a.draft, 'отжимания')
    expect(b.key).toBe('new2')
    expect(b.draft).toHaveLength(base.length + 2)
  })

  it('addItem copies the default prescription instead of sharing it', () => {
    const a = addItem([], 'x')
    a.draft[0].rx.sets = 9
    expect(addItem([], 'y').draft[0].rx.sets).toBe(3)
  })

  it('addItem keeps tempIds unique after a removal', () => {
    let d = addItem([], 'a').draft
    d = addItem(d, 'b').draft // new1, new2
    d = removeItem(d, 'new1')
    expect(newTempId(d)).toBe('new1')
    const c = addItem(d, 'c')
    expect(c.key).toBe('new1')
    expect(new Set(c.draft.map((i) => i.key)).size).toBe(c.draft.length)
  })

  it('removeItem drops the item', () => {
    expect(removeItem(draftFromDay(mon()), 102).map((i) => i.key)).toEqual([101, 103])
  })

  it('replaceItem cleans the name and leaves the rest', () => {
    const d = replaceItem(draftFromDay(mon()), 101, '  Жим  Гантелей ')
    expect(d[0].name).toBe('жим гантелей')
    expect(d[0].rx).toEqual(rxOf(mon().exercises[0]))
    expect(d[1].name).toBe('тяга блока')
  })

  it('setRx replaces only that item and copies the rx', () => {
    const r = rx({ sets: 5 })
    const d = setRx(draftFromDay(mon()), 102, r)
    r.sets = 99
    expect(d[1].rx.sets).toBe(5)
    expect(d[0].rx.sets).toBe(4)
  })

  it('moveItem swaps neighbours', () => {
    const d = draftFromDay(mon())
    expect(moveItem(d, 1, -1).map((i) => i.key)).toEqual([102, 101, 103])
    expect(moveItem(d, 1, 1).map((i) => i.key)).toEqual([101, 103, 102])
  })

  it('moveItem out of bounds is a no-op', () => {
    const d = draftFromDay(mon())
    expect(moveItem(d, 0, -1)).toBe(d)
    expect(moveItem(d, 2, 1)).toBe(d)
    expect(moveItem(d, -1, 1)).toBe(d)
    expect(moveItem(d, 3, -1)).toBe(d)
  })

  it('itemChange: added, untouched, replaced, prescribed, unknown', () => {
    const base = mon()
    const d = draftFromDay(base)
    expect(itemChange(base, { key: 'new1', name: 'x', rx: rx() })).toEqual({ added: true, replaced: false, prescribed: false })
    expect(itemChange(base, d[0])).toEqual({ added: false, replaced: false, prescribed: false })
    expect(itemChange(base, { ...d[0], name: 'Жим Лежа' })).toMatchObject({ replaced: false }) // only case and ё differ
    expect(itemChange(base, { ...d[0], name: 'жим гантелей' })).toMatchObject({ replaced: true, prescribed: false })
    expect(itemChange(base, { ...d[0], rx: rx({ sets: 5 }) })).toMatchObject({ replaced: false, prescribed: true })
    expect(itemChange(base, { key: 999, name: 'x', rx: rx() })).toEqual({ added: false, replaced: false, prescribed: false })
  })
})

// ---------- 3. checks ----------

describe('validateRx', () => {
  it.each([
    [0, false],
    [1, true],
    [20, true],
    [21, false],
    [-1, false],
    [2.5, false],
    [NaN, false],
  ])('sets %s -> valid %s', (sets, ok) => {
    expect(validateRx(rx({ sets })).sets === undefined).toBe(ok)
  })

  it.each([
    [0, false],
    [1, true],
    [100, true],
    [101, false],
    [8.5, false],
  ])('repsMin %s (repsMax null) -> valid %s', (repsMin, ok) => {
    expect(validateRx(rx({ repsMin, repsMax: null })).reps === undefined).toBe(ok)
  })

  it.each([
    [100, true],
    [101, false],
    [0, false],
  ])('repsMax %s with repsMin 1 -> valid %s', (repsMax, ok) => {
    expect(validateRx(rx({ repsMin: 1, repsMax })).reps === undefined).toBe(ok)
  })

  it('repsMin > repsMax is an error', () => {
    expect(validateRx(rx({ repsMin: 12, repsMax: 8 })).reps).toBe('«От» больше, чем «до»')
  })

  it('repsMin == repsMax and repsMax null are fine', () => {
    expect(validateRx(rx({ repsMin: 8, repsMax: 8 }))).toEqual({})
    expect(validateRx(rx({ repsMin: 8, repsMax: null }))).toEqual({})
  })

  it('a plain prescription needs repsMin', () => {
    expect(validateRx(rx({ repsMin: null, repsMax: null })).reps).toBeDefined()
  })

  it.each([
    [1, false],
    [2, true],
    [5, true],
    [6, false],
  ])('dropReps of length %s -> valid %s', (len, ok) => {
    const dropReps = Array.from({ length: len }, () => 6)
    expect(validateRx(rx({ repsMin: null, repsMax: null, dropReps })).drop === undefined).toBe(ok)
  })

  it.each([
    [[12, 0], false],
    [[12, 101], false],
    [[100, 1], true],
    [[12, 6.5], false],
  ])('dropReps %j -> valid %s', (dropReps, ok) => {
    expect(validateRx(rx({ repsMin: null, repsMax: null, dropReps })).drop === undefined).toBe(ok)
  })

  it('a dropset does not need repsMin and does not check reps', () => {
    expect(validateRx(rx({ repsMin: null, repsMax: null, dropReps: [12, 6, 6] }))).toEqual({})
    expect(validateRx(rx({ repsMin: 500, repsMax: 1, dropReps: [12, 6, 6] }))).toEqual({})
  })

  it('rxValid mirrors validateRx', () => {
    expect(rxValid(rx())).toBe(true)
    expect(rxValid(rx({ sets: 0 }))).toBe(false)
  })
})

describe('validateName', () => {
  it('empty and whitespace-only are rejected', () => {
    expect(validateName('')).toBe('Пустое название')
    expect(validateName('   \n ')).toBe('Пустое название')
  })

  it('200 characters pass, 201 do not', () => {
    expect(validateName('а'.repeat(200))).toBeNull()
    expect(validateName('а'.repeat(201))).toBe('Название длиннее 200 символов')
  })

  it('the length is counted after the spaces collapse', () => {
    expect(validateName('а'.repeat(100) + ' '.repeat(50) + 'б'.repeat(99))).toBeNull() // 100 + 1 + 99
    expect(validateName('а'.repeat(100) + ' '.repeat(50) + 'б'.repeat(100))).not.toBeNull() // 201
  })
})

describe('validateDraft', () => {
  it('the untouched day is ok', () => {
    expect(validateDraft(mon(), draftFromDay(mon()))).toEqual({ ok: true, day: null, items: {} })
  })

  it('an empty draft is a day-level error', () => {
    const c = validateDraft(mon(), [])
    expect(c.ok).toBe(false)
    expect(c.day).toBe('В дне должно остаться хотя бы одно упражнение')
  })

  it('21 exercises are too many, 20 are fine', () => {
    let d: Draft = []
    for (let i = 1; i <= 20; i++) d = addItem(d, `упражнение ${i}`).draft
    expect(validateDraft(mon(), d).ok).toBe(true)
    d = addItem(d, 'упражнение 21').draft
    const c = validateDraft(mon(), d)
    expect(c.ok).toBe(false)
    expect(c.day).toBe('В дне не больше 20 упражнений')
  })

  it('a duplicate by normalized name (ё/е, case) flags the later item', () => {
    const d = replaceItem(draftFromDay(mon()), 102, 'ЖИМ ЛЕЖА')
    const c = validateDraft(mon(), d)
    expect(c.ok).toBe(false)
    expect(c.items).toEqual({ '102': 'Это упражнение уже есть в дне' })
  })

  it('an added duplicate of an existing exercise is flagged', () => {
    const { draft, key } = addItem(draftFromDay(mon()), 'Тяга  Блока')
    expect(validateDraft(mon(), draft).items).toEqual({ [key]: 'Это упражнение уже есть в дне' })
  })

  it('an untouched invalid template item is not flagged', () => {
    const base: ProgramDay = { weekday: 1, title: 't', exercises: [ex(1, 'a', 1, 0, 0, 500), ex(2, 'b', 2, 3, 10, 12)] }
    expect(validateDraft(base, draftFromDay(base))).toEqual({ ok: true, day: null, items: {} })
  })

  it('the same item changed to something invalid is flagged', () => {
    const base: ProgramDay = { weekday: 1, title: 't', exercises: [ex(1, 'a', 1, 0, 0, 500), ex(2, 'b', 2, 3, 10, 12)] }
    const c = validateDraft(base, setRx(draftFromDay(base), 1, rx({ sets: 21 })))
    expect(c.ok).toBe(false)
    expect(c.items['1']).toBe('Подходов от 1 до 20')
  })

  it('the first problem of an item: sets, then reps, then drops', () => {
    const base = mon()
    expect(validateDraft(base, setRx(draftFromDay(base), 102, rx({ sets: 0, repsMin: 0 }))).items['102']).toMatch(/Подходов/)
    expect(validateDraft(base, setRx(draftFromDay(base), 102, rx({ repsMin: 12, repsMax: 8 }))).items['102']).toMatch(/больше/)
    expect(validateDraft(base, setRx(draftFromDay(base), 103, rx({ repsMin: null, repsMax: null, dropReps: [12] }))).items['103']).toMatch(/дропсете/)
  })

  it('a replaced item with an empty name is flagged', () => {
    const c = validateDraft(mon(), replaceItem(draftFromDay(mon()), 101, '   '))
    expect(c.items['101']).toBe('Пустое название')
  })

  it('an added item with a bad prescription is flagged', () => {
    const { draft, key } = addItem(draftFromDay(mon()), 'новое', rx({ sets: 0 }))
    expect(validateDraft(mon(), draft).items[key]).toBe('Подходов от 1 до 20')
  })
})

// ---------- 4. draft -> ops ----------

describe('draftOps', () => {
  const base = () => mon()
  const draft0 = () => draftFromDay(base())

  it('no changes: no ops', () => {
    expect(draftOps(1, 1, base(), draft0())).toEqual([])
  })

  it('replace only', () => {
    const ops = draftOps(1, 1, base(), replaceItem(draft0(), 101, 'Жим Гантелей'))
    expect(ops).toEqual([{ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'жим гантелей' }])
  })

  it('a replace to the same name in another case or with ё/е is nothing', () => {
    expect(draftOps(1, 1, base(), replaceItem(draft0(), 101, 'ЖИМ ЛЕЖА'))).toEqual([])
  })

  it('prescribe only: repsMax is filled from repsMin', () => {
    const ops = draftOps(1, 1, base(), setRx(draft0(), 102, rx({ sets: 4, repsMin: 10, repsMax: null })))
    expect(ops).toEqual([{ op: 'prescribe', week: 1, weekday: 1, itemId: 102, sets: 4, repsMin: 10, repsMax: 10, dropReps: null, intensity: null }])
  })

  it('prescribe a dropset: reps are null', () => {
    const ops = draftOps(1, 1, base(), setRx(draft0(), 103, rx({ sets: 4, repsMin: null, repsMax: null, dropReps: [10, 5, 5] })))
    expect(ops).toEqual([{ op: 'prescribe', week: 1, weekday: 1, itemId: 103, sets: 4, repsMin: null, repsMax: null, dropReps: [10, 5, 5], intensity: null }])
  })

  it('an intensity change is a prescribe', () => {
    const d = setRx(draft0(), 101, { ...rxOf(base().exercises[0]), intensity: 'light' })
    const ops = draftOps(1, 1, base(), d)
    expect(ops).toHaveLength(1)
    expect(ops[0]).toMatchObject({ op: 'prescribe', itemId: 101, intensity: 'light' })
  })

  it('replace and prescribe of the same item: one replace, then one prescribe', () => {
    let d = replaceItem(draft0(), 102, 'тяга гантели')
    d = setRx(d, 102, rx({ sets: 5 }))
    const ops = draftOps(1, 1, base(), d)
    expect(ops.map((o) => o.op)).toEqual(['replace', 'prescribe'])
    expect(ops.every((o) => 'itemId' in o && o.itemId === 102)).toBe(true)
  })

  it('two successive setRx fold into one prescribe', () => {
    let d = setRx(draft0(), 102, rx({ sets: 4 }))
    d = setRx(d, 102, rx({ sets: 5 }))
    const ops = draftOps(1, 1, base(), d)
    expect(ops).toHaveLength(1)
    expect(ops[0]).toMatchObject({ op: 'prescribe', sets: 5 })
  })

  it('setRx back to the original is no op', () => {
    let d = setRx(draft0(), 102, rx({ sets: 4 }))
    d = setRx(d, 102, rxOf(base().exercises[1]))
    expect(draftOps(1, 1, base(), d)).toEqual([])
  })

  it('add then remove of the same tempId is nothing', () => {
    const a = addItem(draft0(), 'x')
    expect(draftOps(1, 1, base(), removeItem(a.draft, a.key))).toEqual([])
  })

  it('remove', () => {
    expect(draftOps(1, 1, base(), removeItem(draft0(), 102))).toEqual([{ op: 'remove', week: 1, weekday: 1, itemId: 102 }])
  })

  it('removing the first item needs no reorder', () => {
    const ops = draftOps(1, 1, base(), removeItem(draft0(), 101))
    expect(ops.map((o) => o.op)).toEqual(['remove'])
  })

  it('add at the end carries the prescription and position = index + 1, no reorder', () => {
    const { draft, key } = addItem(draft0(), 'Подтягивания', rx({ sets: 4, repsMin: 6, repsMax: null, intensity: 'heavy' }))
    expect(draftOps(1, 1, base(), draft)).toEqual([
      { op: 'add', week: 1, weekday: 1, tempId: key, name: 'подтягивания', position: 4, sets: 4, repsMin: 6, repsMax: 6, dropReps: null, intensity: 'heavy' },
    ])
  })

  it('add of a dropset carries the drops', () => {
    const { draft } = addItem(draft0(), 'x', rx({ repsMin: null, repsMax: null, dropReps: [10, 5, 5] }))
    const add = draftOps(1, 1, base(), draft)[0]
    expect(add).toMatchObject({ op: 'add', repsMin: null, repsMax: null, dropReps: [10, 5, 5] })
  })

  it('moveItem up: one reorder with the full permutation', () => {
    const d = moveItem(draft0(), 2, -1)
    expect(draftOps(1, 1, base(), d)).toEqual([{ op: 'reorder', week: 1, weekday: 1, itemIds: [101, 103, 102] }])
  })

  it('add then move it to the top: add at position 1 and no reorder', () => {
    let { draft } = addItem(draft0(), 'x')
    draft = moveItem(moveItem(moveItem(draft, 3, -1), 2, -1), 1, -1)
    expect(draft[0].key).toBe('new1')
    const ops = draftOps(1, 1, base(), draft)
    expect(ops.map((o) => o.op)).toEqual(['add'])
    expect(ops[0]).toMatchObject({ position: 1, tempId: 'new1' })
  })

  it('add in the middle: the position is the final index + 1', () => {
    let { draft } = addItem(draft0(), 'x')
    draft = moveItem(draft, 3, -1) // [101, 102, new1, 103]
    const ops = draftOps(1, 1, base(), draft)
    expect(ops.map((o) => o.op)).toEqual(['add'])
    expect(ops[0]).toMatchObject({ position: 3 })
  })

  it('spec case: add a new one, move it up, change its sets -> a single add with the prescription inside', () => {
    let { draft, key } = addItem(draft0(), 'x')
    draft = moveItem(draft, 3, -1)
    draft = setRx(draft, key, rx({ sets: 7 }))
    const ops = draftOps(1, 1, base(), draft)
    expect(ops).toHaveLength(1)
    expect(ops[0]).toMatchObject({ op: 'add', tempId: key, position: 3, sets: 7 })
  })

  it('an add that the adds and removes alone do not put in place needs a reorder with the tempId in it', () => {
    let { draft } = addItem(draft0(), 'x')
    draft = moveItem(draft, 3, -1) // [101, 102, new1, 103]
    draft = moveItem(draft, 0, 1) // [102, 101, new1, 103]
    const ops = draftOps(1, 1, base(), draft)
    expect(ops.map((o) => o.op)).toEqual(['add', 'reorder'])
    expect(ops[1]).toEqual({ op: 'reorder', week: 1, weekday: 1, itemIds: [102, 101, 'new1', 103] })
  })

  it('order of ops: removes, replaces, prescribes, adds, then the reorder', () => {
    let d = removeItem(draft0(), 102) // [101, 103]
    d = replaceItem(d, 101, 'жим гантелей')
    d = setRx(d, 103, rx({ repsMin: null, repsMax: null, dropReps: [8, 4, 4] }))
    d = addItem(d, 'подтягивания').draft // [101, 103, new1]
    d = moveItem(d, 0, 1) // [103, 101, new1]
    const ops = draftOps(1, 1, base(), d)
    expect(ops.map((o) => o.op)).toEqual(['remove', 'replace', 'prescribe', 'add', 'reorder'])
    expect(ops[4]).toMatchObject({ itemIds: [103, 101, 'new1'] })
  })

  it('two adds keep their final positions in ascending order', () => {
    let d = addItem(draft0(), 'a').draft
    d = addItem(d, 'b').draft
    const ops = draftOps(1, 1, base(), d)
    expect(ops.map((o) => (o.op === 'add' ? o.position : null))).toEqual([4, 5])
  })

  it('items without a server id are not sent', () => {
    const bundledDay: ProgramDay = { weekday: 1, title: 't', exercises: [ex(undefined, 'a', 1, 3, 10, 12), ex(5, 'b', 2, 3, 10, 12)] }
    expect(draftOps(1, 1, bundledDay, draftFromDay(bundledDay))).toEqual([])
  })

  describe('scopes', () => {
    const scope = { structure: [1, 2, 3, 4], prescribe: [2] }

    it('structure ops carry weeks 1..4, prescribe carries none when it is the edited week only', () => {
      let d = removeItem(draft0(), 103)
      d = replaceItem(d, 101, 'жим гантелей')
      d = setRx(d, 102, rx({ sets: 5 }))
      const { draft } = addItem(d, 'x')
      const ops = draftOps(2, 1, dayOf(buildProgram(), 2), draft.map((i) => ({ ...i, key: typeof i.key === 'number' ? i.key + 100 : i.key })), scope)
      for (const o of ops) {
        if (o.op === 'prescribe') expect(o.weeks).toBeUndefined()
        else expect(o.weeks).toEqual([1, 2, 3, 4])
      }
      expect(ops.map((o) => o.op)).toEqual(['remove', 'replace', 'prescribe', 'add'])
    })

    it('reorder carries the structure weeks', () => {
      const ops = draftOps(2, 1, dayOf(buildProgram(), 2), draftFromDay(dayOf(buildProgram(), 2)).reverse(), scope)
      expect(ops).toHaveLength(1)
      expect(ops[0]).toMatchObject({ op: 'reorder', weeks: [1, 2, 3, 4] })
    })

    it('the edited week is added to the weeks and they are sorted', () => {
      const ops = draftOps(3, 1, dayOf(buildProgram(), 3), removeItem(draftFromDay(dayOf(buildProgram(), 3)), 301), { structure: [4, 1] })
      expect(ops[0]).toMatchObject({ op: 'remove', weeks: [1, 3, 4] })
    })

    it('prescribe weeks include the edited week', () => {
      const d = setRx(draftFromDay(dayOf(buildProgram(), 2)), 202, rx({ sets: 5 }))
      const ops = draftOps(2, 1, dayOf(buildProgram(), 2), d, { prescribe: [4, 3] })
      expect(ops[0]).toMatchObject({ op: 'prescribe', weeks: [2, 3, 4] })
    })

    it('no scope at all: no weeks anywhere', () => {
      const ops = draftOps(1, 1, base(), removeItem(draft0(), 101))
      expect(ops[0]).not.toHaveProperty('weeks')
    })
  })
})

describe('weeksParam', () => {
  it('undefined stays undefined', () => expect(weeksParam(undefined, 2)).toBeUndefined())
  it('only the edited week: undefined', () => {
    expect(weeksParam([2], 2)).toBeUndefined()
    expect(weeksParam([], 2)).toBeUndefined()
  })
  it('adds the edited week, removes duplicates, sorts', () => {
    expect(weeksParam([4, 2, 4, 1], 3)).toEqual([1, 2, 3, 4])
    expect(weeksParam([3], 2)).toEqual([2, 3])
  })
})

// ---------- 5. scope ----------

describe('scopeWeeks / defaultScope / opKinds', () => {
  it('week', () => expect(scopeWeeks('week', 3, WEEKS)).toEqual([3]))
  it('from', () => expect(scopeWeeks('from', 3, WEEKS)).toEqual([3, 4]))
  it('all (sorted)', () => expect(scopeWeeks('all', 2, [4, 1, 3, 2])).toEqual([1, 2, 3, 4]))
  it('pick always includes the edited week, sorted, only existing weeks', () => {
    expect(scopeWeeks('pick', 2, WEEKS, [4, 1])).toEqual([1, 2, 4])
    expect(scopeWeeks('pick', 2, WEEKS, [9])).toEqual([2])
    expect(scopeWeeks('pick', 2, WEEKS)).toEqual([2])
  })
  describe('defaultScope', () => {
    const d0 = () => draftFromDay(mon())
    const scopeOf = (d: Draft) => defaultScope(draftOps(1, 1, mon(), d))
    it('replacements only: all weeks', () =>
      expect(scopeOf(replaceItem(d0(), 101, 'жим гантелей'))).toEqual({ structure: 'all', prescribe: 'week' }))
    it('replace plus prescribe: replace to all weeks, sets this week', () =>
      expect(scopeOf(setRx(replaceItem(d0(), 101, 'жим гантелей'), 102, rx({ sets: 5 })))).toEqual({ structure: 'all', prescribe: 'week' }))
    it('replace plus remove: this week (a removal is never preselected for every week)', () =>
      expect(scopeOf(removeItem(replaceItem(d0(), 101, 'жим гантелей'), 102))).toEqual({ structure: 'week', prescribe: 'week' }))
    it('add, remove or move alone: this week', () => {
      expect(scopeOf(addItem(d0(), 'шраги').draft).structure).toBe('week')
      expect(scopeOf(removeItem(d0(), 102)).structure).toBe('week')
      expect(scopeOf(moveItem(d0(), 0, 1)).structure).toBe('week')
    })
    it('no ops: this week', () => expect(defaultScope([])).toEqual({ structure: 'week', prescribe: 'week' }))
  })

  it('opKinds', () => {
    const d0 = draftFromDay(mon())
    expect(opKinds([])).toEqual({ structure: false, prescribe: false })
    expect(opKinds(draftOps(1, 1, mon(), setRx(d0, 102, rx({ sets: 5 }))))).toEqual({ structure: false, prescribe: true })
    expect(opKinds(draftOps(1, 1, mon(), removeItem(d0, 102)))).toEqual({ structure: true, prescribe: false })
    expect(opKinds(draftOps(1, 1, mon(), setRx(removeItem(d0, 101), 102, rx({ sets: 5 }))))).toEqual({ structure: true, prescribe: true })
  })
})

// ---------- 6. applyOps ----------

describe('applyOps', () => {
  it('replace on all weeks applies where the exercise matches and skips the others with a reason', () => {
    const p = buildProgram()
    const r = applied(p, [{ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'Жим Гантелей', weeks: WEEKS }])
    expect(names(dayOf(r.program, 1))[0]).toBe('жим гантелей')
    expect(names(dayOf(r.program, 2))[0]).toBe('жим гантелей')
    expect(names(dayOf(r.program, 3))[0]).toBe('жим под 30°')
    expect(names(dayOf(r.program, 4))[0]).toBe('жим под 30°')
    expect(r.results).toEqual([
      {
        op: 0,
        weeks: [1, 2],
        skipped: [
          { week: 3, reason: 'нет этого упражнения' },
          { week: 4, reason: 'нет этого упражнения' },
        ],
      },
    ])
  })

  it('replace matches exercises of other weeks by normalized name', () => {
    const p = buildProgram()
    dayOf(p, 2).exercises[0].name = 'Жим Лежа' // ё/е and case differ from week 1
    const r = applied(p, [{ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'x', weeks: [1, 2] }])
    expect(r.results[0].weeks).toEqual([1, 2])
  })

  it('replace does not touch the other weekday', () => {
    const p = buildProgram()
    const r = applied(p, [{ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'x', weeks: WEEKS }])
    expect(names(dayOf(r.program, 2, 3))).toEqual(['приседания', 'румынская тяга'])
  })

  it('replace to a name already in the day: an error in the edited week', () => {
    const r = applyOps(buildProgram(), [{ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'Тяга блока' }])
    expect(r).toEqual({ ok: false, op: 0, error: 'Упражнение уже есть в дне' })
  })

  it('replace to a name already in the day: a skip in other weeks', () => {
    const p = buildProgram()
    dayOf(p, 2).exercises.push(ex(250, 'жим гантелей', 4, 3, 10, 12)) // week 2 already has the target name
    const r = applied(p, [{ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'жим гантелей', weeks: [1, 2] }])
    expect(r.results[0].weeks).toEqual([1])
    expect(r.results[0].skipped).toEqual([{ week: 2, reason: 'упражнение уже есть в дне' }])
    expect(names(dayOf(r.program, 2))[0]).toBe('жим лёжа')
  })

  it('prescribe on all weeks sets the absolute prescription and the intensity; the original is not mutated', () => {
    const p = buildProgram()
    const r = applied(p, [{ op: 'prescribe', week: 1, weekday: 1, itemId: 102, sets: 5, repsMin: 6, repsMax: null, dropReps: null, intensity: 'light', weeks: WEEKS }])
    for (const w of WEEKS) {
      const e = dayOf(r.program, w).exercises[1]
      expect(e.prescription).toEqual({ sets: 5, reps_min: 6, reps_max: 6, drop_reps: null, raw: '5х6' })
      expect(e.intensity).toBe('light')
    }
    expect(r.results[0].weeks).toEqual(WEEKS)
    expect(p).toEqual(buildProgram())
  })

  it('prescribe turns a plain exercise into a dropset', () => {
    const r = applied(buildProgram(), [{ op: 'prescribe', week: 1, weekday: 1, itemId: 102, sets: 3, repsMin: null, repsMax: null, dropReps: [12, 6, 6], intensity: null }])
    expect(dayOf(r.program, 1).exercises[1].prescription).toEqual({ sets: 3, reps_min: null, reps_max: null, drop_reps: [12, 6, 6], raw: 'дропсет 3х 12-6-6' })
  })

  it('prescribe without weeks touches the edited week only', () => {
    const r = applied(buildProgram(), [{ op: 'prescribe', week: 2, weekday: 1, itemId: 202, sets: 9, repsMin: 5, repsMax: 5, dropReps: null, intensity: null }])
    expect(dayOf(r.program, 2).exercises[1].prescription.sets).toBe(9)
    expect(dayOf(r.program, 1).exercises[1].prescription.sets).toBe(3)
    expect(dayOf(r.program, 3).exercises[1].prescription.sets).toBe(3)
  })

  it('add on all weeks inserts at the same position and renumbers the order', () => {
    const r = applied(buildProgram(), [
      { op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'Подтягивания', position: 2, sets: 4, repsMin: 6, repsMax: 8, dropReps: null, intensity: 'heavy', weeks: WEEKS },
    ])
    for (const w of WEEKS) {
      const d = dayOf(r.program, w)
      expect(d.exercises[1].name).toBe('подтягивания')
      expect(d.exercises[1].prescription.raw).toBe('4х6-8')
      expect(d.exercises[1].intensity).toBe('heavy')
      expect(d.exercises.map((e) => e.order)).toEqual([1, 2, 3, 4])
    }
    expect(r.results[0].weeks).toEqual(WEEKS)
  })

  it('add clamps a too large position to the end, per day', () => {
    const r = applied(buildProgram(), [{ op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'x', position: 99, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null, weeks: [1, 2] }])
    expect(dayOf(r.program, 1).exercises.map((e) => e.name).pop()).toBe('x')
    expect(dayOf(r.program, 2).exercises.map((e) => e.name).pop()).toBe('x')
  })

  it('add skips a week where the exercise already exists', () => {
    const p = buildProgram()
    dayOf(p, 3).exercises.push(ex(399, 'Подтягивания', 4, 3, 10, 12))
    const r = applied(p, [{ op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'подтягивания', position: 4, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null, weeks: WEEKS }])
    expect(r.results[0].weeks).toEqual([1, 2, 4])
    expect(r.results[0].skipped).toEqual([{ week: 3, reason: 'упражнение уже есть в дне' }])
    expect(dayOf(r.program, 3).exercises).toHaveLength(4)
  })

  it('add of an existing name in the edited week is an error', () => {
    const r = applyOps(buildProgram(), [{ op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'ТЯГА БЛОКА', position: 1, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null }])
    expect(r).toMatchObject({ ok: false, op: 0 })
  })

  it('add to a full day (20) is an error in the edited week and a skip elsewhere', () => {
    const p = buildProgram()
    const fill = (d: ProgramDay) => {
      for (let i = d.exercises.length; i < 20; i++) d.exercises.push(ex(9000 + i, `доп ${i}`, i + 1, 3, 10, 12))
    }
    fill(dayOf(p, 2))
    const add = (week: number, weeks?: number[]): ProgramOp => ({ op: 'add', week, weekday: 1, tempId: 'new1', name: 'x', position: 1, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null, weeks })
    const r = applied(p, [add(1, [1, 2])])
    expect(r.results[0].skipped).toEqual([{ week: 2, reason: 'в дне уже 20 упражнений' }])
    expect(applyOps(p, [add(2)])).toMatchObject({ ok: false, op: 0 })
  })

  it('remove on all weeks removes the same exercise and renumbers', () => {
    const r = applied(buildProgram(), [{ op: 'remove', week: 1, weekday: 1, itemId: 102, weeks: WEEKS }])
    for (const w of WEEKS) {
      expect(names(dayOf(r.program, w))).not.toContain('тяга блока')
      expect(dayOf(r.program, w).exercises.map((e) => e.order)).toEqual([1, 2])
    }
  })

  it('remove skips a week where it is the last exercise', () => {
    const p = buildProgram()
    dayOf(p, 3, 3).exercises = [ex(311, 'приседания', 1, 4, 6, 8)]
    const r = applied(p, [{ op: 'remove', week: 1, weekday: 3, itemId: 111, weeks: [1, 2, 3] }])
    expect(r.results[0].weeks).toEqual([1, 2])
    expect(r.results[0].skipped).toEqual([{ week: 3, reason: 'последнее упражнение дня' }])
    expect(dayOf(r.program, 3, 3).exercises).toHaveLength(1)
  })

  it('remove of the last exercise in the edited week is an error', () => {
    const p = buildProgram()
    dayOf(p, 1, 3).exercises = [ex(111, 'приседания', 1, 4, 6, 8)]
    expect(applyOps(p, [{ op: 'remove', week: 1, weekday: 3, itemId: 111 }])).toEqual({ ok: false, op: 0, error: 'Нельзя убрать последнее упражнение дня' })
  })

  it('removing both exercises of a day one after another fails on the second', () => {
    const r = applyOps(buildProgram(), [
      { op: 'remove', week: 1, weekday: 3, itemId: 111 },
      { op: 'remove', week: 1, weekday: 3, itemId: 112 },
    ])
    expect(r).toMatchObject({ ok: false, op: 1 })
  })

  it('reorder applies in weeks with the identical set and skips the others', () => {
    const r = applied(buildProgram(), [{ op: 'reorder', week: 1, weekday: 1, itemIds: [103, 101, 102], weeks: WEEKS }])
    expect(names(dayOf(r.program, 1))).toEqual(['отведения на дельты', 'жим лёжа', 'тяга блока'])
    expect(names(dayOf(r.program, 2))).toEqual(['отведения на дельты', 'жим лёжа', 'тяга блока'])
    expect(names(dayOf(r.program, 3))).toEqual(['жим под 30°', 'тяга блока', 'отведения на дельты']) // untouched
    expect(r.results[0].weeks).toEqual([1, 2])
    expect(r.results[0].skipped).toEqual([
      { week: 3, reason: 'другой набор упражнений' },
      { week: 4, reason: 'другой набор упражнений' },
    ])
    expect(dayOf(r.program, 2).exercises.map((e) => e.order)).toEqual([1, 2, 3])
  })

  it('reorder with a missing, repeated or foreign id is an error', () => {
    const p = buildProgram()
    const reorder = (itemIds: (number | string)[]): ProgramOp => ({ op: 'reorder', week: 1, weekday: 1, itemIds })
    expect(applyOps(p, [reorder([101, 102])])).toMatchObject({ ok: false, op: 0 })
    expect(applyOps(p, [reorder([101, 101, 102])])).toMatchObject({ ok: false, op: 0 })
    expect(applyOps(p, [reorder([101, 102, 999])])).toMatchObject({ ok: false, op: 0 })
  })

  it('ops see the result of the previous ones', () => {
    const r = applied(buildProgram(), [
      { op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'жим гантелей' },
      { op: 'remove', week: 1, weekday: 1, itemId: 101 },
    ])
    expect(names(dayOf(r.program, 1))).toEqual(['тяга блока', 'отведения на дельты'])
  })

  it('an unknown itemId is an error', () => {
    expect(applyOps(buildProgram(), [{ op: 'remove', week: 1, weekday: 1, itemId: 999 }])).toMatchObject({ ok: false, op: 0 })
  })

  it('an unknown tempId is an error', () => {
    const r = applyOps(buildProgram(), [{ op: 'prescribe', week: 1, weekday: 1, itemId: 'new7', sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null }])
    expect(r).toMatchObject({ ok: false, op: 0 })
  })

  it('a week that does not exist in `weeks` is an error', () => {
    expect(applyOps(buildProgram(), [{ op: 'remove', week: 1, weekday: 1, itemId: 101, weeks: [1, 9] }])).toEqual({ ok: false, op: 0, error: 'Нет недели 9' })
  })

  it('an unknown edited week or weekday is an error', () => {
    expect(applyOps(buildProgram(), [{ op: 'remove', week: 9, weekday: 1, itemId: 101 }])).toMatchObject({ ok: false })
    expect(applyOps(buildProgram(), [{ op: 'remove', week: 1, weekday: 5, itemId: 101 }])).toMatchObject({ ok: false })
  })

  it('a missing weekday in another week is a skip', () => {
    const p = buildProgram()
    weekOf(p, 4).days = weekOf(p, 4).days.filter((d) => d.weekday !== 3)
    const r = applied(p, [{ op: 'prescribe', week: 1, weekday: 3, itemId: 111, sets: 9, repsMin: 5, repsMax: 5, dropReps: null, intensity: null, weeks: WEEKS }])
    expect(r.results[0].skipped).toEqual([{ week: 4, reason: 'нет этого дня' }])
  })

  it('more than 50 ops is an error', () => {
    const op: ProgramOp = { op: 'prescribe', week: 1, weekday: 1, itemId: 101, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null }
    expect(applyOps(buildProgram(), Array(50).fill(op))).toMatchObject({ ok: true })
    expect(applyOps(buildProgram(), Array(51).fill(op))).toMatchObject({ ok: false })
  })

  it('an empty ops list returns an equal copy', () => {
    const p = buildProgram()
    const r = applied(p, [])
    expect(r.program).toEqual(p)
    expect(r.program).not.toBe(p)
    expect(r.results).toEqual([])
  })

  // Regression: renumber() once replaced the added object with a copy, so later ops by tempId lost it.
  it('tempId flows: add, then prescribe the new item in the same list', () => {
    const r = applied(buildProgram(), [
      { op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'x', position: 4, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null },
      { op: 'prescribe', week: 1, weekday: 1, itemId: 'new1', sets: 5, repsMin: 5, repsMax: 5, dropReps: null, intensity: null },
    ])
    expect(dayOf(r.program, 1).exercises[3].prescription.raw).toBe('5х5')
  })

  it('tempId flows: add, then reorder with the tempId in the permutation', () => {
    const r = applied(buildProgram(), [
      { op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'x', position: 4, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null },
      { op: 'reorder', week: 1, weekday: 1, itemIds: ['new1', 101, 102, 103] },
    ])
    expect(names(dayOf(r.program, 1))).toEqual(['x', 'жим лёжа', 'тяга блока', 'отведения на дельты'])
  })

  it('tempId flows: add at the top, then reorder, then prescribe, all by tempId', () => {
    const r = applied(buildProgram(), [
      { op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'x', position: 1, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null },
      { op: 'reorder', week: 1, weekday: 1, itemIds: [101, 'new1', 102, 103] },
      { op: 'prescribe', week: 1, weekday: 1, itemId: 'new1', sets: 7, repsMin: 5, repsMax: 5, dropReps: null, intensity: null },
    ])
    expect(names(dayOf(r.program, 1))).toEqual(['жим лёжа', 'x', 'тяга блока', 'отведения на дельты'])
    expect(dayOf(r.program, 1).exercises[1].prescription.sets).toBe(7)
  })

  it('order is renumbered 1..n after every kind of structural op', () => {
    const r = applied(buildProgram(), [
      { op: 'add', week: 1, weekday: 1, tempId: 'new1', name: 'x', position: 1, sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null },
      { op: 'remove', week: 1, weekday: 1, itemId: 102 },
    ])
    expect(dayOf(r.program, 1).exercises.map((e) => e.order)).toEqual([1, 2, 3])
  })

  it('the input program is never mutated', () => {
    const p = buildProgram()
    applied(p, [
      { op: 'remove', week: 1, weekday: 1, itemId: 101, weeks: WEEKS },
      { op: 'replace', week: 1, weekday: 3, itemId: 111, name: 'x', weeks: WEEKS },
      { op: 'add', week: 1, weekday: 1, tempId: 'n', name: 'y', position: 1, sets: 3, repsMin: 1, repsMax: 1, dropReps: null, intensity: null, weeks: WEEKS },
    ])
    expect(p).toEqual(buildProgram())
  })
})

describe('applyOps: draftOps round trip', () => {
  function roundTrip(draft: Draft, week = 1) {
    const p = buildProgram()
    const base = dayOf(p, week)
    const ops = draftOps(week, 1, base, draft)
    const r = applied(p, ops)
    const day = dayOf(r.program, week)
    expect(day.exercises.map((e) => normalizeName(e.name))).toEqual(draft.map((i) => normalizeName(i.name)))
    day.exercises.forEach((e, i) => expect(sameRx(rxOf(e), draft[i].rx)).toBe(true))
    expect(day.exercises.map((e) => e.order)).toEqual(draft.map((_, i) => i + 1))
    return ops
  }
  const d0 = () => draftFromDay(mon())

  it('no changes', () => roundTrip(d0()))
  it('replace', () => roundTrip(replaceItem(d0(), 101, 'жим гантелей')))
  it('prescribe, plain and dropset', () => {
    let d = setRx(d0(), 102, rx({ sets: 5, repsMin: 6, repsMax: null, intensity: 'light' }))
    d = setRx(d, 103, rx({ repsMin: null, repsMax: null, dropReps: [10, 5, 5, 5] }))
    roundTrip(d)
  })
  it('a plain exercise turned into a dropset and back', () => {
    roundTrip(setRx(d0(), 102, rx({ repsMin: null, repsMax: null, dropReps: [12, 6, 6] })))
    roundTrip(setRx(d0(), 103, rx({ sets: 3, repsMin: 8, repsMax: 12 })))
  })
  it('remove', () => roundTrip(removeItem(d0(), 101)))
  it('reorder', () => roundTrip(moveItem(moveItem(d0(), 2, -1), 1, -1)))
  it('add at the end', () => roundTrip(addItem(d0(), 'Подтягивания', rx({ sets: 4, repsMin: 6, repsMax: 8, intensity: 'medium' })).draft))
  it('add at the top', () => {
    let { draft } = addItem(d0(), 'x')
    for (let i = 3; i > 0; i--) draft = moveItem(draft, i, -1)
    const ops = roundTrip(draft)
    expect(ops.map((o) => o.op)).toEqual(['add'])
  })
  it('two adds at the end', () => roundTrip(addItem(addItem(d0(), 'a').draft, 'b').draft))
  it('remove, replace, prescribe and add together', () => {
    let d = removeItem(d0(), 102)
    d = replaceItem(d, 101, 'Жим Гантелей')
    d = setRx(d, 103, rx({ sets: 4, repsMin: null, repsMax: null, dropReps: [8, 4] }))
    d = addItem(d, 'подтягивания').draft
    roundTrip(d)
  })
  it('remove and add in the same day with the days order kept', () => {
    let d = removeItem(d0(), 101)
    d = addItem(d, 'z').draft
    roundTrip(d)
  })
  it('a draft in another week (3) where the first exercise is a variation', () => {
    roundTrip(replaceItem(draftFromDay(mon(3)), 302, 'тяга гантели'), 3)
  })
  it('add, then move it so that a reorder with its tempId is needed', () => {
    let { draft } = addItem(d0(), 'x')
    draft = moveItem(draft, 3, -1) // [101, 102, new1, 103]
    draft = moveItem(draft, 0, 1) // [102, 101, new1, 103]
    roundTrip(draft)
  })
  it('a random sequence of edits (seeded) always round-trips (tempIds in reorders too)', () => {
    let seed = 12345
    const rnd = (n: number) => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed % n
    }
    for (let round = 0; round < 40; round++) {
      let d = d0()
      for (let step = 0; step < 6; step++) {
        const k = rnd(5)
        if (k === 0 && d.length > 1) d = removeItem(d, d[rnd(d.length)].key)
        else if (k === 1) d = addItem(d, `доп ${round}-${step}`, rx({ sets: 1 + rnd(8) })).draft
        else if (k === 2) d = setRx(d, d[rnd(d.length)].key, rx({ sets: 1 + rnd(8) }))
        else if (k === 3) d = replaceItem(d, d[rnd(d.length)].key, `замена ${round}-${step}`)
        else d = moveItem(d, rnd(d.length), rnd(2) ? 1 : -1)
      }
      const ops = draftOps(1, 1, mon(), d)
      const r = applied(buildProgram(), ops)
      const day = dayOf(r.program, 1)
      expect(day.exercises.map((e) => normalizeName(e.name))).toEqual(d.map((i) => normalizeName(i.name)))
      day.exercises.forEach((e, i) => expect(sameRx(rxOf(e), d[i].rx)).toBe(true))
    }
  })
})

// ---------- 7. rebaseDraft ----------

describe('rebaseDraft', () => {
  const oldDay = () => mon()
  const withNew = (change: (d: ProgramDay) => void) => {
    const d = mon()
    change(d)
    return d
  }

  it('no edits: the new day as a draft', () => {
    const newDay = withNew((d) => (d.exercises[1].prescription = { sets: 5, reps_min: 5, reps_max: 5, drop_reps: null, raw: '5х5' }))
    expect(rebaseDraft(oldDay(), newDay, draftFromDay(oldDay()), 1)).toEqual(draftFromDay(newDay))
  })

  it('an edit survives when the new day changed another, untouched item', () => {
    const newDay = withNew((d) => (d.exercises[1].prescription = { sets: 5, reps_min: 5, reps_max: 5, drop_reps: null, raw: '5х5' }))
    const draft = replaceItem(draftFromDay(oldDay()), 101, 'жим гантелей')
    const out = rebaseDraft(oldDay(), newDay, draft, 1)!
    expect(out).not.toBeNull()
    expect(out[0].name).toBe('жим гантелей')
    expect(out[1].rx.sets).toBe(5) // the elsewhere change is kept, not reverted
    expect(out.map((i) => i.key)).toEqual([101, 102, 103])
  })

  it('a prescribe edit is applied absolute on top of the new day', () => {
    const newDay = withNew((d) => (d.exercises[0].name = 'жим лёжа узким'))
    const draft = setRx(draftFromDay(oldDay()), 102, rx({ sets: 6 }))
    const out = rebaseDraft(oldDay(), newDay, draft, 1)!
    expect(out[0].name).toBe('жим лёжа узким')
    expect(out[1].rx.sets).toBe(6)
  })

  it('a removal is carried over', () => {
    const out = rebaseDraft(oldDay(), oldDay(), removeItem(draftFromDay(oldDay()), 102), 1)!
    expect(out.map((i) => i.key)).toEqual([101, 103])
  })

  it('null when an edited item is gone from the new day', () => {
    const newDay = withNew((d) => (d.exercises = d.exercises.filter((e) => e.id !== 103)))
    const draft = setRx(draftFromDay(oldDay()), 103, rx({ repsMin: null, repsMax: null, dropReps: [10, 5, 5] }))
    expect(rebaseDraft(oldDay(), newDay, draft, 1)).toBeNull()
  })

  it('added items keep their tempId', () => {
    const newDay = withNew((d) => (d.exercises[2].prescription = { sets: 4, reps_min: null, reps_max: null, drop_reps: [10, 5, 5], raw: 'дропсет 4х 10-5-5' }))
    const a = addItem(draftFromDay(oldDay()), 'Подтягивания', rx({ sets: 4 }))
    const out = rebaseDraft(oldDay(), newDay, a.draft, 1)!
    expect(out).toHaveLength(4)
    expect(out[3]).toMatchObject({ key: a.key, name: 'подтягивания' })
    expect(out[3].rx.sets).toBe(4)
    expect(out[2].rx.dropReps).toEqual([10, 5, 5])
  })

  it('two added items keep their own tempIds', () => {
    const a = addItem(addItem(draftFromDay(oldDay()), 'a').draft, 'b')
    const out = rebaseDraft(oldDay(), oldDay(), a.draft, 1)!
    expect(out.map((i) => i.key)).toEqual([101, 102, 103, 'new1', 'new2'])
  })

  it('an added item the new day already has -> null (the add fails in the edited week)', () => {
    const newDay = withNew((d) => d.exercises.push(ex(150, 'подтягивания', 4, 3, 10, 12)))
    const a = addItem(draftFromDay(oldDay()), 'подтягивания')
    expect(rebaseDraft(oldDay(), newDay, a.draft, 1)).toBeNull()
  })

  it('an add moved to the top (tempId in a reorder) survives a rebase', () => {
    let { draft } = addItem(draftFromDay(oldDay()), 'x')
    draft = moveItem(draft, 3, -1)
    draft = moveItem(draft, 0, 1) // [102, 101, new1, 103]: needs a reorder with the tempId
    const out = rebaseDraft(oldDay(), oldDay(), draft, 1)
    expect(out?.map((i) => i.key)).toEqual([102, 101, 'new1', 103])
  })
})

// ---------- 8. summaries ----------

describe('formatWeeks', () => {
  it.each([
    [[], ''],
    [[1], '1'],
    [[1, 2], '1, 2'],
    [[1, 2, 3], '1–3'],
    [[1, 2, 3, 4], '1–4'],
    [[1, 2, 6, 7, 8], '1, 2, 6–8'],
    [[1, 3, 5], '1, 3, 5'],
    [[1, 3, 4, 5, 7], '1, 3–5, 7'],
    [[3, 1, 2, 2], '1–3'],
    [[8, 6, 7, 2, 1, 1], '1, 2, 6–8'],
  ])('%j -> "%s"', (weeks, out) => {
    expect(formatWeeks(weeks)).toBe(out)
  })
})

describe('summarize', () => {
  const ops: ProgramOp[] = [
    { op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'x' },
    { op: 'remove', week: 1, weekday: 1, itemId: 102 },
  ]
  const nameOf = (op: ProgramOp) => `${op.op}!`

  it('changed is the sorted union of the weeks that landed', () => {
    const results: OpResult[] = [
      { op: 0, weeks: [4, 1], skipped: [] },
      { op: 1, weeks: [2, 1], skipped: [] },
    ]
    expect(summarize(ops, results, nameOf)).toEqual({ changed: [1, 2, 4], skipped: [] })
  })

  it('skipped weeks are grouped per op and reason', () => {
    const results: OpResult[] = [
      {
        op: 0,
        weeks: [1, 2],
        skipped: [
          { week: 4, reason: 'нет этого упражнения' },
          { week: 3, reason: 'нет этого упражнения' },
          { week: 5, reason: 'нет этого дня' },
        ],
      },
      { op: 1, weeks: [1], skipped: [{ week: 3, reason: 'нет этого упражнения' }] },
    ]
    const s = summarize(ops, results, nameOf)
    expect(s.changed).toEqual([1, 2])
    expect(s.skipped).toEqual([
      { weeks: [3, 4], reason: 'нет этого упражнения', what: 'replace!' },
      { weeks: [5], reason: 'нет этого дня', what: 'replace!' },
      { weeks: [3], reason: 'нет этого упражнения', what: 'remove!' },
    ])
  })

  it('works end to end with applyOps', () => {
    const o: ProgramOp[] = [{ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'жим гантелей', weeks: WEEKS }]
    const r = applied(buildProgram(), o)
    expect(summarize(o, r.results, () => 'жим').skipped).toEqual([{ weeks: [3, 4], reason: 'нет этого упражнения', what: 'жим' }])
    expect(summarize(o, r.results, () => 'жим').changed).toEqual([1, 2])
  })

  it('empty results', () => {
    expect(summarize([], [], nameOf)).toEqual({ changed: [], skipped: [] })
  })
})

describe('opLabel', () => {
  const base = mon()
  const { draft, key } = addItem(draftFromDay(base), 'Подтягивания')

  it('replace', () => {
    expect(opLabel({ op: 'replace', week: 1, weekday: 1, itemId: 101, name: 'жим гантелей' }, base, draft)).toBe('замена жим лёжа → жим гантелей')
  })
  it('prescribe, by id and by tempId', () => {
    const p = { sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null }
    expect(opLabel({ op: 'prescribe', week: 1, weekday: 1, itemId: 102, ...p }, base, draft)).toBe('подходы: тяга блока')
    expect(opLabel({ op: 'prescribe', week: 1, weekday: 1, itemId: key, ...p }, base, draft)).toBe('подходы: подтягивания')
  })
  it('add', () => {
    const o = draftOps(1, 1, base, draft)[0]
    expect(opLabel(o, base, draft)).toBe('добавить подтягивания')
  })
  it('remove', () => {
    expect(opLabel({ op: 'remove', week: 1, weekday: 1, itemId: 103 }, base, draft)).toBe('убрать отведения на дельты')
  })
  it('reorder', () => {
    expect(opLabel({ op: 'reorder', week: 1, weekday: 1, itemIds: [] }, base, draft)).toBe('порядок упражнений')
  })
  it('an unknown reference gives an empty name', () => {
    expect(opLabel({ op: 'remove', week: 1, weekday: 1, itemId: 999 }, base, draft)).toBe('убрать ')
  })
})

// ---------- 9. editOutcome ----------

describe('editOutcome', () => {
  const prog = (over: Partial<ProgramOut> = {}): ProgramOut => ({ id: 'p-u1', name: 'p', source: null, version: 2, editable: true, basedOn: 'p', weeks: [], ...over })

  it('200 with a program: saved, results default to []', () => {
    const p = prog()
    expect(editOutcome(200, { program: p })).toEqual({ kind: 'saved', program: p, switchedFrom: null, results: [] })
  })

  it('200 with switchedFrom and results', () => {
    const p = prog()
    const results: OpResult[] = [{ op: 0, weeks: [1], skipped: [] }]
    expect(editOutcome(200, { program: p, switchedFrom: 'tpl', results })).toEqual({ kind: 'saved', program: p, switchedFrom: 'tpl', results })
  })

  it('200 with a non-string switchedFrom is null', () => {
    expect(editOutcome(200, { program: prog(), switchedFrom: 5 })).toMatchObject({ kind: 'saved', switchedFrom: null })
  })

  it('200 without a usable program is a failure', () => {
    expect(editOutcome(200, {}).kind).toBe('failed')
    expect(editOutcome(200, null).kind).toBe('failed')
    expect(editOutcome(200, { program: { id: 1, weeks: [] } }).kind).toBe('failed')
    expect(editOutcome(200, { program: { id: 'x' } }).kind).toBe('failed')
  })

  it('409 not_active', () => {
    expect(editOutcome(409, { detail: 'not_active' })).toEqual({ kind: 'not_active' })
  })

  it('409 version with the program', () => {
    const p = prog({ version: 5 })
    expect(editOutcome(409, { detail: 'version', program: p })).toEqual({ kind: 'conflict', program: p })
  })

  it('409 without a program: conflict with null', () => {
    expect(editOutcome(409, { detail: 'version' })).toEqual({ kind: 'conflict', program: null })
    expect(editOutcome(409, null)).toEqual({ kind: 'conflict', program: null })
    expect(editOutcome(409, { detail: 'version', program: { nope: 1 } })).toEqual({ kind: 'conflict', program: null })
  })

  it('422 with a string detail', () => {
    expect(editOutcome(422, { detail: 'Нельзя убрать последнее упражнение дня' })).toEqual({ kind: 'invalid', message: 'Нельзя убрать последнее упражнение дня' })
  })

  it('422 with the FastAPI list: the first message', () => {
    const detail = [{ loc: ['body', 'ops', 0, 'sets'], msg: 'Input should be less than or equal to 20', type: 'x' }, { msg: 'second' }]
    expect(editOutcome(422, { detail })).toEqual({ kind: 'invalid', message: 'Input should be less than or equal to 20' })
  })

  it('422 without a usable detail: a generic message', () => {
    expect(editOutcome(422, null)).toEqual({ kind: 'invalid', message: 'Сервер не принял правку' })
    expect(editOutcome(422, { detail: [] })).toEqual({ kind: 'invalid', message: 'Сервер не принял правку' })
    expect(editOutcome(422, { detail: '' })).toEqual({ kind: 'invalid', message: 'Сервер не принял правку' })
    expect(editOutcome(422, { detail: [{}] })).toEqual({ kind: 'invalid', message: 'Сервер не принял правку' })
  })

  it('404, 500 and a null body are failures', () => {
    expect(editOutcome(404, { detail: 'unknown program' })).toMatchObject({ kind: 'failed', message: 'Программа не найдена на сервере' })
    expect(editOutcome(500, null).kind).toBe('failed')
    expect(editOutcome(500, { detail: 'x' }).kind).toBe('failed')
    expect(editOutcome(0, null).kind).toBe('failed')
    expect(editOutcome(404, null).kind).toBe('failed')
  })
})

// ---------- 10. retargetWorkouts ----------

describe('retargetWorkouts', () => {
  const wk = (id: string, programId: string, startedAt: string): Workout => ({
    id,
    programId,
    week: 1,
    weekday: 1,
    startedAt,
    finishedAt: null,
    exercises: [],
  })
  const START = '2026-09-07'
  const IN_RUN = '2026-09-10T08:00:00.000Z'
  const OLD = '2026-08-20T08:00:00.000Z'
  const state = () => ({
    startDate: START,
    active: wk('a', 'tpl', IN_RUN),
    pending: [wk('p1', 'tpl', IN_RUN), wk('p2', 'tpl', OLD), wk('p3', 'other', IN_RUN)],
    history: [wk('h1', 'tpl', IN_RUN), wk('h2', 'tpl', OLD), wk('h3', 'other', IN_RUN), wk('h4', 'tpl-u1', IN_RUN)],
  })

  it('moves the active, pending and history of the current run', () => {
    const s = retargetWorkouts(state(), 'tpl', 'tpl-u1')
    expect(s.active!.programId).toBe('tpl-u1')
    expect(s.pending.map((w) => w.programId)).toEqual(['tpl-u1', 'tpl', 'other'])
    expect(s.history.map((w) => w.programId)).toEqual(['tpl-u1', 'tpl', 'other', 'tpl-u1'])
  })

  it('earlier runs and other programs are untouched', () => {
    const before = state()
    const s = retargetWorkouts(before, 'tpl', 'tpl-u1')
    expect(s.pending[1]).toBe(before.pending[1])
    expect(s.history[1]).toBe(before.history[1])
    expect(s.pending[2]).toBe(before.pending[2])
    expect(before).toEqual(state()) // the input is not mutated
  })

  it('a workout that started exactly at the local midnight of startDate is in the run', () => {
    const at = new Date(`${START}T00:00:00`).toISOString()
    const before = new Date(new Date(`${START}T00:00:00`).getTime() - 1000).toISOString()
    const s = retargetWorkouts({ ...state(), history: [wk('x', 'tpl', at), wk('y', 'tpl', before)] }, 'tpl', 'c')
    expect(s.history.map((w) => w.programId)).toEqual(['c', 'tpl'])
  })

  it('the active workout moves whatever its start (it is the current run by definition)', () => {
    const s = retargetWorkouts({ ...state(), active: wk('a', 'tpl', OLD) }, 'tpl', 'c')
    expect(s.active!.programId).toBe('c')
  })

  it('no active workout stays null', () => {
    expect(retargetWorkouts({ ...state(), active: null }, 'tpl', 'c').active).toBeNull()
  })

  it('is idempotent', () => {
    const once = retargetWorkouts(state(), 'tpl', 'tpl-u1')
    expect(retargetWorkouts(once, 'tpl', 'tpl-u1')).toEqual(once)
  })

  it('from === to returns the same object', () => {
    const s = state()
    expect(retargetWorkouts(s, 'tpl', 'tpl')).toBe(s)
  })

  it('keeps the other fields of the state', () => {
    const s = retargetWorkouts({ ...state(), extra: 42 }, 'tpl', 'c')
    expect(s.extra).toBe(42)
    expect(s.startDate).toBe(START)
  })
})

// ---------- 11. afterEdit (store.applyEdit's state transition) ----------

describe('afterEdit', () => {
  const wk = (id: string, programId: string, startedAt = '2026-09-10T08:00:00.000Z'): Workout => ({
    id,
    programId,
    week: 1,
    weekday: 1,
    startedAt,
    finishedAt: null,
    exercises: [],
  })
  const base = (programId: string, programVersion: number | null = 1) => ({
    programId,
    programVersion,
    startDate: '2026-09-07',
    active: wk('a', programId),
    pending: [wk('p', programId)],
    history: [wk('h1', programId), wk('h0', programId, '2026-08-01T08:00:00.000Z')],
  })

  it('a fork while on the template: the copy becomes active, the run moves to it, its version is set', () => {
    const s = afterEdit(base('tpl'), { id: 'tpl.u1', version: 1 }, 'tpl')
    expect(s.programId).toBe('tpl.u1')
    expect(s.programVersion).toBe(1)
    expect(s.active!.programId).toBe('tpl.u1')
    expect(s.pending[0].programId).toBe('tpl.u1')
    expect(s.history.map((w) => w.programId)).toEqual(['tpl.u1', 'tpl']) // the earlier run stays on the template
  })

  it('again after the live sync already switched to the copy: only stragglers move, nothing else changes', () => {
    const once = afterEdit(base('tpl'), { id: 'tpl.u1', version: 1 }, 'tpl')
    expect(afterEdit(once, { id: 'tpl.u1', version: 1 }, 'tpl')).toEqual(once)
    // The sync switched programId, but the prepared workout still names the template.
    const synced = { ...base('tpl'), programId: 'tpl.u1' }
    const s = afterEdit(synced, { id: 'tpl.u1', version: 1 }, 'tpl')
    expect(s.programId).toBe('tpl.u1')
    expect(s.active!.programId).toBe('tpl.u1')
  })

  it('another active program is left alone', () => {
    const before = base('other', 4)
    expect(afterEdit(before, { id: 'tpl.u1', version: 1 }, 'tpl')).toBe(before)
  })

  it('a save to an existing copy only bumps the version', () => {
    const before = base('tpl.u1', 3)
    const s = afterEdit(before, { id: 'tpl.u1', version: 4 }, null)
    expect(s).toEqual({ ...before, programVersion: 4 })
    expect(s.active).toBe(before.active)
    expect(s.history).toBe(before.history)
  })
})
