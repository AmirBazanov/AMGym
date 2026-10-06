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

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`./api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData, ...init.headers },
  })
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${await res.text()}`)
  return (res.status === 204 ? undefined : await res.json()) as T
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

export type ReminderKind = 'text' | 'nutrition' | 'advice'

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
