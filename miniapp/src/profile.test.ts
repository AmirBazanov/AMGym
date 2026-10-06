import { describe, expect, it } from 'vitest'
import type { Profile } from './api'
import { ABOUT_MAX, GOALS, normalizeProfile, profileErrors, profileLimits, profilePatch } from './profile'

const NOW = new Date('2026-10-07T12:00:00Z')

/** Local copy: api.ts reads `window` at load, so it cannot be value-imported in node. */
const EMPTY: Profile = { weightKg: null, heightCm: null, birthYear: null, goal: null, about: null }
const profile = (over: Partial<Profile> = {}): Profile => ({ ...EMPTY, ...over })

describe('GOALS / ABOUT_MAX', () => {
  it('lists the four goals in order', () => {
    expect(GOALS.map((g) => g.key)).toEqual(['mass', 'cut', 'strength', 'health'])
    expect(GOALS.every((g) => g.label.length > 0)).toBe(true)
  })

  it('limits about to 500 characters', () => {
    expect(ABOUT_MAX).toBe(500)
  })
})

describe('profileLimits', () => {
  it('uses the server ranges and a birth year bound of (current year - 10)', () => {
    expect(profileLimits(NOW)).toEqual({
      weightKg: { min: 30, max: 300 },
      heightCm: { min: 120, max: 250 },
      birthYear: { min: 1930, max: 2016 },
    })
  })

  it('moves the birth year bound with the calendar', () => {
    expect(profileLimits(new Date('2030-06-15T12:00:00Z')).birthYear.max).toBe(2020)
  })
})

describe('normalizeProfile', () => {
  it('rounds weight to 0.1 kg', () => {
    expect(normalizeProfile(profile({ weightKg: 82.46 })).weightKg).toBe(82.5)
    expect(normalizeProfile(profile({ weightKg: 82.44 })).weightKg).toBe(82.4)
    expect(normalizeProfile(profile({ weightKg: 80 })).weightKg).toBe(80)
  })

  it('rounds height and birth year to whole numbers', () => {
    const n = normalizeProfile(profile({ heightCm: 180.4, birthYear: 1990.6 }))
    expect(n.heightCm).toBe(180)
    expect(n.birthYear).toBe(1991)
  })

  it('keeps null numbers and the goal untouched', () => {
    expect(normalizeProfile(profile({ goal: 'cut' }))).toEqual(profile({ goal: 'cut' }))
  })

  it('trims about and turns blank text into null', () => {
    expect(normalizeProfile(profile({ about: '  болит колено \n' })).about).toBe('болит колено')
    expect(normalizeProfile(profile({ about: '   \n\t ' })).about).toBeNull()
    expect(normalizeProfile(profile({ about: '' })).about).toBeNull()
    expect(normalizeProfile(profile({ about: null })).about).toBeNull()
  })
})

describe('profileErrors', () => {
  it('accepts an empty profile: null values clear fields', () => {
    expect(profileErrors(EMPTY, NOW)).toEqual({})
  })

  it('accepts weight boundaries and rejects values outside', () => {
    expect(profileErrors(profile({ weightKg: 30 }), NOW)).toEqual({})
    expect(profileErrors(profile({ weightKg: 300 }), NOW)).toEqual({})
    expect(profileErrors(profile({ weightKg: 29.9 }), NOW)).toEqual({ weightKg: true })
    expect(profileErrors(profile({ weightKg: 300.1 }), NOW)).toEqual({ weightKg: true })
  })

  it('accepts height boundaries and rejects values outside', () => {
    expect(profileErrors(profile({ heightCm: 120 }), NOW)).toEqual({})
    expect(profileErrors(profile({ heightCm: 250 }), NOW)).toEqual({})
    expect(profileErrors(profile({ heightCm: 119 }), NOW)).toEqual({ heightCm: true })
    expect(profileErrors(profile({ heightCm: 251 }), NOW)).toEqual({ heightCm: true })
  })

  it('checks birth year against 1930..(year - 10)', () => {
    expect(profileErrors(profile({ birthYear: 1930 }), NOW)).toEqual({})
    expect(profileErrors(profile({ birthYear: 2016 }), NOW)).toEqual({})
    expect(profileErrors(profile({ birthYear: 1929 }), NOW)).toEqual({ birthYear: true })
    expect(profileErrors(profile({ birthYear: 2017 }), NOW)).toEqual({ birthYear: true })
  })

  it('validates the rounded value, not the raw one', () => {
    // 29.96 rounds to 30.0 which is in range; 29.94 rounds to 29.9 which is not
    expect(profileErrors(profile({ weightKg: 29.96 }), NOW)).toEqual({})
    expect(profileErrors(profile({ weightKg: 29.94 }), NOW)).toEqual({ weightKg: true })
  })

  it('accepts about of exactly 500 characters and rejects 501', () => {
    expect(profileErrors(profile({ about: 'a'.repeat(ABOUT_MAX) }), NOW)).toEqual({})
    expect(profileErrors(profile({ about: 'a'.repeat(ABOUT_MAX + 1) }), NOW)).toEqual({ about: true })
  })

  it('measures about length after trimming', () => {
    expect(profileErrors(profile({ about: `  ${'a'.repeat(ABOUT_MAX)}  ` }), NOW)).toEqual({})
    expect(profileErrors(profile({ about: ' '.repeat(600) }), NOW)).toEqual({})
  })

  it('reports every bad field at once', () => {
    const bad = profile({ weightKg: 5, heightCm: 500, birthYear: 1800, about: 'a'.repeat(501) })
    expect(profileErrors(bad, NOW)).toEqual({ weightKg: true, heightCm: true, birthYear: true, about: true })
  })
})

describe('profilePatch', () => {
  const saved = profile({ weightKg: 80, heightCm: 180, birthYear: 1990, goal: 'mass', about: 'note' })

  it('is empty when nothing changed', () => {
    expect(profilePatch({ ...saved }, saved)).toEqual({})
  })

  it('ignores whitespace and rounding differences', () => {
    const draft = profile({ weightKg: 80.04, heightCm: 180.2, birthYear: 1990, goal: 'mass', about: '  note  ' })
    expect(profilePatch(draft, saved)).toEqual({})
  })

  it('treats blank about and null about as the same', () => {
    expect(profilePatch(profile({ about: '   ' }), EMPTY)).toEqual({})
  })

  it('returns only the changed keys, normalized', () => {
    expect(profilePatch({ ...saved, weightKg: 81.46, about: '  new  ' }, saved)).toEqual({
      weightKg: 81.5,
      about: 'new',
    })
  })

  it('includes a null for a cleared field', () => {
    expect(profilePatch({ ...saved, weightKg: null }, saved)).toEqual({ weightKg: null })
    expect(profilePatch({ ...saved, about: '   ' }, saved)).toEqual({ about: null })
  })

  it('includes a changed goal and a goal cleared to null', () => {
    expect(profilePatch({ ...saved, goal: 'cut' }, saved)).toEqual({ goal: 'cut' })
    expect(profilePatch({ ...saved, goal: null }, saved)).toEqual({ goal: null })
  })

  it('includes a value set on a previously empty profile', () => {
    expect(profilePatch(profile({ heightCm: 175 }), EMPTY)).toEqual({ heightCm: 175 })
  })
})
