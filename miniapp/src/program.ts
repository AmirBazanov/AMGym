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
  id?: number // ProgramItem.id; only in programs from the server
  name: string
  intensity: Intensity | null
  prescription: Prescription
  order: number
}

export interface ProgramDay {
  id?: number // ProgramDay.id (POST /api/workouts programDayId); only in programs from the server
  weekday: number // 1 = Monday
  title: string
  focus?: string | null // muscle-group title ("Руки и плечи"); absent in an old cache
  exercises: ProgramExercise[]
}

export interface ProgramWeek {
  number: number
  days: ProgramDay[]
}

export interface Program {
  id: string // slug
  name: string
  source: string
  weeks: ProgramWeek[]
  // Only in programs from the server (GET /api/programs/{slug}):
  version?: number // Program.version, bumped on every edit
  editable?: boolean // the user's own copy
  basedOn?: string | null // template slug of a copy
}

/** Bundled templates: the demo and the fallback before the server program is known (offline first start). */
export const PROGRAMS: Program[] = [{ id: 'arms_specialization_8w', ...(arms as Omit<Program, 'id'>) }]

// Programs from the server by slug (store.ts keeps them in localStorage and registers them here on load).
let serverPrograms: Readonly<Record<string, Program>> = {}

/** Replaces the registry of server programs; looked up before the bundled ones. */
export function setServerPrograms(map: Readonly<Record<string, Program>>): void {
  serverPrograms = map
}

/** The program by slug: the server's copy first, then the bundled one; undefined when neither knows it. */
export function findProgram(id: string | null): Program | undefined {
  if (id == null) return undefined
  return (Object.prototype.hasOwnProperty.call(serverPrograms, id) ? serverPrograms[id] : undefined) ?? PROGRAMS.find((p) => p.id === id)
}

/** findProgram, else the first bundled program, so screens always have something to show. */
export function getProgram(id: string | null): Program {
  return findProgram(id) ?? PROGRAMS[0]
}

export function getDay(program: Program, week: number, weekday: number): ProgramDay | undefined {
  return program.weeks.find((w) => w.number === week)?.days.find((d) => d.weekday === weekday)
}

/**
 * The day a workout was built from: its ProgramDay.id when the program has it (a move_day may have put the
 * day on another weekday and another day on the workout's), else the day on its week and weekday.
 */
export function workoutDay(
  program: Program,
  w: { week: number; weekday: number; programDayId?: number | null },
): ProgramDay | undefined {
  if (w.programDayId != null) {
    const own = program.weeks.find((x) => x.number === w.week)?.days.find((d) => d.id === w.programDayId)
    if (own) return own
  }
  return getDay(program, w.week, w.weekday)
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
  const [y, m, d] = startISO.split('-').map(Number)
  // UTC day numbers avoid off-by-one around DST switches.
  const days = (Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) - Date.UTC(y, m - 1, d)) / 86_400_000
  const weeks = program.weeks.length
  const week = Math.min(Math.max(Math.floor(days / 7) + 1, 1), weeks)
  const weekday = ((date.getDay() + 6) % 7) + 1
  return { week, weekday, finished: days >= weeks * 7, notStarted: days < 0 }
}

/** The training day for today, or the next one in the same week (wrapping to the next week). */
export function nextTrainingDay(program: Program, week: number, weekday: number) {
  const w = program.weeks.find((x) => x.number === week)
  const today = w?.days.find((d) => d.weekday === weekday)
  if (today) return { week, weekday, isToday: true }
  const later = w?.days.find((d) => d.weekday > weekday)
  if (later) return { week, weekday: later.weekday, isToday: false }
  const nw = program.weeks.find((x) => x.number === week + 1 && x.days.length)
  if (nw) return { week: nw.number, weekday: nw.days[0].weekday, isToday: false }
  // Past the last training day: stay on the last day of the program.
  const last = w?.days[w.days.length - 1]
  return { week, weekday: last?.weekday ?? weekday, isToday: false }
}

/**
 * Short muscle-group title for a day: the program's `focus` when it has one (short form: its first word,
 * «Руки и плечи» -> «Руки»), else the old rule for an old cache: Wednesday is «База», other days arms.
 */
export function dayFocus(day: ProgramDay, short = false): string {
  const focus = day.focus?.trim()
  if (focus) return short ? focus.split(/\s+/)[0] : focus
  if (day.weekday === 3) return 'База'
  return short ? 'Руки' : 'Руки и плечи'
}
