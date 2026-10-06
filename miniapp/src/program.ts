import arms from '../../data/programs/arms_specialization_8w.json'

export type Intensity = 'heavy' | 'medium' | 'light'

export interface Prescription {
  sets: number
  reps_min: number | null
  reps_max: number | null
  drop_reps: number[] | null
  raw: string
}

export interface ProgramExercise {
  name: string
  intensity: Intensity | null
  prescription: Prescription
  order: number
}

export interface ProgramDay {
  weekday: number // 1 = Monday
  title: string
  exercises: ProgramExercise[]
}

export interface ProgramWeek {
  number: number
  days: ProgramDay[]
}

export interface Program {
  id: string
  name: string
  source: string
  weeks: ProgramWeek[]
}

export const PROGRAMS: Program[] = [{ id: 'arms_specialization_8w', ...(arms as Omit<Program, 'id'>) }]

export function getProgram(id: string | null): Program {
  return PROGRAMS.find((p) => p.id === id) ?? PROGRAMS[0]
}

export function getDay(program: Program, week: number, weekday: number): ProgramDay | undefined {
  return program.weeks.find((w) => w.number === week)?.days.find((d) => d.weekday === weekday)
}

export function programExerciseNames(program: Program): string[] {
  const seen = new Set<string>()
  for (const w of program.weeks) for (const d of w.days) for (const e of d.exercises) seen.add(e.name)
  return [...seen]
}

export const WEEKDAY_SHORT = ['', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
export const WEEKDAY_LONG = ['', 'Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

export const INTENSITY_LABEL: Record<Intensity, string> = {
  heavy: 'тяжёлая',
  medium: 'средняя',
  light: 'лёгкая',
}

/** Russian plural: plural(5, ['упражнение', 'упражнения', 'упражнений']). */
export function plural(n: number, forms: [string, string, string]): string {
  const m10 = n % 10
  const m100 = n % 100
  const form = m10 === 1 && m100 !== 11 ? 0 : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 1 : 2
  return `${n} ${forms[form]}`
}

export function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export function isDropset(p: Prescription): boolean {
  return Boolean(p.drop_reps?.length)
}

/** Human-readable prescription: "6 × 8–12" or "3 дропсета × 12-6-6". */
export function formatPrescription(p: Prescription): string {
  if (isDropset(p)) return `${p.sets} × дропсет ${p.drop_reps!.join('-')}`
  if (p.reps_min == null) return p.raw
  const reps = p.reps_max && p.reps_max !== p.reps_min ? `${p.reps_min}–${p.reps_max}` : `${p.reps_min}`
  return `${p.sets} × ${reps}`
}

/** Which program week/day falls on a given date, counting from the program start (a Monday). */
export function programPosition(program: Program, startISO: string, date = new Date()) {
  const start = new Date(startISO + 'T00:00:00')
  const today = new Date(date.getFullYear(), date.getMonth(), date.getDate())
  const days = Math.floor((today.getTime() - start.getTime()) / 86_400_000)
  const weeks = program.weeks.length
  const week = Math.min(Math.max(Math.floor(days / 7) + 1, 1), weeks)
  const weekday = ((today.getDay() + 6) % 7) + 1
  return { week, weekday, finished: days >= weeks * 7, notStarted: days < 0 }
}

/** The training day for today, or the next one in the same week (wrapping to the next week). */
export function nextTrainingDay(program: Program, week: number, weekday: number) {
  const w = program.weeks.find((x) => x.number === week)
  const today = w?.days.find((d) => d.weekday === weekday)
  if (today) return { week, weekday, isToday: true }
  const later = w?.days.find((d) => d.weekday > weekday)
  if (later) return { week, weekday: later.weekday, isToday: false }
  const nw = program.weeks.find((x) => x.number === week + 1) ?? program.weeks[0]
  return { week: nw.number, weekday: nw.days[0].weekday, isToday: false }
}

/** Short muscle-group title for a day, derived from the program structure. */
export function dayFocus(day: ProgramDay, short = false): string {
  if (day.weekday === 3) return 'База'
  return short ? 'Руки' : 'Руки и плечи'
}
