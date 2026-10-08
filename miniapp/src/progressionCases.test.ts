// The shared weight-suggestion contract: data/progression_cases.json runs here against the Mini App
// (progression.ts + plan.ts) and in pytest against bot/src/gymbot/services/next_weights.py. A failure means
// the bot and the Mini App would show different numbers for the same day.
import { describe, expect, it } from 'vitest'
import raw from '../../data/progression_cases.json'
import { scaleWeight, suggestFor } from './plan'
import type { Intensity, Prescription, ProgramExercise } from './program'
import {
  equipment,
  grip,
  modifiers,
  movement,
  perHand,
  related,
  roundToStep,
  unilateral,
  type Source,
} from './progression'
import type { Baseline, WeightOverride, Workout } from './store'

interface CaseSet {
  weight: number | null
  reps: number | null
  done?: boolean
}

interface SuggestCase {
  id: string
  kind: 'suggest'
  today: string
  exercise: { name: string; intensity: Intensity | null; order: number; prescription: Prescription }
  history: { startedAt: string; exercises: { name: string; sets: CaseSet[] }[] }[]
  baselines: Baseline[]
  overrides: WeightOverride[]
  factor: number
  expected: {
    weight: number | null
    source: Source
    baseWeight?: number | null
    perHand?: boolean
    hintKg?: number | null
    reasonContains?: string[] // pytest only: Mini App reasons may be worded differently
  }
}

interface RoundCase {
  id: string
  kind: 'roundToStep'
  input: { weight: number; step: number }
  expected: { weight: number }
}

interface ScaleCase {
  id: string
  kind: 'scaleWeight'
  input: { base: number; factor: number; step: number }
  expected: { weight: number }
}

type Case = SuggestCase | RoundCase | ScaleCase

const CASES = (raw as unknown as { cases: Case[] }).cases

/** The JSON history in the store's Workout shape (only startedAt, names and sets matter here). */
function toHistory(h: SuggestCase['history']): Workout[] {
  return h.map((w, i) => ({
    id: `case-${i}`,
    programId: 'case',
    week: 1,
    weekday: 1,
    startedAt: w.startedAt,
    finishedAt: w.startedAt,
    exercises: w.exercises.map((e) => ({
      name: e.name,
      target: '',
      dropset: false,
      sets: e.sets.map((s) => ({ weight: s.weight ?? null, reps: s.reps ?? null, done: s.done ?? true })),
    })),
  }))
}

function toExercise(e: SuggestCase['exercise']): ProgramExercise {
  return { name: e.name, intensity: e.intensity ?? null, order: e.order, prescription: e.prescription }
}

describe('shared progression vectors (data/progression_cases.json)', () => {
  it('has every vector, every kind and every source', () => {
    expect(CASES).toHaveLength(50)
    expect(new Set(CASES.map((c) => c.kind))).toEqual(new Set(['suggest', 'roundToStep', 'scaleWeight']))
    const sources = new Set(CASES.flatMap((c) => (c.kind === 'suggest' ? [c.expected.source] : [])))
    expect(sources).toEqual(new Set(['override', 'history', 'baseline', 'related', 'hint', 'none']))
  })

  it.each(CASES.map((c) => [c.id, c] as const))('%s', (_id, c) => {
    switch (c.kind) {
      case 'roundToStep':
        expect(roundToStep(c.input.weight, c.input.step)).toBe(c.expected.weight)
        return
      case 'scaleWeight':
        expect(scaleWeight(c.input.base, c.input.factor, c.input.step)).toBe(c.expected.weight)
        return
      case 'suggest': {
        const r = suggestFor(
          toHistory(c.history),
          toExercise(c.exercise),
          c.baselines,
          c.overrides,
          c.today,
          c.factor,
        )
        const exp = c.expected
        expect({ weight: r.weight, source: r.suggestion.source }, r.suggestion.reason).toEqual({
          weight: exp.weight,
          source: exp.source,
        })
        if ('baseWeight' in exp) expect(r.baseWeight).toBe(exp.baseWeight)
        if ('perHand' in exp) expect(r.suggestion.perHand).toBe(exp.perHand)
        if ('hintKg' in exp) expect(r.suggestion.hintKg).toBe(exp.hintKg)
        return
      }
      default:
        throw new Error(`unknown case kind: ${(c as { kind: string }).kind}`)
    }
  })
})

// The same table as bot/tests/test_next_weights.py test_name_classifiers: the regexes are ported by hand
// (JS \w is ASCII-only), so they are checked on names beyond the vectors too.
describe('name classifiers (as next_weights.py)', () => {
  const TABLE: [string, string | null, string | null, string | null][] = [
    ['сгибания с гантелями на бицепс с супинацией', 'dumbbell', 'curl', 'supinated'],
    ['сгибания с гантелями на бицепс с пронацией', 'dumbbell', 'curl', 'pronated'],
    ['сгибания на бицепс с ez грифом хватом снизу', 'ez', 'curl', 'supinated'],
    ['сгибания на бицепс с EZ грифом хватом сверху', 'ez', 'curl', 'pronated'],
    ['молотки с гантелями', 'dumbbell', 'curl', 'neutral'],
    ['подъём штанги на бицепс', 'barbell', 'curl', 'supinated'],
    ['французский жим в блоке из-за головы', 'cable', 'triceps_extension', null],
    ['французский жим лёжа', null, 'triceps_extension', null],
    ['жим гантелей сидя', 'dumbbell', 'overhead_press', null],
    ['жим сидя в смите', 'smith', 'overhead_press', null],
    ['жим лёжа', null, 'bench', null],
    ['жим штанги лёжа', 'barbell', 'bench', null],
    ['отведения на дельты', null, 'lateral_raise', null],
    ['отведения пек дек на заднюю дельту', 'machine', 'rear_delt', null],
    ['отведения пек-дек на заднюю дельту', 'machine', 'rear_delt', null],
    ['тяга вертикального блока', 'cable', 'pulldown', null],
    ['тяга горизонтального блока', 'cable', 'row', null],
    ['подтягивания', 'bodyweight', 'pulldown', null],
    ['присед со штангой', 'barbell', 'squat', null],
    ['румынская тяга', null, 'hinge', null],
    ['сгибания ног в тренажёре', 'machine', null, null],
    ['разгибания ног', null, null, null],
    ['жим ногами', null, null, null],
  ]

  it.each(TABLE)('%s', (name, eq, mv, g) => {
    expect([equipment(name), movement(name), grip(name)]).toEqual([eq, mv, g])
  })

  // test_variants_and_unilateral in the pytest file.
  const VARIANTS: [string, string, string[], boolean, boolean][] = [
    ['жим штанги лёжа узким хватом на трицепс', 'bench', ['close', 'lying'], false, false],
    ['французский жим с ez', 'triceps_extension', [], false, false],
    ['румынская тяга со штангой', 'hinge', ['rdl'], false, false],
    ['становая тяга со штангой', 'hinge', ['deadlift'], false, false],
    ['фронтальный присед со штангой', 'squat', ['front'], false, false],
    ['болгарские приседания с гантелями', 'squat', [], true, true],
    ['гоблет-присед с гантелью', 'squat', [], true, false],
    ['концентрированные сгибания с гантелью', 'curl', [], true, false],
    ['сгибания с гантелями сидя', 'curl', [], false, true], // curls ignore seated / standing
    ['жим гантелей сидя', 'overhead_press', ['seated'], false, true],
    ['жим гантелей на наклонной скамье', 'bench', ['incline'], false, true],
  ]

  it.each(VARIANTS)('variant of %s', (name, mv, mods, oneSide, hand) => {
    expect(movement(name)).toBe(mv)
    expect([...modifiers(name)].sort()).toEqual([...mods].sort())
    expect([unilateral(name), perHand(name)]).toEqual([oneSide, hand])
  })

  it('prefers the same grip, then the same equipment', () => {
    const h = toHistory([
      {
        startedAt: '2026-10-05T15:00:00Z',
        exercises: [
          { name: 'сгибания с гантелями на бицепс с супинацией', sets: [{ weight: 20, reps: 8 }] },
          { name: 'сгибания на бицепс с ez грифом хватом снизу', sets: [{ weight: 30, reps: 10 }] },
        ],
      },
    ])
    const target: ProgramExercise = {
      name: 'сгибания на бицепс с ez грифом хватом сверху',
      intensity: 'medium',
      order: 1,
      prescription: { sets: 3, reps_min: 8, reps_max: 12, drop_reps: null, raw: '3х8-12' },
    }
    // Both differ in grip: the same equipment (EZ) wins over the dumbbells.
    expect(related(h, target).related).toBe('сгибания на бицепс с ez грифом хватом снизу')
  })
})
