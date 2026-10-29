// Pure logic of the «Дела» section: parsing /api/life answers, schedule dates, labels, sorting, the edit
// forms and a safe Markdown parser. No browser globals here (tested in node); api.ts does the requests.
// Contract: docs/superpowers/plans/2026-10-29-life-assistant.md, task 1.3 (camelCase JSON).
// Dates are YYYY-MM-DD strings in the server TIMEZONE. «Today» always comes from the server (view.today),
// never from the browser clock; date math works on UTC midnights, so the viewer's time zone does not matter.
import { plural } from './program'

// ---------- types of GET /api/life/view ----------

export type SlotArea = 'work' | 'study'
export type EventArea = 'study' | 'work' | 'other'
export type ProjectKind = 'work' | 'learning' | 'own'
export type ProjectStatus = 'active' | 'paused' | 'idea' | 'done'
export type AgendaKind = 'slot' | 'event' | 'deadline' | 'chore'

export const SLOT_AREAS: readonly SlotArea[] = ['work', 'study']
export const EVENT_AREAS: readonly EventArea[] = ['study', 'work', 'other']
export const PROJECT_KIND_KEYS: readonly ProjectKind[] = ['work', 'learning', 'own']
export const PROJECT_STATUSES: readonly ProjectStatus[] = ['active', 'paused', 'idea', 'done']
const AGENDA_KINDS: readonly AgendaKind[] = ['slot', 'event', 'deadline', 'chore']

export interface LifeSettings {
  remindBefore: number // minutes before a timed plan
  summaryTime: string // HH:MM of the morning summary
  summaryEnabled: boolean
}

/** A weekly schedule slot (lesson, class). weekday 1 = Monday .. 7 = Sunday. */
export interface LifeSlot {
  id: number
  weekday: number
  start: string | null
  end: string | null
  title: string
  area: SlotArea
  tag: string | null
  place: string | null
  vld: string | null // HH:MM: fill in the journal by then after the lesson
  note: string | null
}

/** A plan on a date. remindBefore null: the user's default (settings.remindBefore). */
export interface LifeEvent {
  id: number
  day: string
  start: string | null
  end: string | null
  title: string
  area: EventArea
  projectId: number | null
  remindBefore: number | null
  done: boolean
  note: string | null
}

export interface LifeDeadline {
  id: number
  day: string
  title: string
  course: string | null
  projectId: number | null
  done: boolean
}

export interface LifeTask {
  id: number
  title: string
  tag: string | null
  projectId: number | null
  done: boolean
  doneAt: string | null // ISO, UTC
}

/** A household chore every `everyDays` days; `left` days until due (0 today or never done, < 0 overdue). */
export interface LifeChore {
  id: number
  title: string
  everyDays: number
  lastDone: string | null
  left: number
}

export interface LifeProject {
  id: number
  name: string
  kind: ProjectKind
  status: ProjectStatus
  description: string | null
  link: string | null
  nextStep: string | null
}

/** A note in the view: no body (GET /api/life/notes/{id} has it). */
export interface LifeNoteHead {
  id: number
  title: string
  tags: string[]
  author: 'me' | 'bot'
  projectId: number | null
  updatedAt: string | null // ISO, UTC
}

export interface LifeNote extends LifeNoteHead {
  body: string // Markdown
}

/** One point of a day's agenda, built by the server (services/life.day_agenda). */
export interface AgendaItem {
  kind: AgendaKind
  id: number
  title: string
  start: string | null
  end: string | null
  area: string | null
  place: string | null
  done: boolean
  overdueDays: number // chores: days past due (0 = due today); others 0
}

export interface LifeView {
  today: string
  settings: LifeSettings | null
  slots: LifeSlot[]
  events: LifeEvent[]
  deadlines: LifeDeadline[]
  tasks: LifeTask[]
  chores: LifeChore[]
  projects: LifeProject[]
  notes: LifeNoteHead[]
  agenda: AgendaItem[]
  /** Optional `user: {name}` (not in the task 1.3 contract yet): whose data, shown for a /web login. */
  userName: string | null
}

/** Path segment of each kind in /api/life/{kind}/{id}, and the item it answers with. */
export interface LifeItems {
  slots: LifeSlot
  events: LifeEvent
  deadlines: LifeDeadline
  tasks: LifeTask
  chores: LifeChore
  projects: LifeProject
  notes: LifeNote
}
export type LifeKind = keyof LifeItems
/** Kinds with POST /api/life/{kind}/{id}/done and /undone. */
export type DoneKind = 'events' | 'deadlines' | 'tasks' | 'chores'

/** Agenda kinds are singular, API paths plural. */
export const AGENDA_PATH: Record<AgendaKind, DoneKind> = {
  slot: 'events', // never marked done: slots repeat every week (see agendaDoneKind)
  event: 'events',
  deadline: 'deadlines',
  chore: 'chores',
}

/** The /done path of an agenda point; null for slots (weekly routine, nothing to mark). */
export function agendaDoneKind(kind: AgendaKind): DoneKind | null {
  return kind === 'slot' ? null : AGENDA_PATH[kind]
}

// ---------- parsing (defensive: malformed items are dropped, unknown enums fall back) ----------

type Obj = Record<string, unknown>
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
export const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/

const isObj = (v: unknown): v is Obj => v != null && typeof v === 'object' && !Array.isArray(v)
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null)
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) ? v : null)
const posId = (v: unknown): number | null => {
  const n = int(v)
  return n != null && n > 0 ? n : null
}
const dateStr = (v: unknown): string | null => (typeof v === 'string' && DATE_RE.test(v) ? v : null)
/** "HH:MM", also from "HH:MM:SS". */
const timeStr = (v: unknown): string | null => (typeof v === 'string' && TIME_RE.test(v.slice(0, 5)) ? v.slice(0, 5) : null)
function oneOf<T extends string>(v: unknown, list: readonly T[], fallback: T): T {
  return typeof v === 'string' && (list as readonly string[]).includes(v) ? (v as T) : fallback
}
function list<T>(raw: unknown, parse: (r: unknown) => T | null): T[] {
  if (!Array.isArray(raw)) return []
  const out: T[] = []
  for (const r of raw) {
    const item = parse(r)
    if (item) out.push(item)
  }
  return out
}

export function parseSlot(raw: unknown): LifeSlot | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  const title = str(raw.title)
  const weekday = int(raw.weekday)
  if (id == null || !title || weekday == null || weekday < 1 || weekday > 7) return null
  return {
    id,
    weekday,
    start: timeStr(raw.start),
    end: timeStr(raw.end),
    title,
    area: oneOf(raw.area, SLOT_AREAS, 'work'),
    tag: str(raw.tag),
    place: str(raw.place),
    vld: timeStr(raw.vld),
    note: str(raw.note),
  }
}

export function parseEvent(raw: unknown): LifeEvent | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  const title = str(raw.title)
  const day = dateStr(raw.day)
  if (id == null || !title || !day) return null
  const before = int(raw.remindBefore)
  return {
    id,
    day,
    start: timeStr(raw.start),
    end: timeStr(raw.end),
    title,
    area: oneOf(raw.area, EVENT_AREAS, 'other'),
    projectId: posId(raw.projectId),
    remindBefore: before != null && before >= 0 ? before : null,
    done: raw.done === true,
    note: str(raw.note),
  }
}

export function parseDeadline(raw: unknown): LifeDeadline | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  const title = str(raw.title)
  const day = dateStr(raw.day)
  if (id == null || !title || !day) return null
  return { id, day, title, course: str(raw.course), projectId: posId(raw.projectId), done: raw.done === true }
}

export function parseTask(raw: unknown): LifeTask | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  const title = str(raw.title)
  if (id == null || !title) return null
  return { id, title, tag: str(raw.tag), projectId: posId(raw.projectId), done: raw.done === true, doneAt: str(raw.doneAt) }
}

export function parseChore(raw: unknown): LifeChore | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  const title = str(raw.title)
  const every = int(raw.everyDays)
  if (id == null || !title || every == null || every < 1) return null
  return { id, title, everyDays: every, lastDone: dateStr(raw.lastDone), left: int(raw.left) ?? 0 }
}

export function parseProject(raw: unknown): LifeProject | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  const name = str(raw.name)
  if (id == null || !name) return null
  return {
    id,
    name,
    kind: oneOf(raw.kind, PROJECT_KIND_KEYS, 'own'),
    status: oneOf(raw.status, PROJECT_STATUSES, 'active'),
    description: str(raw.description),
    link: str(raw.link),
    nextStep: str(raw.nextStep),
  }
}

export function parseNoteHead(raw: unknown): LifeNoteHead | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  if (id == null) return null
  const tags = Array.isArray(raw.tags) ? raw.tags.filter((t): t is string => typeof t === 'string' && t.trim() !== '') : []
  return {
    id,
    title: str(raw.title) ?? 'Без названия',
    tags,
    author: raw.author === 'bot' ? 'bot' : 'me',
    projectId: posId(raw.projectId),
    updatedAt: str(raw.updatedAt),
  }
}

export function parseNote(raw: unknown): LifeNote | null {
  const head = parseNoteHead(raw)
  if (!head) return null
  const body = (raw as Obj).body
  return { ...head, body: typeof body === 'string' ? body : '' }
}

export function parseAgendaItem(raw: unknown): AgendaItem | null {
  if (!isObj(raw)) return null
  const id = posId(raw.id)
  const title = str(raw.title)
  if (id == null || !title || typeof raw.kind !== 'string' || !(AGENDA_KINDS as readonly string[]).includes(raw.kind)) return null
  return {
    kind: raw.kind as AgendaKind,
    id,
    title,
    start: timeStr(raw.start),
    end: timeStr(raw.end),
    area: str(raw.area),
    place: str(raw.place),
    done: raw.done === true,
    overdueDays: Math.max(0, int(raw.overdueDays) ?? 0),
  }
}

export function parseSettings(raw: unknown): LifeSettings | null {
  if (!isObj(raw)) return null
  const before = int(raw.remindBefore)
  return {
    remindBefore: before != null && before >= 0 ? before : 30,
    summaryTime: timeStr(raw.summaryTime) ?? '08:00',
    summaryEnabled: raw.summaryEnabled !== false,
  }
}

/** GET /api/life/view; null when the answer has no valid `today` (not this API). Missing lists read as []. */
export function parseView(raw: unknown): LifeView | null {
  if (!isObj(raw)) return null
  const today = dateStr(raw.today)
  if (!today) return null
  return {
    today,
    settings: parseSettings(raw.settings),
    slots: list(raw.slots, parseSlot),
    events: list(raw.events, parseEvent),
    deadlines: list(raw.deadlines, parseDeadline),
    tasks: list(raw.tasks, parseTask),
    chores: list(raw.chores, parseChore),
    projects: list(raw.projects, parseProject),
    notes: list(raw.notes, parseNoteHead),
    agenda: sortAgenda(list(raw.agenda, parseAgendaItem)),
    userName: isObj(raw.user) ? str(raw.user.name) : null,
  }
}

/** Parser of the item each kind's POST/PATCH/done answers with. */
export const LIFE_PARSERS: { [K in LifeKind]: (raw: unknown) => LifeItems[K] | null } = {
  slots: parseSlot,
  events: parseEvent,
  deadlines: parseDeadline,
  tasks: parseTask,
  chores: parseChore,
  projects: parseProject,
  notes: parseNote,
}

/** Russian text of an API error: FastAPI's `{"detail": "..."}` when it is a string, else null. */
export function errorDetail(message: string): string | null {
  const json = message.slice(message.indexOf(' ') + 1)
  try {
    const detail = (JSON.parse(json) as { detail?: unknown } | null)?.detail
    if (typeof detail === 'string' && detail.trim()) return detail
    // Pydantic validation errors: a list of {msg}.
    if (Array.isArray(detail)) {
      const msg = (detail[0] as { msg?: unknown } | undefined)?.msg
      if (typeof msg === 'string' && msg.trim()) return msg
    }
  } catch {
    // Not JSON.
  }
  return null
}

// ---------- dates ----------

const DAY_MS = 86_400_000
const pad = (n: number) => String(n).padStart(2, '0')

function toUTC(d: string): number {
  return Date.UTC(Number(d.slice(0, 4)), Number(d.slice(5, 7)) - 1, Number(d.slice(8, 10)))
}
function fromUTC(ms: number): string {
  const d = new Date(ms)
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
}

export function addDays(d: string, n: number): string {
  return fromUTC(toUTC(d) + n * DAY_MS)
}

/** Days from `from` to `to` (negative when `to` is earlier). */
export function daysBetween(from: string, to: string): number {
  return Math.round((toUTC(to) - toUTC(from)) / DAY_MS)
}

/** 1 = Monday .. 7 = Sunday, as LifeSlot.weekday. */
export function weekdayOf(d: string): number {
  return ((new Date(toUTC(d)).getUTCDay() + 6) % 7) + 1
}

/** The 7 dates (Monday first) of the week `offset` weeks from the one with `today`. */
export function weekDates(today: string, offset = 0): string[] {
  const monday = addDays(today, 1 - weekdayOf(today) + offset * 7)
  return Array.from({ length: 7 }, (_, i) => addDays(monday, i))
}

export const WEEKDAYS = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
export const WEEKDAYS_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']

/** "3 ноября". */
export function dayMonth(d: string): string {
  return `${Number(d.slice(8, 10))} ${MONTHS[Number(d.slice(5, 7)) - 1] ?? ''}`
}

/** "Четверг, 29 октября". */
export function longDate(d: string): string {
  const w = WEEKDAYS[weekdayOf(d) - 1] ?? ''
  return `${w.charAt(0).toUpperCase()}${w.slice(1)}, ${dayMonth(d)}`
}

/** Heading of a day in the schedule: "Сегодня · 29 октября", "Завтра · …", "Пт · 31 октября". */
export function dayHeading(d: string, today: string): string {
  const diff = daysBetween(today, d)
  const name = diff === 0 ? 'Сегодня' : diff === 1 ? 'Завтра' : diff === -1 ? 'Вчера' : WEEKDAYS_SHORT[weekdayOf(d) - 1]
  return `${name} · ${dayMonth(d)}`
}

/** "Эта неделя", "Следующая неделя", "Прошлая неделя", else "3 – 9 ноября" / "27 октября – 2 ноября". */
export function weekLabel(today: string, offset: number): string {
  if (offset === 0) return 'Эта неделя'
  if (offset === 1) return 'Следующая неделя'
  if (offset === -1) return 'Прошлая неделя'
  const dates = weekDates(today, offset)
  const first = dates[0]!
  const last = dates[6]!
  return first.slice(5, 7) === last.slice(5, 7)
    ? `${Number(first.slice(8, 10))} – ${dayMonth(last)}`
    : `${dayMonth(first)} – ${dayMonth(last)}`
}

const DAYS: [string, string, string] = ['день', 'дня', 'дней']

/** Due label from days until it: "сегодня", "завтра", "через 3 дня", "просрочено на 2 дня". */
export function dueLabel(days: number): string {
  if (days === 0) return 'сегодня'
  if (days === 1) return 'завтра'
  if (days > 1) return `через ${plural(days, DAYS)}`
  return `просрочено на ${plural(-days, DAYS)}`
}

/** "каждый день", "раз в неделю", "раз в 3 дня". */
export function everyLabel(n: number): string {
  if (n === 1) return 'каждый день'
  if (n === 7) return 'раз в неделю'
  return `раз в ${plural(n, DAYS)}`
}

/** "10:00–11:30", "10:00" or "". */
export function timeRange(start: string | null, end: string | null): string {
  if (!start) return ''
  return end ? `${start}–${end}` : start
}

// ---------- labels ----------

export const SLOT_AREA_LABEL: Record<SlotArea, string> = { work: 'Работа', study: 'Учёба' }
export const EVENT_AREA_LABEL: Record<EventArea, string> = { study: 'Учёба', work: 'Работа', other: 'Личное' }
export const PROJECT_KIND_LABEL: Record<ProjectKind, string> = { work: 'Работа', learning: 'Обучение', own: 'Своё' }
export const PROJECT_STATUS_LABEL: Record<ProjectStatus, string> = {
  active: 'в работе',
  paused: 'на паузе',
  idea: 'идея',
  done: 'готово',
}

function areaLabel(area: string | null): string | null {
  if (area === 'work') return 'Работа'
  if (area === 'study') return 'Учёба'
  if (area === 'other') return 'Личное'
  return null
}

/** Second line of an agenda point: what it is and where. */
export function agendaLabel(item: AgendaItem): string {
  switch (item.kind) {
    case 'slot':
      return [areaLabel(item.area) ?? 'Расписание', item.place].filter(Boolean).join(' · ')
    case 'event':
      return [areaLabel(item.area) ?? 'План', item.place].filter(Boolean).join(' · ')
    case 'deadline':
      return 'Дедлайн'
    case 'chore':
      return `Дом · ${dueLabel(-item.overdueDays)}`
  }
}

// ---------- sorting and selections ----------

/** Timed first by start, then untimed; otherwise the given order (sort is stable). */
export function byStart<T extends { start: string | null }>(a: T, b: T): number {
  if (a.start && b.start) return a.start < b.start ? -1 : a.start > b.start ? 1 : 0
  return a.start ? -1 : b.start ? 1 : 0
}

export function sortAgenda(items: readonly AgendaItem[]): AgendaItem[] {
  return [...items].sort(byStart)
}

export function inAgenda(agenda: readonly AgendaItem[], kind: AgendaKind, id: number): boolean {
  return agenda.some((a) => a.kind === kind && a.id === id)
}

/** Open deadlines due within `within` days (overdue ones too), soonest first, with days until each. */
export function soonDeadlines(
  deadlines: readonly LifeDeadline[],
  today: string,
  within = 3,
): (LifeDeadline & { days: number })[] {
  return deadlines
    .filter((d) => !d.done)
    .map((d) => ({ ...d, days: daysBetween(today, d.day) }))
    .filter((d) => d.days <= within)
    .sort((a, b) => a.days - b.days || a.title.localeCompare(b.title, 'ru'))
}

/** Open deadlines from `today` on (and overdue), soonest first. */
export function openDeadlines(deadlines: readonly LifeDeadline[], today: string): (LifeDeadline & { days: number })[] {
  return soonDeadlines(deadlines, today, Infinity)
}

/** Chores in order of urgency: most overdue first, then by title. */
export function sortChores(chores: readonly LifeChore[]): LifeChore[] {
  return [...chores].sort((a, b) => a.left - b.left || a.title.localeCompare(b.title, 'ru'))
}

/** Chores due today or overdue, most overdue first. */
export function dueChores(chores: readonly LifeChore[]): LifeChore[] {
  return sortChores(chores.filter((c) => c.left <= 0))
}

/** Open tasks in the server's order. */
export function openTasks(tasks: readonly LifeTask[]): LifeTask[] {
  return tasks.filter((t) => !t.done)
}

/** Done tasks (the server sends the last 7 days), newest first. */
export function doneTasks(tasks: readonly LifeTask[]): LifeTask[] {
  return tasks
    .filter((t) => t.done)
    .sort((a, b) => (b.doneAt ?? '').localeCompare(a.doneAt ?? '') || b.id - a.id)
}

const STATUS_RANK: Record<ProjectStatus, number> = { active: 0, paused: 1, idea: 2, done: 3 }

/** Projects grouped by kind (Работа, Обучение, Своё; empty groups dropped), active first, then by name. */
export function projectGroups(projects: readonly LifeProject[]): { kind: ProjectKind; label: string; projects: LifeProject[] }[] {
  return PROJECT_KIND_KEYS.map((kind) => ({
    kind,
    label: PROJECT_KIND_LABEL[kind],
    projects: projects
      .filter((p) => p.kind === kind)
      .sort((a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status] || a.name.localeCompare(b.name, 'ru')),
  })).filter((g) => g.projects.length > 0)
}

/** Notes, recently changed first. */
export function sortNotes<T extends LifeNoteHead>(notes: readonly T[]): T[] {
  return [...notes].sort((a, b) => (b.updatedAt ?? '').localeCompare(a.updatedAt ?? '') || b.id - a.id)
}

/**
 * The view right after marking an item done or not, before the server answers (the reload that follows
 * brings the truth). A chore done today is due again in everyDays and leaves the agenda.
 */
export function markDone(view: LifeView, kind: DoneKind, id: number, done: boolean): LifeView {
  const agendaKind: AgendaKind = kind === 'events' ? 'event' : kind === 'deadlines' ? 'deadline' : 'chore'
  const agenda =
    kind === 'chores' && done
      ? view.agenda.filter((a) => !(a.kind === 'chore' && a.id === id))
      : view.agenda.map((a) => (a.kind === agendaKind && a.id === id ? { ...a, done } : a))
  switch (kind) {
    case 'tasks':
      return { ...view, tasks: view.tasks.map((t) => (t.id === id ? { ...t, done, doneAt: done ? new Date().toISOString() : null } : t)) }
    case 'events':
      return { ...view, agenda, events: view.events.map((e) => (e.id === id ? { ...e, done } : e)) }
    case 'deadlines':
      return { ...view, agenda, deadlines: view.deadlines.map((d) => (d.id === id ? { ...d, done } : d)) }
    case 'chores':
      if (!done) return view
      return {
        ...view,
        agenda,
        chores: view.chores.map((c) => (c.id === id ? { ...c, lastDone: view.today, left: c.everyDays } : c)),
      }
  }
}

// ---------- the week schedule ----------

export interface ScheduleItem {
  kind: 'slot' | 'event' | 'deadline'
  id: number
  title: string
  start: string | null
  end: string | null
  sub: string
  done: boolean
}

export interface ScheduleDay {
  date: string
  isToday: boolean
  items: ScheduleItem[]
}

/** Each date with its weekly slots, its plans and its deadlines; timed first by start. */
export function scheduleWeek(
  view: Pick<LifeView, 'slots' | 'events' | 'deadlines'>,
  dates: readonly string[],
  today: string,
): ScheduleDay[] {
  return dates.map((date) => {
    const wd = weekdayOf(date)
    const items: ScheduleItem[] = [
      ...view.slots
        .filter((s) => s.weekday === wd)
        .map((s) => ({
          kind: 'slot' as const,
          id: s.id,
          title: s.title,
          start: s.start,
          end: s.end,
          sub: [SLOT_AREA_LABEL[s.area], s.place].filter(Boolean).join(' · '),
          done: false,
        })),
      ...view.events
        .filter((e) => e.day === date)
        .map((e) => ({
          kind: 'event' as const,
          id: e.id,
          title: e.title,
          start: e.start,
          end: e.end,
          sub: ['План', EVENT_AREA_LABEL[e.area]].join(' · '),
          done: e.done,
        })),
      ...view.deadlines
        .filter((d) => d.day === date)
        .map((d) => ({
          kind: 'deadline' as const,
          id: d.id,
          title: d.title,
          start: null,
          end: null,
          sub: ['Дедлайн', d.course].filter(Boolean).join(' · '),
          done: d.done,
        })),
    ]
    return { date, isToday: date === today, items: items.sort(byStart) }
  })
}

// ---------- edit forms (one generic sheet per kind) ----------

export type FieldType = 'text' | 'textarea' | 'time' | 'date' | 'int' | 'weekday' | 'choice' | 'project' | 'tags'

export interface FieldSpec {
  key: string
  label: string
  type: FieldType
  required?: boolean
  options?: { value: string; label: string }[]
  placeholder?: string
  min?: number
  max?: number
}

const opts = <T extends string>(labels: Record<T, string>, keys: readonly T[]) => keys.map((k) => ({ value: k, label: labels[k] }))

export const LIFE_FORMS: Record<LifeKind, FieldSpec[]> = {
  slots: [
    { key: 'title', label: 'Название', type: 'text', required: true, placeholder: 'Например, «ИЗ Математика»' },
    { key: 'area', label: 'Где', type: 'choice', required: true, options: opts(SLOT_AREA_LABEL, SLOT_AREAS) },
    { key: 'weekday', label: 'День недели', type: 'weekday', required: true },
    { key: 'start', label: 'Начало', type: 'time', required: true },
    { key: 'end', label: 'Конец', type: 'time' },
    { key: 'place', label: 'Место', type: 'text', placeholder: 'Кабинет, адрес' },
    { key: 'note', label: 'Заметка', type: 'text' },
  ],
  events: [
    { key: 'title', label: 'Что', type: 'text', required: true, placeholder: 'Например, «Созвон»' },
    { key: 'area', label: 'Раздел', type: 'choice', required: true, options: opts(EVENT_AREA_LABEL, EVENT_AREAS) },
    { key: 'day', label: 'Дата', type: 'date', required: true },
    { key: 'start', label: 'Начало', type: 'time' },
    { key: 'end', label: 'Конец', type: 'time' },
    { key: 'remindBefore', label: 'Напомнить за, мин', type: 'int', min: 0, max: 1440, placeholder: 'как обычно' },
    { key: 'projectId', label: 'Проект', type: 'project' },
    { key: 'note', label: 'Заметка', type: 'text' },
  ],
  deadlines: [
    { key: 'title', label: 'Что сдать', type: 'text', required: true, placeholder: 'Например, «Лабораторная №1»' },
    { key: 'day', label: 'Срок', type: 'date', required: true },
    { key: 'course', label: 'Предмет', type: 'text' },
    { key: 'projectId', label: 'Проект', type: 'project' },
  ],
  tasks: [
    { key: 'title', label: 'Задача', type: 'text', required: true, placeholder: 'Например, «Обновить резюме»' },
    { key: 'tag', label: 'Метка', type: 'text', placeholder: 'работа, учёба…' },
    { key: 'projectId', label: 'Проект', type: 'project' },
  ],
  chores: [
    { key: 'title', label: 'Дело по дому', type: 'text', required: true, placeholder: 'Например, «Уборка»' },
    { key: 'everyDays', label: 'Раз в сколько дней', type: 'int', required: true, min: 1, max: 365 },
    { key: 'lastDone', label: 'Последний раз', type: 'date' },
  ],
  projects: [
    { key: 'name', label: 'Название', type: 'text', required: true },
    { key: 'kind', label: 'Вид', type: 'choice', required: true, options: opts(PROJECT_KIND_LABEL, PROJECT_KIND_KEYS) },
    { key: 'status', label: 'Статус', type: 'choice', required: true, options: opts(PROJECT_STATUS_LABEL, PROJECT_STATUSES) },
    { key: 'nextStep', label: 'Следующий шаг', type: 'text' },
    { key: 'description', label: 'Описание', type: 'textarea' },
    { key: 'link', label: 'Ссылка', type: 'text', placeholder: 'https://…' },
  ],
  notes: [
    { key: 'title', label: 'Заголовок', type: 'text', required: true },
    { key: 'body', label: 'Текст (Markdown)', type: 'textarea' },
    { key: 'tags', label: 'Теги через запятую', type: 'tags' },
    { key: 'projectId', label: 'Проект', type: 'project' },
  ],
}

/** Defaults of a new item of each kind (the form keeps every value as a string). */
const NEW_DEFAULTS: Partial<Record<LifeKind, Record<string, string>>> = {
  slots: { area: 'study' },
  events: { area: 'other' },
  chores: { everyDays: '7' },
  projects: { kind: 'own', status: 'active' },
}

export type FormValues = Record<string, string>

function toFormString(v: unknown): string {
  if (v == null) return ''
  if (Array.isArray(v)) return v.filter((x) => typeof x === 'string').join(', ')
  return String(v)
}

/** Form values of `item` (an edit) or of a new item with `defaults` (for example the day or project). */
export function formValues(kind: LifeKind, item: object | null, defaults: FormValues = {}): FormValues {
  const src = (item ?? {}) as Obj
  const out: FormValues = {}
  for (const f of LIFE_FORMS[kind]) out[f.key] = item ? toFormString(src[f.key]) : (defaults[f.key] ?? NEW_DEFAULTS[kind]?.[f.key] ?? '')
  return out
}

function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => x === b[i])
  // A stored '' and a cleared field are the same "nothing".
  const norm = (v: unknown) => (v === '' || v === undefined ? null : v)
  return norm(a) === norm(b)
}

/**
 * Request body from the form: values typed and checked. A new item gets every filled field; an edit only
 * the changed ones (an empty body: nothing changed). `error` is a Russian text for the sheet.
 */
export function formBody(
  kind: LifeKind,
  values: FormValues,
  original: object | null,
): { body: Record<string, unknown>; error: string | null } {
  const orig = original as Obj | null
  const body: Record<string, unknown> = {}
  for (const f of LIFE_FORMS[kind]) {
    const raw = (values[f.key] ?? '').trim()
    if (!raw && f.required) return { body: {}, error: `Заполни «${f.label}»` }
    let v: unknown = null
    if (raw) {
      switch (f.type) {
        case 'time':
          if (!TIME_RE.test(raw)) return { body: {}, error: `«${f.label}»: время ЧЧ:ММ` }
          v = raw
          break
        case 'date':
          if (!DATE_RE.test(raw)) return { body: {}, error: `«${f.label}»: дата ГГГГ-ММ-ДД` }
          v = raw
          break
        case 'int':
        case 'weekday':
        case 'project': {
          const n = Number(raw)
          const min = f.type === 'weekday' ? 1 : (f.min ?? (f.type === 'project' ? 1 : -Infinity))
          const max = f.type === 'weekday' ? 7 : (f.max ?? Infinity)
          if (!Number.isInteger(n) || n < min || n > max)
            return { body: {}, error: `«${f.label}»: число${Number.isFinite(min) && Number.isFinite(max) ? ` от ${min} до ${max}` : ''}` }
          v = n
          break
        }
        case 'tags':
          v = [...new Set(raw.split(',').map((t) => t.trim()).filter(Boolean))]
          break
        case 'textarea':
          // Markdown keeps its inner line breaks and indentation; only the outer blank lines go.
          v = (values[f.key] ?? '').replace(/^\s*\n|\s+$/g, '')
          break
        default:
          v = raw
      }
    } else if (f.type === 'tags') v = []
    if (orig) {
      if (!sameValue(v, orig[f.key])) body[f.key] = v
    } else if (v != null && !(Array.isArray(v) && v.length === 0)) body[f.key] = v
  }
  const start = ('start' in body ? body.start : orig?.start) as string | null | undefined
  const end = ('end' in body ? body.end : orig?.end) as string | null | undefined
  if (start && end && end <= start && ('start' in body || 'end' in body)) return { body: {}, error: 'Конец раньше начала' }
  return { body, error: null }
}

// ---------- Markdown: a small, safe subset rendered as React elements (never as HTML) ----------

export type MdInline =
  | { t: 'text'; text: string }
  | { t: 'code'; text: string }
  | { t: 'bold'; children: MdInline[] }
  | { t: 'italic'; children: MdInline[] }
  | { t: 'link'; href: string; children: MdInline[] }

export interface MdListItem {
  checked: boolean | null // "- [ ]" / "- [x]"; null for a plain item
  inline: MdInline[]
}

export type MdBlock =
  | { t: 'h'; level: 1 | 2 | 3; inline: MdInline[] }
  | { t: 'p'; lines: MdInline[][] }
  | { t: 'ul' | 'ol'; items: MdListItem[] }
  | { t: 'quote'; lines: MdInline[][] }
  | { t: 'code'; text: string }
  | { t: 'hr' }

/** Only http(s) links are followed; javascript:, data: and the rest are not links. */
export function safeHref(url: string): string | null {
  return /^https?:\/\/[^\s]+$/i.test(url.trim()) ? url.trim() : null
}

// `code` | **bold** | [text](url) | *italic* | a bare http(s) URL
const INLINE_RE = /`([^`\n]+)`|\*\*(.+?)\*\*|\[([^\]\n]+)\]\(([^)\s]+)\)|\*([^*\s](?:[^*\n]*[^*\s])?)\*|(https?:\/\/[^\s<>"'()]+)/g

export function parseInline(s: string): MdInline[] {
  const out: MdInline[] = []
  const text = (t: string) => {
    if (!t) return
    const prev = out[out.length - 1]
    if (prev?.t === 'text') prev.text += t
    else out.push({ t: 'text', text: t })
  }
  let last = 0
  for (const m of s.matchAll(INLINE_RE)) {
    text(s.slice(last, m.index))
    last = m.index + m[0].length
    if (m[1] != null) out.push({ t: 'code', text: m[1] })
    else if (m[2] != null) out.push({ t: 'bold', children: parseInline(m[2]) })
    else if (m[3] != null) {
      const href = safeHref(m[4] ?? '')
      if (href) out.push({ t: 'link', href, children: parseInline(m[3]) })
      else text(m[3]) // an unsafe link keeps only its label, as plain text
    } else if (m[5] != null) out.push({ t: 'italic', children: parseInline(m[5]) })
    else if (m[6] != null) {
      const trail = /[.,;:!?]+$/.exec(m[6])?.[0] ?? ''
      const url = m[6].slice(0, m[6].length - trail.length)
      out.push({ t: 'link', href: url, children: [{ t: 'text', text: url }] })
      text(trail)
    }
  }
  text(s.slice(last))
  return out
}

/** Headings, paragraphs, lists (with checkboxes), quotes, fenced code, rules; everything else is text. */
export function parseMarkdown(src: string): MdBlock[] {
  const lines = src.replace(/\r\n?/g, '\n').split('\n')
  const blocks: MdBlock[] = []
  let para: string[] = []
  // The list or quote that the next matching line continues; any other line closes it.
  let open = null as MdBlock | null

  const flush = () => {
    if (para.length) blocks.push({ t: 'p', lines: para.map(parseInline) })
    para = []
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!
    const fence = /^\s{0,3}(```|~~~)/.exec(line)
    if (fence) {
      flush()
      open = null
      const body: string[] = []
      i++
      while (i < lines.length && !lines[i]!.trimStart().startsWith(fence[1]!)) body.push(lines[i++]!)
      blocks.push({ t: 'code', text: body.join('\n') })
      continue
    }
    if (!line.trim()) {
      flush()
      open = null
      continue
    }
    const h = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line)
    if (h) {
      flush()
      open = null
      blocks.push({ t: 'h', level: Math.min(3, h[1]!.length) as 1 | 2 | 3, inline: parseInline(h[2]!) })
      continue
    }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush()
      open = null
      blocks.push({ t: 'hr' })
      continue
    }
    const li = /^\s*([-*+]|\d{1,3}[.)])\s+(.*)$/.exec(line)
    if (li) {
      flush()
      const t = /\d/.test(li[1]!) ? 'ol' : 'ul'
      const box = /^\[([ xX])\]\s+(.*)$/.exec(li[2]!)
      const item: MdListItem = box
        ? { checked: box[1] !== ' ', inline: parseInline(box[2]!) }
        : { checked: null, inline: parseInline(li[2]!) }
      if (open?.t === t) open.items.push(item)
      else {
        open = { t, items: [item] }
        blocks.push(open)
      }
      continue
    }
    const q = /^\s{0,3}>\s?(.*)$/.exec(line)
    if (q) {
      flush()
      if (open?.t === 'quote') open.lines.push(parseInline(q[1]!))
      else {
        open = { t: 'quote', lines: [parseInline(q[1]!)] }
        blocks.push(open)
      }
      continue
    }
    open = null
    para.push(line.trim())
  }
  flush()
  return blocks
}
