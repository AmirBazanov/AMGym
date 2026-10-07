import { describe, expect, it } from 'vitest'
import type { BodyWeight } from './api'
import {
  addDays,
  chartPoints,
  dropWeight,
  filterPeriod,
  formatDayShort,
  formatDelta,
  formatWeight,
  parseBodyWeight,
  parseBodyWeights,
  upsertWeight,
  weightChange,
  weightInputError,
  weightSaveErrorText,
} from './bodyWeight'

function w(date: string, weightKg: number, source: BodyWeight['source'] = 'chat'): BodyWeight {
  return { date, weightKg, source }
}

describe('parseBodyWeights', () => {
  it('keeps a valid list oldest first', () => {
    const raw = [w('2026-10-01', 85.2), w('2026-10-02', 84.9, 'miniapp')]
    expect(parseBodyWeights(raw)).toEqual(raw)
  })

  it('sorts unsorted input by date', () => {
    const raw = [w('2026-10-03', 84), w('2026-10-01', 85), w('2026-10-02', 84.5)]
    expect(parseBodyWeights(raw).map((x) => x.date)).toEqual(['2026-10-01', '2026-10-02', '2026-10-03'])
  })

  it('returns an empty list for a non-array', () => {
    for (const raw of [null, undefined, {}, 'x', 42]) expect(parseBodyWeights(raw)).toEqual([])
  })

  it('drops items with a bad or missing date', () => {
    const raw = [
      { date: '2026-1-5', weightKg: 84, source: 'chat' },
      { date: '07.10.2026', weightKg: 84, source: 'chat' },
      { weightKg: 84, source: 'chat' },
      { date: 20261007, weightKg: 84, source: 'chat' },
      w('2026-10-07', 84),
    ]
    expect(parseBodyWeights(raw)).toEqual([w('2026-10-07', 84)])
  })

  it('drops items with a non-number, NaN, infinite or non-positive weight', () => {
    const raw = [
      { date: '2026-10-01', weightKg: '84.6', source: 'chat' },
      { date: '2026-10-02', weightKg: Number.NaN, source: 'chat' },
      { date: '2026-10-03', weightKg: Infinity, source: 'chat' },
      { date: '2026-10-04', weightKg: 0, source: 'chat' },
      { date: '2026-10-05', weightKg: -3, source: 'chat' },
      { date: '2026-10-06', source: 'chat' },
      w('2026-10-07', 84),
    ]
    expect(parseBodyWeights(raw)).toEqual([w('2026-10-07', 84)])
  })

  it('drops null and non-object items', () => {
    expect(parseBodyWeights([null, undefined, 'x', 5, w('2026-10-07', 84)])).toEqual([w('2026-10-07', 84)])
  })

  it('reads an unknown or missing source as chat', () => {
    const raw = [
      { date: '2026-10-01', weightKg: 84, source: 'telegram' },
      { date: '2026-10-02', weightKg: 84 },
    ]
    expect(parseBodyWeights(raw).map((x) => x.source)).toEqual(['chat', 'chat'])
  })

  it('keeps the known sources', () => {
    const raw = [w('2026-10-01', 84, 'chat'), w('2026-10-02', 84, 'miniapp'), w('2026-10-03', 84, 'mcp')]
    expect(parseBodyWeights(raw).map((x) => x.source)).toEqual(['chat', 'miniapp', 'mcp'])
  })

  it('keeps the last item for a duplicate date', () => {
    const raw = [w('2026-10-01', 85), w('2026-10-02', 84), w('2026-10-01', 86, 'mcp')]
    expect(parseBodyWeights(raw)).toEqual([w('2026-10-01', 86, 'mcp'), w('2026-10-02', 84)])
  })
})

describe('parseBodyWeight', () => {
  it('returns null for a non-object', () => {
    for (const raw of [null, undefined, 'x', 42, true]) expect(parseBodyWeight(raw)).toBeNull()
  })

  it('returns the item for a good POST answer', () => {
    expect(parseBodyWeight({ date: '2026-10-07', weightKg: 84.6, source: 'miniapp' })).toEqual(w('2026-10-07', 84.6, 'miniapp'))
  })
})

describe('upsertWeight and dropWeight', () => {
  it('replaces the value of the same date', () => {
    const list = [w('2026-10-01', 85), w('2026-10-02', 84.5)]
    const next = upsertWeight(list, w('2026-10-02', 84.1, 'miniapp'))
    expect(next).toEqual([w('2026-10-01', 85), w('2026-10-02', 84.1, 'miniapp')])
    expect(list[1].weightKg).toBe(84.5)
  })

  it('inserts a new date in order', () => {
    const list = [w('2026-10-01', 85), w('2026-10-03', 84)]
    expect(upsertWeight(list, w('2026-10-02', 84.5)).map((x) => x.date)).toEqual(['2026-10-01', '2026-10-02', '2026-10-03'])
  })

  it('drops by date and leaves the others', () => {
    const list = [w('2026-10-01', 85), w('2026-10-02', 84.5), w('2026-10-03', 84)]
    expect(dropWeight(list, '2026-10-02')).toEqual([w('2026-10-01', 85), w('2026-10-03', 84)])
    expect(dropWeight(list, '2026-12-12')).toEqual(list)
  })
})

describe('addDays', () => {
  it('crosses a month boundary', () => {
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28')
  })

  it('knows leap years', () => {
    expect(addDays('2024-03-01', -1)).toBe('2024-02-29')
  })

  it('crosses a year boundary', () => {
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
  })
})

describe('filterPeriod', () => {
  const list = [w('2026-09-07', 86), w('2026-09-08', 85.5), w('2026-10-01', 85), w('2026-10-08', 84), w('2026-10-09', 83)]

  it('keeps the last N days inclusive and drops future dates', () => {
    expect(filterPeriod(list, 30, '2026-10-08').map((x) => x.date)).toEqual(['2026-09-08', '2026-10-01', '2026-10-08'])
  })

  it('keeps everything for null and returns a copy', () => {
    const all = filterPeriod(list, null, '2026-10-08')
    expect(all).toEqual(list)
    expect(all).not.toBe(list)
  })
})

describe('weightChange', () => {
  it('returns null for an empty list', () => {
    expect(weightChange([], 7)).toBeNull()
  })

  it('returns null for a single entry', () => {
    expect(weightChange([w('2026-10-08', 84)], 7)).toBeNull()
  })

  it('computes the delta rounded to 0.1 and returns the reference entry', () => {
    const from = w('2026-10-01', 85.2)
    const change = weightChange([from, w('2026-10-08', 84.0)], 7)
    expect(change?.delta).toBe(-1.2)
    expect(change?.from).toBe(from)
  })

  it('picks the newest entry dated at or before latest minus N days', () => {
    const list = [w('2026-09-28', 86), w('2026-10-01', 85), w('2026-10-05', 84.5), w('2026-10-08', 84)]
    const change = weightChange(list, 7)
    expect(change?.from.date).toBe('2026-10-01')
    expect(change?.delta).toBe(-1)
  })

  it('returns null when the only older entry is too recent', () => {
    expect(weightChange([w('2026-10-03', 85), w('2026-10-08', 84)], 7)).toBeNull()
  })

  it('returns null when the reference is older than twice the period', () => {
    const list = [w('2026-09-02', 86), w('2026-09-20', 85), w('2026-10-08', 84)]
    // 7 days: the newest candidate is 09-20 (18 days back), older than 2 x 7
    expect(weightChange(list, 7)).toBeNull()
    // 30 days: 09-02 is 36 days back, within 60
    expect(weightChange(list, 30)).toEqual({ delta: -2, from: list[0] })
  })
})

describe('weightInputError', () => {
  it('asks for a value when it is empty or NaN', () => {
    expect(weightInputError(null)).not.toBeNull()
    expect(weightInputError(Number.NaN)).not.toBeNull()
  })

  it('rejects values outside 30..250', () => {
    expect(weightInputError(29.9)).not.toBeNull()
    expect(weightInputError(250.1)).not.toBeNull()
  })

  it('accepts the bounds and ordinary values', () => {
    for (const v of [30, 250, 84.6, 84]) expect(weightInputError(v)).toBeNull()
  })

  it('rejects a step finer than 0.1 instead of rounding', () => {
    expect(weightInputError(84.65)).not.toBeNull()
  })

  it('tolerates float noise such as 70.3', () => {
    expect(weightInputError(70.3)).toBeNull()
  })
})

describe('weightSaveErrorText', () => {
  it('explains the 422 limits', () => {
    const text = weightSaveErrorText(422)
    expect(text).toContain('30')
    expect(text).toContain('250')
  })

  it('asks to log in again on 401 and 403', () => {
    expect(weightSaveErrorText(401)).toBe(weightSaveErrorText(403))
    expect(weightSaveErrorText(401)).toContain('войти')
  })

  it('falls back to a generic message and never leaks server text', () => {
    const generic = weightSaveErrorText(null)
    expect(weightSaveErrorText(500)).toBe(generic)
    expect(generic).not.toBe(weightSaveErrorText(422))
    expect(generic).not.toBe(weightSaveErrorText(401))
    expect(new Set([422, 401, 403, 500, null].map(weightSaveErrorText)).size).toBe(3)
  })
})

describe('formatting', () => {
  it('formats a weight with a decimal comma and one digit', () => {
    expect(formatWeight(84.6)).toBe('84,6')
    expect(formatWeight(84)).toBe('84,0')
  })

  it('formats a delta with a sign and a real minus', () => {
    expect(formatDelta(0.4)).toBe('+0,4')
    expect(formatDelta(-1.2)).toBe('−1,2')
  })

  it('formats a zero or rounded-to-zero delta without a sign', () => {
    expect(formatDelta(0)).toBe('0,0')
    expect(formatDelta(-0.04)).toBe('0,0')
  })

  it('formats a short day without a time zone shift', () => {
    expect(formatDayShort('2026-10-07')).toBe('07.10')
  })

  it('maps chart points to label and kg', () => {
    expect(chartPoints([w('2026-10-01', 85.2), w('2026-10-08', 84)])).toEqual([
      { label: '01.10', kg: 85.2 },
      { label: '08.10', kg: 84 },
    ])
  })
})
