import {
  LIFE_PARSERS,
  parseNote,
  parseSettings,
  parseView,
  type DoneKind,
  type LifeItems,
  type LifeKind,
  type LifeNote,
  type LifeSettings,
  type LifeView,
} from './life'

// Requests carry Telegram initData; the backend validates its HMAC with the bot token before trusting
// the user id. Never trust initDataUnsafe on the server. Outside Telegram (a computer browser) the bot's
// /web command gives a link `/?login=<token>`: the token is kept in localStorage and sent as X-App-Token.
const initData: string =
  (window as unknown as { Telegram?: { WebApp?: { initData?: string } } }).Telegram?.WebApp?.initData ?? ''

/** Opened from Telegram (as opposed to a plain browser preview). */
export const inTelegram = initData.length > 0

const APP_TOKEN_KEY = 'gymapp.appToken'

/**
 * `?login=<token>` of this page load, removed from the address at once (history.replaceState, other params
 * and the hash stay): the token must not stay in the address bar, the history or a shared link.
 */
function takeLoginParam(): string | null {
  try {
    const url = new URL(location.href)
    const token = url.searchParams.get('login')
    if (token == null) return null
    url.searchParams.delete('login')
    history.replaceState(history.state, '', url.pathname + url.search + url.hash)
    return token.trim() || null
  } catch {
    return null
  }
}

function readAppToken(): string | null {
  try {
    return localStorage.getItem(APP_TOKEN_KEY) || null
  } catch {
    return null
  }
}

function writeAppToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(APP_TOKEN_KEY, token)
    else localStorage.removeItem(APP_TOKEN_KEY)
  } catch {
    // Storage unavailable (private mode): the app works without the computer login.
  }
}

// Login CSRF guard: a foreign link must not silently replace a login. Inside Telegram `?login` is ignored
// (initData decides who it is). A first login is stored, and the app shows whose it is (freshLogin). A link
// with another token than the stored one waits for the user's «Войти по новой ссылке?» (takePendingLogin).
const loginParam = takeLoginParam()
let pendingLogin: string | null = null
if (!inTelegram && loginParam) {
  const stored = readAppToken()
  if (!stored || stored === loginParam) writeAppToken(loginParam)
  else pendingLogin = loginParam
}

/** Token of the computer login (/web in the bot); used only when there is no Telegram initData. */
const appToken: string | null = inTelegram ? null : readAppToken()

/** Requests are authenticated: opened from Telegram, or logged in from a computer with /web. */
export const hasAuth = inTelegram || appToken != null

/** Logged in from a computer with the /web token (not in Telegram). */
export const webLogin = !inTelegram && appToken != null

/** This page load stored the token from a /web link: show whose login it is, so a foreign link is seen. */
export const freshLogin = webLogin && loginParam === appToken

/** A /web link with a token other than the stored one; returned once. Confirm, then switchAppLogin. */
export function takePendingLogin(): string | null {
  const t = pendingLogin
  pendingLogin = null
  return t
}

/** Replace the computer login with `token` and reload (the store and live updates start from scratch). */
export function switchAppLogin(token: string): void {
  writeAppToken(token)
  location.reload()
}

/** Forget the computer login and reload: «Открой /web в боте» again. */
export function logoutAppLogin(): void {
  writeAppToken(null)
  location.reload()
}

/** Auth headers of every API request: initData inside Telegram, otherwise the /web token if there is one. */
export function authHeaders(): Record<string, string> {
  if (appToken) return { 'X-App-Token': appToken }
  // Outside Telegram without a token the empty header stays: local dev with DEV_USER_ID answers to it.
  return { 'X-Telegram-Init-Data': initData }
}

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
    // Without a caller's signal a hung request would never settle: give up after API_TIMEOUT_MS.
    signal: init.signal ?? timeoutSignal(API_TIMEOUT_MS),
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...init.headers },
  })
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${await res.text()}`)
  return { status: res.status, data: (res.status === 204 ? undefined : await res.json()) as T }
}

/** Default timeout of api() and apiWithStatus() when the caller passes no `signal`. */
export const API_TIMEOUT_MS = 15_000

/** The day plan may wait for the LLM (several routes with failover): longer than API_TIMEOUT_MS. */
const PLAN_TIMEOUT_MS = 60_000

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
  return api<DayPlan>('/plan/today', { signal: timeoutSignal(PLAN_TIMEOUT_MS) })
}

export function regenerateTodayPlan(): Promise<DayPlan> {
  return api<DayPlan>('/plan/today/regenerate', { method: 'POST', signal: timeoutSignal(PLAN_TIMEOUT_MS) })
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

// ---- «Дела» (life): /api/life/*, contract in docs/superpowers/plans/2026-10-29-life-assistant.md, task 1.3.
// Types, parsing and the rest of the logic live in life.ts. Never cached offline. ----

/** An answer that does not parse (an older server, a proxy page) is a 502-like error, not a crash later. */
function parsed<T>(value: T | null, what: string): T {
  if (value == null) throw new ApiError(502, `502 bad ${what}`)
  return value
}

/** The view for `date` (its agenda; the server's own "today" without it). */
export async function getLifeView(date?: string): Promise<LifeView> {
  return parsed(parseView(await api<unknown>(`/life/view${date ? `?date=${date}` : ''}`)), 'life view')
}

export async function createLife<K extends LifeKind>(kind: K, body: Record<string, unknown>): Promise<LifeItems[K]> {
  const raw = await api<unknown>(`/life/${kind}`, { method: 'POST', body: JSON.stringify(body) })
  return parsed(LIFE_PARSERS[kind](raw), kind)
}

/** Partial update: only the given keys change, null clears. 404 when gone, 422 with a Russian detail. */
export async function updateLife<K extends LifeKind>(kind: K, id: number, body: Record<string, unknown>): Promise<LifeItems[K]> {
  const raw = await api<unknown>(`/life/${kind}/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
  return parsed(LIFE_PARSERS[kind](raw), kind)
}

export function deleteLife(kind: LifeKind, id: number): Promise<void> {
  return api<void>(`/life/${kind}/${id}`, { method: 'DELETE' })
}

/** POST …/{id}/done or /undone. A chore's done takes the day it was done (the server's today without it). */
export async function setLifeDone<K extends DoneKind>(kind: K, id: number, done: boolean, day?: string): Promise<LifeItems[K]> {
  const init: RequestInit = { method: 'POST' }
  if (done && day) init.body = JSON.stringify({ day })
  const raw = await api<unknown>(`/life/${kind}/${id}/${done ? 'done' : 'undone'}`, init)
  return parsed(LIFE_PARSERS[kind](raw), kind)
}

/** The note with its Markdown body. */
export async function getLifeNote(id: number): Promise<LifeNote> {
  return parsed(parseNote(await api<unknown>(`/life/notes/${id}`)), 'note')
}

/** Partial settings. The saved settings when the server answers with them; null for 204 or another body. */
export async function putLifeSettings(patch: Partial<LifeSettings>): Promise<LifeSettings | null> {
  return parseSettings(await api<unknown>('/life/settings', { method: 'PUT', body: JSON.stringify(patch) }))
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

// ---- Program editor: PATCH /api/programs/{slug} (spec section «API», phase 2: replace/prescribe/add/remove/reorder;
// phase 3: move_day) ----

/** ProgramItem.id, or the `tempId` of an `add` earlier in the same request. */
export type ItemRef = number | string

export type OpIntensity = 'heavy' | 'medium' | 'light' | null

/** The full prescription of an `add`/`prescribe`: dropset (dropReps, reps null) or reps min..max. */
export interface OpPrescription {
  sets: number
  repsMin: number | null
  repsMax: number | null
  dropReps: number[] | null
  intensity: OpIntensity
}

// `weeks`: where else the edit goes (includes `week`); absent: only `week`. A prescribe without the
// `intensity` key keeps each week's own intensity; null clears it.
export type ProgramOp =
  | { op: 'replace'; week: number; weekday: number; itemId: ItemRef; name: string; weeks?: number[] }
  | ({ op: 'prescribe'; week: number; weekday: number; itemId: ItemRef; weeks?: number[] } & Omit<OpPrescription, 'intensity'> & {
        intensity?: OpIntensity
      })
  | ({ op: 'add'; week: number; weekday: number; tempId: string; name: string; position: number; weeks?: number[] } & OpPrescription)
  | { op: 'remove'; week: number; weekday: number; itemId: ItemRef; weeks?: number[] }
  | { op: 'reorder'; week: number; weekday: number; itemIds: ItemRef[]; weeks?: number[] }
  | MoveDayOp

/**
 * The day on `weekday` moves to `toWeekday`; a day already there takes `weekday` (a swap). Only the weekday
 * changes: ProgramDay.id, items and focus stay, so workouts linked to the day (✓) move with it. Weeks before
 * the current program week are skipped (reason «неделя уже прошла»); the source week there is a 422.
 */
export interface MoveDayOp {
  op: 'move_day'
  week: number
  weekday: number
  toWeekday: number // 1 = Monday .. 7 = Sunday
  weeks?: number[]
}

export interface ProgramPatch {
  version: number
  dryRun?: boolean
  ops: ProgramOp[]
}

/** Where op number `op` (index in `ops`) landed and which weeks it skipped, with a reason in Russian. */
export interface OpResult {
  op: number
  weeks: number[]
  skipped: { week: number; reason: string }[]
}

export interface ProgramPatchOut {
  program: ProgramOut
  switchedFrom: string | null // a copy of this template was created and became the active program
  results: OpResult[]
}

/**
 * PATCH /api/programs/{slug}. Resolves with the status and the parsed body for 200, 409 (`{detail:
 * "version", program}` or `{detail: "not_active"}`), 422 and 404; rejects on network errors and timeouts.
 */
export async function patchProgram(slug: string, body: ProgramPatch): Promise<{ status: number; body: unknown }> {
  const res = await fetch(`./api/programs/${encodeURIComponent(slug)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
    signal: timeoutSignal(20_000),
  })
  let parsed: unknown = null
  try {
    parsed = await res.json()
  } catch {
    // Not JSON (a proxy error page): the status alone decides.
  }
  return { status: res.status, body: parsed }
}
