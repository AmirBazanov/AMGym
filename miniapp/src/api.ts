// All requests carry Telegram initData; the backend validates its HMAC with the bot token
// before trusting the user id. Never trust initDataUnsafe on the server.
const initData: string =
  (window as unknown as { Telegram?: { WebApp?: { initData?: string } } }).Telegram?.WebApp?.initData ?? ''

/** Opened from Telegram (as opposed to a plain browser preview). */
export const inTelegram = initData.length > 0

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

/** Like api(), plus the HTTP status for endpoints where 200 and 201 mean different things. */
export async function apiWithStatus<T>(path: string, init: RequestInit = {}): Promise<{ status: number; data: T }> {
  const res = await fetch(`./api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData, ...init.headers },
  })
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${await res.text()}`)
  return { status: res.status, data: (res.status === 204 ? undefined : await res.json()) as T }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await apiWithStatus<T>(path, init)).data
}

/**
 * A signal that aborts after `ms`, so a hung request fails instead of blocking what waits for it.
 * AbortSignal.timeout is missing in old Telegram WebViews: fall back to AbortController + setTimeout.
 * Pass it as `signal` in api()'s init; `keepalive: true` there survives the page being hidden.
 */
export function timeoutSignal(ms: number): AbortSignal {
  if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms)
  const c = new AbortController()
  setTimeout(() => c.abort(), ms)
  return c.signal
}

// ---- Nutrition (contract: docs/superpowers/specs/2026-10-06-nutrition-reminders-voice-design.md, section 1) ----

export interface Macros {
  kcal: number
  protein: number
  fat: number
  carbs: number
}

/** Daily targets; null means "not set". kcal 0..10000, grams 0..1000, whole numbers. */
export interface Targets {
  kcal: number | null
  protein: number | null
  fat: number | null
  carbs: number | null
}

export type MacroKey = keyof Macros

export const EMPTY_TARGETS: Targets = { kcal: null, protein: null, fat: null, carbs: null }

// ---- Profile (used by the bot's /advice). Limits and helpers live in profile.ts. ----

export type Goal = 'mass' | 'cut' | 'strength' | 'health'

/** null means "not set". PUT /api/settings takes a partial profile: a missing key is kept, null clears it. */
export interface Profile {
  weightKg: number | null
  heightCm: number | null
  birthYear: number | null
  goal: Goal | null
  about: string | null
}

export const EMPTY_PROFILE: Profile = { weightKg: null, heightCm: null, birthYear: null, goal: null, about: null }

export interface FoodEntry extends Macros {
  id: number
  eatenAt: string // ISO, UTC
  time: string // HH:MM in the server TIMEZONE
  description: string
  grams: number | null
  estimated: boolean
}

export interface NutritionDay {
  date: string // YYYY-MM-DD in the server TIMEZONE
  targets: Targets
  totals: Macros
  remaining: Targets // target - total; negative when over, null when no target
  entries: FoodEntry[] // sorted by eatenAt
}

export interface NutritionWeekDay extends Macros {
  date: string
  entries: number
}

export interface NutritionWeek {
  targets: Targets
  days: NutritionWeekDay[] // 7 days ending with `end`, empty days included
}

/** Without a date the server answers for its own "today" (TIMEZONE), the only source of truth for it. */
export function getNutritionDay(date?: string): Promise<NutritionDay> {
  return api<NutritionDay>(`/nutrition/day${date ? `?date=${date}` : ''}`)
}

export function getNutritionWeek(end?: string): Promise<NutritionWeek> {
  return api<NutritionWeek>(`/nutrition/week${end ? `?end=${end}` : ''}`)
}

export function deleteFood(id: number): Promise<void> {
  return api<void>(`/food/${id}`, { method: 'DELETE' })
}

// ---- Reminders (contract: same spec, section 2). Never cached offline: loaded when the block is shown. ----

export type ReminderKind = 'text' | 'nutrition' | 'advice' | 'checkin'

/**
 * Reminder sent by the bot at `time` (HH:MM, server TIMEZONE), every day or only on `weekday`
 * (0 = Monday .. 6 = Sunday). `text` is used only for kind "text".
 */
export interface Reminder {
  id: number
  time: string
  kind: ReminderKind
  text: string | null
  weekday: number | null // may be absent on servers older than weekly reminders: read as `?? null`
  enabled: boolean
}

export interface ReminderInput {
  time: string
  kind: ReminderKind
  text?: string
  weekday?: number | null
  enabled?: boolean
}

export const REMINDER_TEXT_MAX = 200
export const REMINDERS_MAX = 20

export function getReminders(): Promise<Reminder[]> {
  return api<Reminder[]>('/reminders')
}

export function createReminder(body: ReminderInput): Promise<Reminder> {
  return api<Reminder>('/reminders', { method: 'POST', body: JSON.stringify(body) })
}

export function updateReminder(id: number, patch: Partial<ReminderInput>): Promise<Reminder> {
  return api<Reminder>(`/reminders/${id}`, { method: 'PATCH', body: JSON.stringify(patch) })
}

export function deleteReminder(id: number): Promise<void> {
  return api<void>(`/reminders/${id}`, { method: 'DELETE' })
}

// ---- Wellbeing: sleep, energy, mood and pains the bot parsed from chat. Never cached offline. ----

export interface Pain {
  place: string
  severity: number | null // 1..5
}

export interface WellbeingEntry {
  id: number
  notedAt: string // ISO, UTC
  date: string // YYYY-MM-DD in the server TIMEZONE
  sleepHours: number | null
  sleepQuality: number | null // 1..5
  energy: number | null // 1..5
  mood: number | null // 1..5
  pains: Pain[]
  note: string | null
}

/** Entries of the last `days` days, newest first (by notedAt). */
export function getWellbeing(days = 14): Promise<WellbeingEntry[]> {
  return api<WellbeingEntry[]>(`/wellbeing?days=${days}`)
}

/** 404 when the entry is already gone or belongs to someone else. */
export function deleteWellbeing(id: number): Promise<void> {
  return api<void>(`/wellbeing/${id}`, { method: 'DELETE' })
}

// ---- Facts the bot remembers about the user (mixed into parsing and advice). Never cached offline. ----

export type FactCategory = 'food' | 'training' | 'health' | 'schedule' | 'other'

export interface Fact {
  id: number
  text: string
  category: FactCategory
  createdAt: string // ISO, UTC
  active: boolean
}

export interface FactInput {
  text: string
  category?: FactCategory // server default: "other"
}

export interface FactPatch {
  text?: string
  category?: FactCategory
  active?: boolean
}

export const FACT_TEXT_MAX = 200
/** More active facts than this: 409 on create and on switching one back on. */
export const FACTS_ACTIVE_MAX = 50

/** Newest first (by createdAt). */
export function getFacts(): Promise<Fact[]> {
  return api<Fact[]>('/facts')
}

/** 201: a new fact. 200: the same text already exists and the server returns that fact unchanged. */
export async function createFact(body: FactInput): Promise<{ fact: Fact; created: boolean }> {
  const res = await apiWithStatus<Fact>('/facts', { method: 'POST', body: JSON.stringify(body) })
  return { fact: res.data, created: res.status === 201 }
}

/** 422 when the new text duplicates another fact. */
export function updateFact(id: number, patch: FactPatch): Promise<Fact> {
  return api<Fact>(`/facts/${id}`, { method: 'PATCH', body: JSON.stringify(patch) })
}

export function deleteFact(id: number): Promise<void> {
  return api<void>(`/facts/${id}`, { method: 'DELETE' })
}

// ---- Adaptive day plan: the server adjusts today's program day to wellbeing, food and recovery. ----

export type Readiness = 'normal' | 'light' | 'rest'

export interface DayPlanExercise {
  name: string // ProgramExercise.name of the program day
  sets: number | null
  repsMin: number | null
  repsMax: number | null
  weightFactor: number | null // 0.9 = 10 % lighter
  skip: boolean
  replaceWith: string | null
  reason: string | null
}

export interface DayPlan {
  date: string // YYYY-MM-DD in the server TIMEZONE
  adjusted: boolean
  readiness: Readiness
  summary: string | null
  exercises: DayPlanExercise[] // program day order; empty when not adjusted
  week?: number // program week the plan is for; absent on older servers
  weekday?: number // 1 = Monday .. 7 = Sunday, as ProgramDay.weekday; absent on older servers
}

/** 404 when today is not a training day. Never cached offline. */
export function getTodayPlan(): Promise<DayPlan> {
  return api<DayPlan>('/plan/today')
}

export function regenerateTodayPlan(): Promise<DayPlan> {
  return api<DayPlan>('/plan/today/regenerate', { method: 'POST' })
}

// ---- Body weight: one value per day (upsert), from the chat, the Mini App or MCP. Never cached offline. ----

export type BodyWeightSource = 'chat' | 'miniapp' | 'mcp'

export interface BodyWeight {
  date: string // YYYY-MM-DD in the server TIMEZONE
  weightKg: number
  source: BodyWeightSource
}

/** Values of the last `days` days (1..3660), oldest first. Raw JSON: parse with bodyWeight.parseBodyWeights. */
export function getBodyWeights(days: number): Promise<unknown> {
  return api<unknown>(`/body-weight?days=${days}`)
}

/** Without a date the server stores it for its own "today". 422: weight outside 30..250 or a bad date. */
export function saveBodyWeight(weightKg: number, date?: string): Promise<unknown> {
  return api<unknown>('/body-weight', { method: 'POST', body: JSON.stringify(date ? { weightKg, date } : { weightKg }) })
}

/** 404 when there is no value for that date. */
export function deleteBodyWeight(date: string): Promise<void> {
  return api<void>(`/body-weight/${encodeURIComponent(date)}`, { method: 'DELETE' })
}

// ---- Programs: the server is their source of truth (templates and the user's own copies). ----
// Contract: docs/superpowers/specs/2026-10-08-program-editor-design.md, section «API». camelCase except
// `prescription`, which keeps the program JSON keys (program.ts Prescription).

export interface ProgramSummary {
  id: string // slug
  name: string
  source: string | null
  weeks: number
  daysPerWeek: number // training days in the first week
  exercises: number // distinct exercises
  editable: boolean // the user's own copy
  basedOn: string | null // template slug of a copy
  version: number
}

export interface ProgramExerciseOut {
  id: number // ProgramItem.id
  name: string
  intensity: 'heavy' | 'medium' | 'light' | null
  order: number
  prescription: { sets: number; reps_min: number | null; reps_max: number | null; drop_reps: number[] | null; raw: string }
}

export interface ProgramDayOut {
  id: number // ProgramDay.id: POST /api/workouts programDayId
  weekday: number // 1 = Monday .. 7 = Sunday
  title: string // "понедельник"
  focus: string | null
  exercises: ProgramExerciseOut[]
}

export interface ProgramOut {
  id: string // slug
  name: string
  source: string | null
  version: number
  editable: boolean
  basedOn: string | null
  weeks: { number: number; days: ProgramDayOut[] }[]
}

/** An exercise to pick from: in the user's programs or logged by them. */
export interface CatalogExercise {
  name: string
  sets: number // main sets the user logged; the server sorts by this desc, then by name
}

/** Programs the user may choose (templates and own copies), oldest first. */
export function getPrograms(): Promise<ProgramSummary[]> {
  return api<ProgramSummary[]>('/programs')
}

/** The whole program, sorted. 404 {"detail": "unknown program"} for an unknown slug or another user's copy. */
export function getProgramOut(slug: string, init: RequestInit = {}): Promise<ProgramOut> {
  return api<ProgramOut>(`/programs/${encodeURIComponent(slug)}`, init)
}

/** Never cached offline: the picker falls back to the program's exercises. */
export function getExercises(): Promise<CatalogExercise[]> {
  return api<CatalogExercise[]>('/exercises')
}
