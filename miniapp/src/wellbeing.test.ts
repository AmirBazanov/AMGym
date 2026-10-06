import { describe, expect, it } from 'vitest'
import type { WellbeingEntry } from './api'
import { entriesCount, formatNotedTime, formatWellbeing, formatWellbeingDate, groupByDate, localISODate, wellbeingDetails } from './wellbeing'

function entry(overrides: Partial<WellbeingEntry> = {}): WellbeingEntry {
  return {
    id: 1,
    notedAt: '2026-10-07T08:00:00Z',
    date: '2026-10-07',
    sleepHours: null,
    sleepQuality: null,
    energy: null,
    mood: null,
    pains: [],
    note: null,
    ...overrides,
  }
}

describe('formatWellbeing', () => {
  it('joins sleep, energy and pain in one line', () => {
    const e = entry({ sleepHours: 6, energy: 2, pains: [{ place: 'левое плечо', severity: 3 }] })
    expect(formatWellbeing(e)).toBe('Сон 6 ч · энергия 2/5 · болит левое плечо')
  })

  it('uses a decimal comma for fractional sleep hours', () => {
    expect(formatWellbeing(entry({ sleepHours: 6.5 }))).toBe('Сон 6,5 ч')
  })

  it('uses the plural verb for several pains', () => {
    const e = entry({
      pains: [
        { place: 'спина', severity: 2 },
        { place: 'колено', severity: null },
      ],
    })
    expect(formatWellbeing(e)).toBe('Болят спина, колено')
  })

  it('shows sleep quality when hours are unknown', () => {
    expect(formatWellbeing(entry({ sleepQuality: 3 }))).toBe('Сон 3/5')
  })

  it('hides sleep quality when hours are known', () => {
    expect(formatWellbeing(entry({ sleepHours: 7, sleepQuality: 4 }))).toBe('Сон 7 ч')
  })

  it('capitalizes the first letter when sleep is absent', () => {
    expect(formatWellbeing(entry({ energy: 4, mood: 5 }))).toBe('Энергия 4/5 · настроение 5/5')
  })

  it('ignores pains with a blank place', () => {
    const e = entry({ energy: 3, pains: [{ place: '   ', severity: 4 }] })
    expect(formatWellbeing(e)).toBe('Энергия 3/5')
  })

  it('falls back to the capitalized note when there is nothing else', () => {
    expect(formatWellbeing(entry({ note: '  устал после работы ' }))).toBe('Устал после работы')
  })

  it('says "Без подробностей" for an empty entry', () => {
    expect(formatWellbeing(entry())).toBe('Без подробностей')
    expect(formatWellbeing(entry({ note: '   ', pains: [{ place: ' ', severity: null }] }))).toBe('Без подробностей')
  })
})

describe('wellbeingDetails', () => {
  it('lists rows in a fixed order', () => {
    const e = entry({
      note: 'плохо спал',
      mood: 3,
      pains: [{ place: 'левое плечо', severity: 4 }],
      energy: 2,
      sleepQuality: 2,
      sleepHours: 5.5,
    })
    expect(wellbeingDetails(e)).toEqual([
      { label: 'Сон', value: '5,5 ч' },
      { label: 'Качество сна', value: '2/5' },
      { label: 'Энергия', value: '2/5' },
      { label: 'Настроение', value: '3/5' },
      { label: 'Боли', value: 'левое плечо (4/5)' },
      { label: 'Заметка', value: 'плохо спал' },
    ])
  })

  it('leaves out empty fields', () => {
    expect(wellbeingDetails(entry({ energy: 4 }))).toEqual([{ label: 'Энергия', value: '4/5' }])
  })

  it('shows a pain without severity as just the place', () => {
    const e = entry({
      pains: [
        { place: ' спина ', severity: null },
        { place: 'колено', severity: 2 },
      ],
    })
    expect(wellbeingDetails(e)).toEqual([{ label: 'Боли', value: 'спина, колено (2/5)' }])
  })

  it('is empty for an empty entry', () => {
    expect(wellbeingDetails(entry())).toEqual([])
  })
})

describe('groupByDate', () => {
  it('sorts days and entries newest first regardless of input order', () => {
    const a = entry({ id: 1, date: '2026-10-05', notedAt: '2026-10-05T07:00:00Z' })
    const b = entry({ id: 2, date: '2026-10-07', notedAt: '2026-10-07T07:00:00Z' })
    const c = entry({ id: 3, date: '2026-10-07', notedAt: '2026-10-07T19:00:00Z' })
    const d = entry({ id: 4, date: '2026-10-06', notedAt: '2026-10-06T12:00:00Z' })
    const days = groupByDate([b, a, d, c])
    expect(days.map((x) => x.date)).toEqual(['2026-10-07', '2026-10-06', '2026-10-05'])
    expect(days[0].entries.map((x) => x.id)).toEqual([3, 2])
  })

  it('groups by the server date, not by notedAt', () => {
    const e = entry({ id: 1, notedAt: '2026-10-06T22:30:00Z', date: '2026-10-07' })
    const days = groupByDate([e])
    expect(days).toHaveLength(1)
    expect(days[0].date).toBe('2026-10-07')
    expect(days[0].entries).toEqual([e])
  })

  it('is empty for no entries', () => {
    expect(groupByDate([])).toEqual([])
  })
})

describe('entriesCount', () => {
  it('declines "запись" by number', () => {
    expect(entriesCount(1)).toBe('1 запись')
    expect(entriesCount(2)).toBe('2 записи')
    expect(entriesCount(5)).toBe('5 записей')
    expect(entriesCount(11)).toBe('11 записей')
    expect(entriesCount(21)).toBe('21 запись')
    expect(entriesCount(22)).toBe('22 записи')
  })
})

describe('formatWellbeingDate', () => {
  it('formats day, month and weekday without a UTC shift', () => {
    expect(formatWellbeingDate('2026-10-07')).toBe('7 октября, ср')
  })
})

describe('localISODate', () => {
  it('pads month and day', () => {
    expect(localISODate(new Date(2026, 0, 5))).toBe('2026-01-05')
  })
})

describe('formatNotedTime', () => {
  it('is HH:MM in device-local time', () => {
    const iso = '2026-10-07T04:05:00Z'
    const d = new Date(iso)
    const expected = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    expect(formatNotedTime(iso)).toBe(expected)
    expect(formatNotedTime(iso)).toMatch(/^\d{2}:\d{2}$/)
  })
})
