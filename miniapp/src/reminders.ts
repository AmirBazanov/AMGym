// Reminder labels. Type-only imports keep this module free of browser globals (tested in node).
import type { Reminder, ReminderKind } from './api'

/** Contract weekday: 0 = Monday .. 6 = Sunday (unlike program.ts, whose WEEKDAY_SHORT is 1-based). */
export const REMINDER_WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
const WEEKLY = ['по понедельникам', 'по вторникам', 'по средам', 'по четвергам', 'по пятницам', 'по субботам', 'по воскресеньям']

export const KIND_TITLE: Record<Exclude<ReminderKind, 'text'>, string> = {
  nutrition: 'Сводка КБЖУ',
  advice: 'Советы недели',
}

/** Defaults offered for a new reminder of each kind (weekly advice fits the end of the week). */
export const KIND_DEFAULTS: Record<ReminderKind, { time: string; weekday: number | null }> = {
  text: { time: '09:00', weekday: null },
  nutrition: { time: '09:00', weekday: null },
  advice: { time: '19:00', weekday: 6 },
}

export function weekdayShort(weekday: number | null | undefined): string {
  return weekday == null ? '' : (REMINDER_WEEKDAYS[weekday] ?? '')
}

/** "Пн 09:00" for weekly reminders, just "09:00" for daily ones. */
export function reminderWhen(r: Pick<Reminder, 'time'> & { weekday?: number | null }): string {
  const day = weekdayShort(r.weekday)
  return day ? `${day} ${r.time}` : r.time
}

/** "каждый день" or "по понедельникам". */
export function repeatPhrase(weekday: number | null | undefined): string {
  return weekday == null ? 'каждый день' : (WEEKLY[weekday] ?? 'каждый день')
}

export function reminderTitle(r: Pick<Reminder, 'kind' | 'text'>): string {
  return r.kind === 'text' ? (r.text ?? '') : (KIND_TITLE[r.kind] ?? '')
}
