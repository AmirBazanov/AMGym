import { describe, expect, it } from 'vitest'
import { KIND_DEFAULTS, KIND_TITLE, REMINDER_WEEKDAYS, reminderTitle, reminderWhen, repeatPhrase, weekdayShort } from './reminders'

describe('weekdayShort', () => {
  it('is 0-based from Monday', () => {
    expect(weekdayShort(0)).toBe('Пн')
    expect(weekdayShort(6)).toBe('Вс')
    expect(REMINDER_WEEKDAYS).toHaveLength(7)
  })

  it('is empty for null, undefined and out-of-range values', () => {
    expect(weekdayShort(null)).toBe('')
    expect(weekdayShort(undefined)).toBe('')
    expect(weekdayShort(7)).toBe('')
    expect(weekdayShort(-1)).toBe('')
  })
})

describe('reminderWhen', () => {
  it('prefixes the weekday for weekly reminders', () => {
    expect(reminderWhen({ time: '09:00', weekday: 0 })).toBe('Пн 09:00')
    expect(reminderWhen({ time: '19:00', weekday: 6 })).toBe('Вс 19:00')
  })

  it('is just the time for daily reminders', () => {
    expect(reminderWhen({ time: '09:00', weekday: null })).toBe('09:00')
    expect(reminderWhen({ time: '09:00' })).toBe('09:00')
  })
})

describe('repeatPhrase', () => {
  it('says "every day" without a weekday', () => {
    expect(repeatPhrase(null)).toBe('каждый день')
    expect(repeatPhrase(undefined)).toBe('каждый день')
  })

  it('uses the plural weekday form', () => {
    expect(repeatPhrase(0)).toBe('по понедельникам')
    expect(repeatPhrase(6)).toBe('по воскресеньям')
  })

  it('falls back to "every day" for an unknown weekday', () => {
    expect(repeatPhrase(9)).toBe('каждый день')
  })
})

describe('reminderTitle', () => {
  it('shows the text of a text reminder', () => {
    expect(reminderTitle({ kind: 'text', text: 'Выпить воды' })).toBe('Выпить воды')
  })

  it('is empty for a text reminder without text', () => {
    expect(reminderTitle({ kind: 'text', text: null })).toBe('')
  })

  it('uses a fixed title for nutrition and advice', () => {
    expect(reminderTitle({ kind: 'nutrition', text: null })).toBe('Сводка КБЖУ')
    expect(reminderTitle({ kind: 'advice', text: null })).toBe('Советы недели')
    expect(KIND_TITLE.nutrition).toBe('Сводка КБЖУ')
  })

  it('ignores stray text on non-text kinds', () => {
    expect(reminderTitle({ kind: 'nutrition', text: 'x' })).toBe('Сводка КБЖУ')
  })
})

describe('KIND_DEFAULTS', () => {
  it('suggests weekly Sunday evening for advice', () => {
    expect(KIND_DEFAULTS.advice).toEqual({ time: '19:00', weekday: 6 })
  })

  it('suggests a daily morning slot for text and nutrition', () => {
    expect(KIND_DEFAULTS.text).toEqual({ time: '09:00', weekday: null })
    expect(KIND_DEFAULTS.nutrition).toEqual({ time: '09:00', weekday: null })
  })
})
