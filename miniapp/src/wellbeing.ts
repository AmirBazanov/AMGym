// Wellbeing formatting. Type-only imports keep this module free of browser globals (tested in node).
import type { WellbeingEntry } from './api'

export interface WellbeingDay {
  date: string // YYYY-MM-DD in the server TIMEZONE
  entries: WellbeingEntry[] // newest first
}

/** 6 -> "6", 6.5 -> "6,5". */
function num(n: number): string {
  return String(Math.round(n * 10) / 10).replace('.', ',')
}

function score(n: number | null | undefined): string | null {
  return n == null ? null : `${n}/5`
}

function sleepHours(h: number | null | undefined): string | null {
  return h == null ? null : `${num(h)} ч`
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function painPlaces(e: WellbeingEntry): string[] {
  return (e.pains ?? []).map((p) => p.place.trim()).filter(Boolean)
}

/**
 * One line for lists: "Сон 6 ч · энергия 2/5 · болит левое плечо".
 * Sleep quality is shown only when the hours are unknown; severities and the note only in the details.
 */
export function formatWellbeing(e: WellbeingEntry): string {
  const parts: string[] = []
  const sleep = sleepHours(e.sleepHours) ?? score(e.sleepQuality)
  if (sleep) parts.push(`сон ${sleep}`)
  if (e.energy != null) parts.push(`энергия ${e.energy}/5`)
  if (e.mood != null) parts.push(`настроение ${e.mood}/5`)
  const places = painPlaces(e)
  if (places.length) parts.push(`${places.length === 1 ? 'болит' : 'болят'} ${places.join(', ')}`)
  if (!parts.length) {
    const note = e.note?.trim()
    return note ? capitalize(note) : 'Без подробностей'
  }
  return capitalize(parts.join(' · '))
}

/**
 * Label/value rows for the full view of one entry; empty fields are left out.
 * Severity goes in parentheses so it reads as belonging to its own place: "плечо, поясница (2/5)".
 */
export function wellbeingDetails(e: WellbeingEntry): { label: string; value: string }[] {
  const rows: { label: string; value: string }[] = []
  const add = (label: string, value: string | null) => value && rows.push({ label, value })
  add('Сон', sleepHours(e.sleepHours))
  add('Качество сна', score(e.sleepQuality))
  add('Энергия', score(e.energy))
  add('Настроение', score(e.mood))
  const pains = (e.pains ?? [])
    .filter((p) => p.place.trim())
    .map((p) => (p.severity != null ? `${p.place.trim()} (${p.severity}/5)` : p.place.trim()))
  add('Боли', pains.length ? pains.join(', ') : null)
  add('Заметка', e.note?.trim() || null)
  return rows
}

/** Groups by the server's `date` (never re-derived from notedAt), newest day and newest entry first. */
export function groupByDate(entries: WellbeingEntry[]): WellbeingDay[] {
  const byDate = new Map<string, WellbeingEntry[]>()
  for (const e of entries) {
    const list = byDate.get(e.date)
    if (list) list.push(e)
    else byDate.set(e.date, [e])
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => (a < b ? 1 : a > b ? -1 : 0))
    .map(([date, list]) => ({ date, entries: [...list].sort((a, b) => Date.parse(b.notedAt) - Date.parse(a.notedAt)) }))
}

/** Device-local YYYY-MM-DD; the app assumes the phone is in the server TIMEZONE, as store.ts does. */
export function localISODate(d = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** HH:MM of `notedAt` in device-local time. */
export function formatNotedTime(iso: string): string {
  const d = new Date(iso)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** "7 октября, ср" from YYYY-MM-DD without a UTC shift. */
export function formatWellbeingDate(date: string): string {
  const [y, m, d] = date.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  const month = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'][m - 1]
  const wd = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'][dt.getDay()]
  return `${d} ${month}, ${wd}`
}

/** "2 записи". */
export function entriesCount(n: number): string {
  const m10 = n % 10
  const m100 = n % 100
  const form = m10 === 1 && m100 !== 11 ? 'запись' : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'записи' : 'записей'
  return `${n} ${form}`
}
