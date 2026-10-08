import { describe, expect, it } from 'vitest'
import { editDistance, exactName, matchScore, searchNames } from './pickerSearch'

const NAMES = [
  'жим лёжа',
  'жим гантелей сидя',
  'тяга верхнего блока',
  'сгибания с гантелями на бицепс с супинацией',
  'французский жим в блоке из-за головы',
  'отведения на дельты',
]

describe('editDistance', () => {
  it('counts edits and stops early above max', () => {
    expect(editDistance('жим', 'жим')).toBe(0)
    expect(editDistance('бицепс', 'бицепц')).toBe(1)
    expect(editDistance('abc', 'xyz', 1)).toBe(2)
    expect(editDistance('a', 'abcdef', 2)).toBe(3)
  })
})

describe('matchScore', () => {
  it('ranks exact, prefix, word prefix, substring, any-order words, typos', () => {
    expect(matchScore('жим лёжа', 'жим лежа')).toBe(0) // ё/е
    expect(matchScore('жим лёжа', 'жим')).toBe(1)
    expect(matchScore('тяга верхнего блока', 'блок')).toBe(2)
    expect(matchScore('тяга верхнего блока', 'ерхн')).toBe(3)
    expect(matchScore('тяга верхнего блока', 'блок тяг')).toBe(4)
    expect(matchScore('отведения на дельты', 'отвидения')).toBe(5)
    expect(matchScore('жим лёжа', 'присед')).toBeNull()
  })

  it('allows no typo in short words', () => {
    expect(matchScore('жим лёжа', 'жом')).toBeNull()
  })
})

describe('searchNames', () => {
  it('keeps the order without a query', () => {
    expect(searchNames(NAMES, '  ')).toEqual(NAMES)
  })

  it('puts better matches first, ties in the given order', () => {
    expect(searchNames(NAMES, 'жим')).toEqual(['жим лёжа', 'жим гантелей сидя', 'французский жим в блоке из-за головы'])
  })

  it('finds a misspelled name so a twin is not created', () => {
    expect(searchNames(NAMES, 'сгибания на бицепц')).toEqual(['сгибания с гантелями на бицепс с супинацией'])
  })
})

describe('exactName', () => {
  it('ignores case, ё/е and spaces', () => {
    expect(exactName(NAMES, '  Жим   лежа ')).toBe('жим лёжа')
    expect(exactName(NAMES, 'жим')).toBeUndefined()
    expect(exactName(NAMES, '')).toBeUndefined()
  })
})
