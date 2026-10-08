import { afterEach, describe, expect, it } from 'vitest'
import { dayFocus, findProgram, getProgram, PROGRAMS, setServerPrograms, type Program, type ProgramDay } from './program'

afterEach(() => setServerPrograms({}))

const SLUG = 'arms_specialization_8w'

describe('program registry', () => {
  it('finds the bundled program when no server programs are loaded', () => {
    setServerPrograms({})
    expect(findProgram(SLUG)).toBe(PROGRAMS[0])
    expect(getProgram(SLUG)).toBe(PROGRAMS[0])
  })

  it('findProgram is undefined for an unknown slug and null, getProgram falls back to the first bundled', () => {
    setServerPrograms({})
    expect(findProgram('copy-x')).toBeUndefined()
    expect(getProgram('copy-x')).toBe(PROGRAMS[0])
    expect(findProgram(null)).toBeUndefined()
    expect(getProgram(null)).toBe(PROGRAMS[0])
  })

  it('prefers the server copy over the bundled one', () => {
    const serverCopy: Program = { ...PROGRAMS[0], name: 'Серверная', version: 2 }
    setServerPrograms({ [SLUG]: serverCopy })
    expect(getProgram(SLUG)).toBe(serverCopy)
    expect(findProgram(SLUG)).toBe(serverCopy)
    expect(getProgram(SLUG).version).toBe(2)
  })

  it('finds a server-only copy', () => {
    const copy: Program = { ...PROGRAMS[0], id: 'copy-x', version: 1, editable: true }
    setServerPrograms({ 'copy-x': copy })
    expect(findProgram('copy-x')).toBe(copy)
  })

  it('resets with an empty registry', () => {
    setServerPrograms({ 'copy-x': { ...PROGRAMS[0], id: 'copy-x' } })
    setServerPrograms({})
    expect(findProgram('copy-x')).toBeUndefined()
  })

  it.each(['__proto__', 'toString', 'constructor', 'hasOwnProperty', 'valueOf'])(
    'does not return Object.prototype stuff for %s',
    (slug) => {
      setServerPrograms({})
      expect(findProgram(slug)).toBeUndefined()
      expect(getProgram(slug)).toBe(PROGRAMS[0])
      setServerPrograms({ [SLUG]: PROGRAMS[0] })
      expect(findProgram(slug)).toBeUndefined()
    },
  )
})

function dayOf(weekday: number, focus?: string | null): ProgramDay {
  const d: ProgramDay = { weekday, title: 'день', exercises: [] }
  if (focus !== undefined) d.focus = focus
  return d
}

describe('dayFocus', () => {
  it('uses the day focus when set', () => {
    expect(dayFocus(dayOf(1, 'Ноги'))).toBe('Ноги')
    expect(dayFocus(dayOf(3, 'Ноги'))).toBe('Ноги')
    expect(dayFocus(dayOf(3, 'Ноги'), true)).toBe('Ноги')
  })

  it('short form is the first word of the focus', () => {
    expect(dayFocus(dayOf(1, 'Руки и плечи'), true)).toBe('Руки')
    expect(dayFocus(dayOf(1, 'Руки и плечи'))).toBe('Руки и плечи')
    expect(dayFocus(dayOf(5, '  Спина   и бицепс '), true)).toBe('Спина')
    expect(dayFocus(dayOf(5, '  Спина   и бицепс '))).toBe('Спина   и бицепс')
  })

  it.each([
    ['undefined', undefined],
    ['null', null],
    ['empty', ''],
    ['blank', '   '],
  ])('falls back to the old rule when the focus is %s', (_n, focus) => {
    expect(dayFocus(dayOf(3, focus))).toBe('База')
    expect(dayFocus(dayOf(3, focus), true)).toBe('База')
    expect(dayFocus(dayOf(1, focus))).toBe('Руки и плечи')
    expect(dayFocus(dayOf(1, focus), true)).toBe('Руки')
    expect(dayFocus(dayOf(5, focus))).toBe('Руки и плечи')
    expect(dayFocus(dayOf(5, focus), true)).toBe('Руки')
  })

  it('bundled program days look the same as under the old rule', () => {
    const days = PROGRAMS[0].weeks.flatMap((w) => w.days)
    expect(days.length).toBeGreaterThan(0)
    for (const d of days) {
      expect(d.focus).toBeTruthy()
      expect(dayFocus(d)).toBe(d.weekday === 3 ? 'База' : 'Руки и плечи')
      expect(dayFocus(d, true)).toBe(d.weekday === 3 ? 'База' : 'Руки')
    }
  })
})
