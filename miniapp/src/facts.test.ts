import { describe, expect, it } from 'vitest'
import type { Fact } from './api'
import { activeCount, categoryLabel, cleanFactText, FACT_CATEGORIES, factTextError, knownCategory, sortFacts } from './facts'

const fact = (o: Partial<Fact>): Fact => ({ id: 1, text: 'x', category: 'other', createdAt: '2026-10-07T05:00:00Z', active: true, ...o })

describe('categoryLabel', () => {
  it('names every category in Russian', () => {
    expect(categoryLabel('food')).toBe('Еда')
    expect(categoryLabel('training')).toBe('Тренировки')
    expect(categoryLabel('health')).toBe('Здоровье')
    expect(categoryLabel('schedule')).toBe('Расписание')
    expect(categoryLabel('other')).toBe('Прочее')
  })

  it('reads unknown, null and undefined as "Прочее"', () => {
    expect(categoryLabel('sleep')).toBe('Прочее')
    expect(categoryLabel(null)).toBe('Прочее')
    expect(categoryLabel(undefined)).toBe('Прочее')
  })
})

describe('FACT_CATEGORIES', () => {
  it('lists the five contract categories once, "other" last', () => {
    expect(FACT_CATEGORIES.map((c) => c.key)).toEqual(['food', 'training', 'health', 'schedule', 'other'])
  })
})

describe('knownCategory', () => {
  it('keeps known categories and maps the rest to "other"', () => {
    expect(knownCategory('health')).toBe('health')
    expect(knownCategory('mood')).toBe('other')
    expect(knownCategory(null)).toBe('other')
  })
})

describe('sortFacts', () => {
  it('puts the newest first and breaks ties by id', () => {
    const list = [
      fact({ id: 1, createdAt: '2026-10-01T10:00:00Z' }),
      fact({ id: 2, createdAt: '2026-10-07T10:00:00Z' }),
      fact({ id: 3, createdAt: '2026-10-07T10:00:00Z' }),
      fact({ id: 4, createdAt: '2026-10-05T10:00:00Z' }),
    ]
    expect(sortFacts(list).map((f) => f.id)).toEqual([3, 2, 4, 1])
  })

  it('does not mutate the input', () => {
    const list = [fact({ id: 1, createdAt: '2026-10-01T00:00:00Z' }), fact({ id: 2, createdAt: '2026-10-02T00:00:00Z' })]
    sortFacts(list)
    expect(list.map((f) => f.id)).toEqual([1, 2])
  })
})

describe('activeCount', () => {
  it('counts only active facts', () => {
    expect(activeCount([fact({ id: 1 }), fact({ id: 2, active: false }), fact({ id: 3 })])).toBe(2)
    expect(activeCount([])).toBe(0)
  })
})

describe('cleanFactText and factTextError', () => {
  it('trims and collapses whitespace', () => {
    expect(cleanFactText('  не ем \n  творог ')).toBe('не ем творог')
  })

  it('rejects empty and whitespace-only text', () => {
    expect(factTextError('', 200)).toBe('empty')
    expect(factTextError('   \n', 200)).toBe('empty')
  })

  it('checks the length after cleaning', () => {
    expect(factTextError('a'.repeat(200), 200)).toBeNull()
    expect(factTextError('a'.repeat(201), 200)).toBe('long')
    expect(factTextError(`  ${'a'.repeat(200)}  `, 200)).toBeNull()
  })
})
