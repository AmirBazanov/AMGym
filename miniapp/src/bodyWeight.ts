// Body weight helpers for the Progress screen. Type-only imports keep this module free of browser
// globals (tested in node). Dates are YYYY-MM-DD strings in the server TIMEZONE; never parsed as UTC.
import type { BodyWeight, BodyWeightSource } from './api'

/** Same limits as POST /api/body-weight (422 outside). */
export const WEIGHT_MIN = 30
export const WEIGHT_MAX = 250
/** GET /api/body-weight accepts days 1..3660; the screen loads everything once and filters locally. */
export const WEIGHT_DAYS_ALL = 3660

export const WEIGHT_PERIODS: { days: number | null; label: string }[] = [
  { days: 30, label: '1 мес' },
  { days: 90, label: '3 мес' },
  { days: null, label: 'Всё' },
]

export const WEIGHT_EMPTY_TEXT = 'Взвешивайся утром натощак и пиши боту «вес 84.6» или запиши здесь'

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const SOURCES: readonly BodyWeightSource[] = ['chat', 'miniapp', 'mcp']

/**
 * Items of GET /api/body-weight, defensively: malformed items are dropped, an unknown source reads as
 * "chat", one value per date (the last one wins), sorted oldest first.
 */
export function parseBodyWeights(raw: unknown): BodyWeight[] {
  if (!Array.isArray(raw)) return []
  const byDate = new Map<string, BodyWeight>()
  for (const it of raw) {
    const item = parseBodyWeight(it)
    if (item) byDate.set(item.date, item)
  }
  return sortByDate([...byDate.values()])
}

/** One item (also the POST answer); null when the shape is wrong. */
export function parseBodyWeight(raw: unknown): BodyWeight | null {
  if (raw == null || typeof raw !== 'object') return null
  const { date, weightKg, source } = raw as Record<string, unknown>
  if (typeof date !== 'string' || !DATE_RE.test(date)) return null
  if (typeof weightKg !== 'number' || !Number.isFinite(weightKg) || weightKg <= 0) return null
  const src = SOURCES.includes(source as BodyWeightSource) ? (source as BodyWeightSource) : 'chat'
  return { date, weightKg, source: src }
}

function sortByDate(list: BodyWeight[]): BodyWeight[] {
  return list.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
}

/** Replaces the value of the item's date (one value per day), keeps the order oldest first. */
export function upsertWeight(list: readonly BodyWeight[], item: BodyWeight): BodyWeight[] {
  return sortByDate([...list.filter((x) => x.date !== item.date), item])
}

export function dropWeight(list: readonly BodyWeight[], date: string): BodyWeight[] {
  return list.filter((x) => x.date !== date)
}

/** YYYY-MM-DD shifted by `n` days, calendar math without time zones. */
export function addDays(date: string, n: number): string {
  const [y, m, d] = date.split('-').map(Number)
  const dt = new Date(Date.UTC(y, m - 1, d + n))
  return dt.toISOString().slice(0, 10)
}

/** Entries of the last `days` days counted back from `today` (inclusive); null keeps everything. */
export function filterPeriod(list: readonly BodyWeight[], days: number | null, today: string): BodyWeight[] {
  if (days == null) return [...list]
  const since = addDays(today, -days)
  return list.filter((x) => x.date >= since && x.date <= today)
}

export interface WeightChange {
  delta: number // latest - reference, kg, rounded to 0.1
  from: BodyWeight
}

/**
 * Change of the newest value against the newest entry at least `days` days older. The reference must be
 * no older than 2 x `days`, otherwise "за 7 дней" would really be a two-month change: null then, and when
 * there is no older entry at all. `list` is oldest first.
 */
export function weightChange(list: readonly BodyWeight[], days: number): WeightChange | null {
  const latest = list[list.length - 1]
  if (!latest) return null
  const upTo = addDays(latest.date, -days)
  const notBefore = addDays(latest.date, -2 * days)
  for (let i = list.length - 2; i >= 0; i--) {
    const x = list[i]
    if (x.date > upTo) continue
    if (x.date < notBefore) return null
    return { delta: Math.round((latest.weightKg - x.weightKg) * 10) / 10, from: x }
  }
  return null
}

/** Why the typed weight can't be saved; null when it can. Step 0.1 kg: 84.65 is rejected, not rounded. */
export function weightInputError(v: number | null): string | null {
  if (v == null || !Number.isFinite(v)) return 'Введи вес в килограммах'
  if (v < WEIGHT_MIN || v > WEIGHT_MAX) return `Вес от ${WEIGHT_MIN} до ${WEIGHT_MAX} кг`
  if (Math.abs(v * 10 - Math.round(v * 10)) > 1e-6) return 'Не точнее 0,1 кг'
  return null
}

/** Message for a failed save; never the raw server body (422 detail may be a string or a list). */
export function weightSaveErrorText(status: number | null): string {
  if (status === 422) return `Сервер не принял вес: нужно от ${WEIGHT_MIN} до ${WEIGHT_MAX} кг с шагом 0,1.`
  if (status === 401 || status === 403) return 'Не получилось войти. Закрой дневник и открой его заново из бота.'
  return 'Не удалось сохранить вес. Попробуй ещё раз.'
}

/** "84,6". */
export function formatWeight(kg: number): string {
  return (Math.round(kg * 10) / 10).toFixed(1).replace('.', ',')
}

/** "+0,4" / "−1,2" / "0,0" with a real minus sign. */
export function formatDelta(kg: number): string {
  const r = Math.round(kg * 10) / 10
  const abs = Math.abs(r).toFixed(1).replace('.', ',')
  return r > 0 ? `+${abs}` : r < 0 ? `−${abs}` : abs
}

/** "07.10" from YYYY-MM-DD without a UTC shift. */
export function formatDayShort(date: string): string {
  const [, m, d] = date.split('-')
  return `${d}.${m}`
}

/** Chart points: label dd.mm, weight in kg. */
export function chartPoints(list: readonly BodyWeight[]): { label: string; kg: number }[] {
  return list.map((x) => ({ label: formatDayShort(x.date), kg: x.weightKg }))
}

export const SOURCE_LABEL: Record<BodyWeightSource, string> = {
  chat: 'из чата',
  miniapp: 'здесь',
  mcp: 'через MCP',
}
