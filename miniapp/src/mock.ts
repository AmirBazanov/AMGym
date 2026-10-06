// Demo history so the screens and charts are not empty before real data exists.
import type { Program } from './program'
import type { Workout } from './store'

// Rough starting working weights, kg (dumbbell exercises: weight of one dumbbell).
const BASE: Record<string, number> = {
  'сгибания с гантелями на бицепс с супинацией': 14,
  'сгибания с гантелями на бицепс с пронацией': 12,
  'сгибания на бицепс с ez грифом хватом снизу': 30,
  'сгибания на бицепс с ez грифом хватом сверху': 22.5,
  'французский жим лёжа': 27.5,
  'французский жим в блоке из-за головы': 25,
  'жим гантелей сидя': 20,
  'жим сидя в смите': 40,
  'отведения на дельты': 10,
  'отведения гантелей на переднюю дельту': 8,
  'отведения пек дек на заднюю дельту': 35,
  'жим лёжа': 70,
  'жим лёжа 30°': 60,
  'тяга вертикального блока': 60,
  'тяга горизонтального блока': 55,
  'присед со штангой': 80,
  'присед в гаке лицом к спинке': 90,
  'румынская тяга': 80,
}

function toISODate(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** Monday three weeks ago, so "today" lands in week 4 of the program. */
export function demoStartDate(): string {
  const d = new Date()
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7) - 21)
  return toISODate(d)
}

// Deterministic pseudo-random so the demo looks the same on every reset.
function rng(seed: number) {
  return () => {
    seed = (seed * 1664525 + 1013904223) % 4294967296
    return seed / 4294967296
  }
}

export function buildDemoHistory(program: Program, startISO: string): Workout[] {
  const start = new Date(startISO + 'T00:00:00')
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const rand = rng(42)
  const out: Workout[] = []
  for (const week of program.weeks) {
    for (const day of week.days) {
      const date = new Date(start)
      date.setDate(start.getDate() + (week.number - 1) * 7 + day.weekday - 1)
      if (date >= today) return out
      date.setHours(18, 30, 0, 0)
      const progress = 1 + (week.number - 1) * 0.06
      out.push({
        id: `demo-${week.number}-${day.weekday}`,
        programId: program.id,
        week: week.number,
        weekday: day.weekday,
        startedAt: date.toISOString(),
        finishedAt: new Date(date.getTime() + 75 * 60_000).toISOString(),
        exercises: day.exercises.map((ex) => {
          const p = ex.prescription
          const raw = (BASE[ex.name] ?? 20) * progress
          const step = raw < 30 ? 0.5 : 2.5
          const weight = Math.round(raw / step) * step
          return {
            name: ex.name,
            target: p.raw,
            dropset: Boolean(p.drop_reps?.length),
            sets: Array.from({ length: p.sets }, (_, i) => {
              const top = p.drop_reps?.[0] ?? p.reps_max ?? 10
              const low = p.drop_reps?.[0] ?? p.reps_min ?? 8
              const reps = Math.max(low, Math.round(top - i * 0.6 - rand() * 2))
              return { weight, reps, done: true }
            }),
          }
        }),
      })
    }
  }
  return out
}
