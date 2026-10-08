import { describe, expect, it } from 'vitest'
import type { Intensity, Prescription, ProgramExercise } from './program'
import type { Baseline, SetEntry, Workout } from './store'
import {
  bestE1rm,
  doubleProgression,
  equipmentStep,
  findBaseline,
  lastSameSession,
  normalizeBaselines,
  percentOf1rm,
  roundToStep,
  suggestWeight,
} from './progression'

/** Workout with one exercise; sets are [weight, reps] pairs, all done. */
function workout(date: string, name: string, sets: [number, number][]): Workout {
  return {
    id: `w-${date}`,
    programId: 'p',
    week: 1,
    weekday: 1,
    startedAt: date,
    finishedAt: date,
    exercises: [
      {
        name,
        target: '',
        dropset: false,
        sets: sets.map(([weight, reps]) => ({ weight, reps, done: true })),
      },
    ],
  }
}

const RANGE: Prescription = { sets: 3, reps_min: 8, reps_max: 12, drop_reps: null, raw: '3х8-12' }
const DROPSET: Prescription = { sets: 3, reps_min: null, reps_max: null, drop_reps: [12, 6, 6], raw: 'дропсет 3х12-6-6' }

function ex(name: string, intensity: Intensity | null, prescription: Partial<Prescription> = {}): ProgramExercise {
  return { name, intensity, prescription: { ...RANGE, ...prescription }, order: 1 }
}

function sets(...pairs: [number, number][]): SetEntry[] {
  return pairs.map(([weight, reps]) => ({ weight, reps, done: true }))
}

describe('equipmentStep', () => {
  it('uses 1 kg for dumbbell exercises', () => {
    expect(equipmentStep('сгибания с гантелями на бицепс')).toBe(1)
  })

  it('is case-insensitive', () => {
    expect(equipmentStep('Жим ГАНТЕЛЕЙ сидя')).toBe(1)
  })

  it('uses 2.5 kg for barbells', () => {
    expect(equipmentStep('жим лёжа')).toBe(2.5)
  })

  it('uses 2.5 kg for machines and cables', () => {
    expect(equipmentStep('отведения на дельты')).toBe(2.5)
  })
})

describe('roundToStep', () => {
  it('rounds down to the nearest step', () => {
    expect(roundToStep(61, 2.5)).toBe(60)
  })

  it('rounds up to the nearest step', () => {
    expect(roundToStep(61.3, 2.5)).toBe(62.5)
  })

  it('rounds to whole kilograms for step 1', () => {
    expect(roundToStep(14.4, 1)).toBe(14)
  })

  it('keeps a weight that is already on the grid exactly', () => {
    expect(roundToStep(62.5, 2.5)).toBe(62.5)
  })
})

describe('percentOf1rm', () => {
  const heavy8 = 1 / (1 + 8 / 30)

  it('heavy is the inverse Epley share', () => {
    expect(percentOf1rm(8, 'heavy')).toBeCloseTo(heavy8, 10)
  })

  it('medium is 90 % of heavy', () => {
    expect(percentOf1rm(8, 'medium')).toBeCloseTo(heavy8 * 0.9, 10)
  })

  it('light is 80 % of heavy', () => {
    expect(percentOf1rm(8, 'light')).toBeCloseTo(heavy8 * 0.8, 10)
  })

  it('no intensity counts as medium', () => {
    expect(percentOf1rm(8, null)).toBeCloseTo(percentOf1rm(8, 'medium'), 10)
  })

  it('falls back to reps_min when reps_max is missing', () => {
    expect(percentOf1rm(null, 'heavy', 6)).toBeCloseTo(percentOf1rm(6, 'heavy'), 10)
  })

  it('falls back to 10 reps when no reps are known', () => {
    expect(percentOf1rm(null, 'heavy', null)).toBeCloseTo(percentOf1rm(10, 'heavy'), 10)
  })
})

describe('bestE1rm', () => {
  const NAME = 'жим лёжа'

  it('picks the best Epley estimate, not the heaviest or the highest volume', () => {
    const history = [
      workout('2026-10-01T10:00:00Z', NAME, [[60, 12]]), // 84
      workout('2026-10-03T10:00:00Z', NAME, [[70, 8]]), // 88.67
      workout('2026-10-05T10:00:00Z', NAME, [[80, 3]]), // 88
    ]
    const best = bestE1rm(history, NAME)
    expect(best).not.toBeNull()
    expect(best!.weight).toBe(70)
    expect(best!.reps).toBe(8)
    expect(best!.date).toBe('2026-10-03T10:00:00Z')
    expect(best!.e1rm).toBeCloseTo(88.667, 3)
  })

  it('ignores sets that are not done or have no weight', () => {
    const w = workout('2026-10-01T10:00:00Z', NAME, [[60, 10]])
    w.exercises[0].sets.push(
      { weight: 200, reps: 5, done: false },
      { weight: null, reps: 20, done: true },
    )
    const best = bestE1rm([w], NAME)
    expect(best?.weight).toBe(60)
    expect(best?.reps).toBe(10)
  })

  it('returns null when the exercise was never done', () => {
    expect(bestE1rm([workout('2026-10-01T10:00:00Z', 'присед', [[100, 5]])], NAME)).toBeNull()
    expect(bestE1rm([], NAME)).toBeNull()
  })

  it('counts missing reps as 1', () => {
    const w = workout('2026-10-01T10:00:00Z', NAME, [])
    w.exercises[0].sets.push({ weight: 90, reps: null, done: true })
    const best = bestE1rm([w], NAME)
    expect(best?.reps).toBe(1)
    expect(best?.e1rm).toBe(90)
  })
})

describe('lastSameSession', () => {
  const NAME = 'жим лёжа'

  it('returns the done weighted sets of the latest workout with the exercise', () => {
    const older = workout('2026-10-01T10:00:00Z', NAME, [[50, 10]])
    const latest = workout('2026-10-03T10:00:00Z', NAME, [
      [60, 10],
      [60, 9],
    ])
    latest.exercises[0].sets.push(
      { weight: 70, reps: 3, done: false },
      { weight: null, reps: 8, done: true },
    )
    expect(lastSameSession([older, latest], NAME)).toEqual(sets([60, 10], [60, 9]))
  })

  it('skips a newer workout that does not have the exercise', () => {
    const withEx = workout('2026-10-01T10:00:00Z', NAME, [[55, 8]])
    const without = workout('2026-10-03T10:00:00Z', 'присед', [[100, 5]])
    expect(lastSameSession([withEx, without], NAME)).toEqual(sets([55, 8]))
  })

  it('returns null when no workout has the exercise', () => {
    expect(lastSameSession([workout('2026-10-01T10:00:00Z', 'присед', [[100, 5]])], NAME)).toBeNull()
    expect(lastSameSession([], NAME)).toBeNull()
  })
})

describe('doubleProgression', () => {
  it('adds a step when every set hit the top of the range', () => {
    expect(doubleProgression(sets([60, 12], [60, 12], [60, 12]), RANGE, 2.5)).toBe(62.5)
  })

  it('adds a step when sets exceed the top of the range', () => {
    expect(doubleProgression(sets([60, 12], [60, 12], [60, 13]), RANGE, 2.5)).toBe(62.5)
  })

  it('keeps the weight when one set is one rep short', () => {
    expect(doubleProgression(sets([60, 12], [60, 12], [60, 11]), RANGE, 2.5)).toBe(60)
  })

  it('does not lower the weight when a set is below reps_min', () => {
    expect(doubleProgression(sets([60, 12], [60, 12], [60, 7]), RANGE, 2.5)).toBe(60)
  })

  it('keeps the top weight for a pyramid with different weights', () => {
    expect(doubleProgression(sets([60, 12], [57.5, 12]), RANGE, 2.5)).toBe(60)
  })

  it('returns null without history', () => {
    expect(doubleProgression(null, RANGE, 2.5)).toBeNull()
    expect(doubleProgression([], RANGE, 2.5)).toBeNull()
  })

  it('dropset: all first drops at 12 reps add a step', () => {
    expect(doubleProgression(sets([20, 12], [20, 12], [20, 12]), DROPSET, 1)).toBe(21)
  })

  it('dropset: a set below the first drop target keeps the weight', () => {
    expect(doubleProgression(sets([20, 12], [20, 10]), DROPSET, 1)).toBe(20)
  })

  it('uses reps_min as the target when reps_max is missing', () => {
    const p: Prescription = { sets: 3, reps_min: 10, reps_max: null, drop_reps: null, raw: '3х10' }
    expect(doubleProgression(sets([50, 10], [50, 10], [50, 10]), p, 2.5)).toBe(52.5)
  })
})

describe('suggestWeight', () => {
  const BENCH = 'жим лёжа'

  // Since the related-exercise transfer suggestWeight never returns null: no number is source "none".
  it('has no number without history', () => {
    expect(suggestWeight([], ex(BENCH, 'medium'))).toMatchObject({ weight: null, source: 'none' })
  })

  it('has no number when the history is about an unrelated exercise', () => {
    const history = [workout('2026-10-01T10:00:00Z', 'присед', [[100, 5]])]
    expect(suggestWeight(history, ex(BENCH, 'medium'))).toMatchObject({ weight: null, source: 'none' })
  })

  it('double progression wins over a lower record-based weight', () => {
    const history = [
      workout('2026-10-01T10:00:00Z', BENCH, [
        [60, 12],
        [60, 12],
        [60, 12],
      ]),
    ]
    const s = suggestWeight(history, ex(BENCH, 'medium'))
    expect(s?.weight).toBe(62.5)
    expect(s?.reason).toContain('прошлый раз 60 × 12 во всех подходах, +2,5 кг')
  })

  it('the record wins when it gives a higher weight', () => {
    const history = [
      workout('2026-09-20T10:00:00Z', BENCH, [[100, 5]]), // e1rm 116.67
      workout('2026-10-01T10:00:00Z', BENCH, [
        [60, 10],
        [60, 10],
        [60, 10],
      ]),
    ]
    const s = suggestWeight(history, ex(BENCH, 'heavy'))
    // 116.67 * 1 / (1 + 12 / 30) = 83.33 -> 82.5 on the 2.5 kg grid
    expect(s?.weight).toBe(roundToStep(116.67 * (1 / (1 + 12 / 30)), 2.5))
    expect(s?.weight).toBe(82.5)
    expect(s?.reason.startsWith('от рекорда 117 кг (1ПМ), 71 % для 12 повторов')).toBe(true)
  })

  it('keeps the same weight when reps were missed and the record is lower', () => {
    const history = [
      workout('2026-10-01T10:00:00Z', BENCH, [
        [60, 10],
        [60, 9],
        [60, 8],
      ]),
    ]
    const s = suggestWeight(history, ex(BENCH, 'medium'))
    expect(s?.weight).toBe(60)
    expect(s?.reason).toBe('как в прошлый раз 60 кг')
  })

  it('dropset with dumbbells: +1 kg beats the record-based weight', () => {
    const history = [
      workout('2026-10-01T10:00:00Z', 'сгибания с гантелями', [
        [20, 12],
        [20, 12],
        [20, 12],
      ]),
    ]
    // record 20 * 1.4 = 28, 28 / 1.4 = 20 < 21
    const s = suggestWeight(history, ex('сгибания с гантелями', 'heavy', DROPSET))
    expect(s?.weight).toBe(21)
  })
})

function bl(exercise: string, weightKg: number, reps: number | null, factId = 1): Baseline {
  return { exercise, weightKg, reps, factId }
}

describe('normalizeBaselines', () => {
  it('treats an absent field (older server) or a non-list as none', () => {
    expect(normalizeBaselines(undefined)).toEqual([])
    expect(normalizeBaselines(null)).toEqual([])
    expect(normalizeBaselines({})).toEqual([])
  })

  it('drops entries without a name or a positive weight, reps below 1 are unknown', () => {
    const raw = [
      bl('жим лёжа', 90, 8),
      bl('  ', 90, 8),
      bl('присед', 0, 5),
      bl('присед', Number.NaN, 5),
      { exercise: 'тяга', reps: 5, factId: 2 },
      bl('румынская тяга', 100, 0, 3),
      null,
    ]
    expect(normalizeBaselines(raw)).toEqual([bl('жим лёжа', 90, 8), bl('румынская тяга', 100, null, 3)])
  })
})

describe('findBaseline', () => {
  it('ignores case and extra whitespace', () => {
    expect(findBaseline([bl(' Жим  ЛЁЖА ', 90, 8)], 'жим лёжа')?.weightKg).toBe(90)
  })

  it('treats ё and е as the same letter, both ways (the server folds it too)', () => {
    expect(findBaseline([bl('жим лежа', 90, 8)], 'жим лёжа')?.weightKg).toBe(90)
    expect(findBaseline([bl('жим лёжа', 90, 8)], 'жим лежа')?.weightKg).toBe(90)
    expect(findBaseline([bl('ЖИМ ЛЁЖА', 90, 8)], 'Жим лежа')?.weightKg).toBe(90)
  })

  it('needs the whole name: "жим лёжа" is not "жим лёжа 30°"', () => {
    expect(findBaseline([bl('жим лёжа', 90, 8)], 'жим лёжа 30°')).toBeNull()
    expect(findBaseline([bl('жим лёжа 30°', 70, 8)], 'жим лёжа')).toBeNull()
  })

  it('takes the newest entry (highest factId) when there are several', () => {
    const list = [bl('жим лёжа', 95, 5, 7), bl('жим лёжа', 90, 8, 3)]
    expect(findBaseline(list, 'жим лёжа')).toEqual(bl('жим лёжа', 95, 5, 7))
  })
})

describe('suggestWeight from baselines', () => {
  // The owner's own words from the bot chat.
  const OWNER = [
    bl('жим лёжа', 90, 8, 1),
    bl('тяга вертикального блока', 80, 10, 2),
    bl('румынская тяга', 100, null, 3),
    bl('присед со штангой', 140, 1, 4),
  ]

  it('without a third argument behaves as before', () => {
    expect(suggestWeight([], ex('жим лёжа', 'heavy'))).toMatchObject({ weight: null, source: 'none' })
  })

  it('reps known: Epley 1RM and the same share as the record branch', () => {
    // 90 × (1 + 8 / 30) = 114; heavy for 12 reps: 114 / 1.4 = 81.4 -> 82.5
    const s = suggestWeight([], ex('жим лёжа', 'heavy'), OWNER)
    expect(s?.weight).toBe(82.5)
    expect(s?.reason).toBe('по твоим словам 90 × 8 (1ПМ ≈ 114 кг), 71 % для 12 повторов')
  })

  it('reps 1: the weight itself is the max', () => {
    // 140 / 1.4 = 100
    const s = suggestWeight([], ex('присед со штангой', 'heavy'), OWNER)
    expect(s?.weight).toBe(100)
    expect(s?.reason).toBe('от твоего максимума 140 кг, 71 % для 12 повторов')
  })

  it('reps unknown: the stated weight is the max, same share as the record branch', () => {
    // 100 / 1.4 = 71.4 -> 72.5
    const s = suggestWeight([], ex('румынская тяга', 'heavy'), OWNER)
    expect(s?.weight).toBe(72.5)
    expect(s?.reason).toBe('по твоим словам ~100 кг (как максимум), 71 % для 12 повторов')
  })

  it('reps unknown follows the intensity like a record', () => {
    // medium 64.3 -> 65, light 57.1 -> 57.5, no intensity = medium
    expect(suggestWeight([], ex('румынская тяга', 'medium'), OWNER)?.weight).toBe(65)
    expect(suggestWeight([], ex('румынская тяга', 'light'), OWNER)?.weight).toBe(57.5)
    expect(suggestWeight([], ex('румынская тяга', null), OWNER)?.weight).toBe(65)
    // 8 reps heavy: 100 / 1.267 = 78.9 -> 80
    expect(suggestWeight([], ex('румынская тяга', 'heavy', { reps_max: 8 }), OWNER)?.weight).toBe(80)
  })

  it('reps unknown with dumbbells rounds to 1 kg', () => {
    // 31 × 0.643 = 19.9 -> 20
    expect(suggestWeight([], ex('жим гантелей сидя', 'medium'), [bl('жим гантелей сидя', 31, null)])?.weight).toBe(20)
  })

  it('dropset: the first drop is the target reps', () => {
    // 90 × 8 -> 114; heavy for 12 reps: 81.4 -> 81 on 1 kg steps
    const s = suggestWeight([], ex('сгибания с гантелями', 'heavy', DROPSET), [bl('сгибания с гантелями', 90, 8)])
    expect(s?.weight).toBe(81)
  })

  it('matches names ignoring case and whitespace', () => {
    expect(suggestWeight([], ex('жим лёжа', 'heavy'), [bl('Жим  Лёжа', 90, 8)])?.weight).toBe(82.5)
  })

  it('does not take a baseline of a similar exercise', () => {
    expect(suggestWeight([], ex('жим лёжа 30°', 'heavy'), OWNER)).toMatchObject({ weight: null, source: 'none' })
  })

  it('is ignored entirely once the exercise has history', () => {
    const history = [workout('2026-10-01T10:00:00Z', 'жим лёжа', [[60, 10], [60, 9], [60, 8]])]
    const s = suggestWeight(history, ex('жим лёжа', 'heavy'), OWNER)
    // Record 60 × 10 -> 80 -> 57.1 -> 57.5 vs "как в прошлый раз 60": the baseline's 82.5 never shows up.
    expect(s?.weight).toBe(60)
    expect(s?.reason).toBe('как в прошлый раз 60 кг')
  })

  it('history of another exercise does not hide the baseline', () => {
    const history = [workout('2026-10-01T10:00:00Z', 'присед со штангой', [[100, 5]])]
    expect(suggestWeight(history, ex('жим лёжа', 'heavy'), OWNER)?.weight).toBe(82.5)
  })

  describe('owner sanity numbers (8–12 reps, program intensities)', () => {
    it.each([
      ['жим лёжа', 'heavy', 82.5],
      ['жим лёжа', 'medium', 72.5],
      ['тяга вертикального блока', 'heavy', 75],
      ['тяга вертикального блока', 'medium', 67.5],
      ['присед со штангой', 'heavy', 100],
      ['присед со штангой', 'medium', 90],
      ['румынская тяга', 'heavy', 72.5],
      ['румынская тяга', 'medium', 65],
    ] as const)('%s, %s -> %d kg', (name, intensity, kg) => {
      expect(suggestWeight([], ex(name, intensity), OWNER)?.weight).toBe(kg)
    })
  })
})
