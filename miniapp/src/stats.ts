import type { ExerciseLog, SetEntry, Workout } from './store'

/** Epley estimate of the one-rep max. */
export function e1rm(weight: number, reps: number): number {
  return reps <= 1 ? weight : weight * (1 + reps / 30)
}

export function setVolume(s: SetEntry): number {
  return s.done && s.weight != null && s.reps != null ? s.weight * s.reps : 0
}

export function exerciseVolume(ex: ExerciseLog): number {
  return ex.sets.reduce((sum, s) => sum + setVolume(s), 0)
}

export function workoutVolume(w: Workout): number {
  return w.exercises.reduce((sum, ex) => sum + exerciseVolume(ex), 0)
}

export function workoutSetCount(w: Workout): number {
  return w.exercises.reduce((n, ex) => n + ex.sets.filter((s) => s.done).length, 0)
}

export interface ExercisePoint {
  date: string // ISO
  label: string // dd.mm
  maxWeight: number
  e1rm: number
  volume: number
  sets: number
}

export function exerciseSeries(history: Workout[], name: string, sinceDays: number | null): ExercisePoint[] {
  const since = sinceDays == null ? 0 : Date.now() - sinceDays * 86_400_000
  const out: ExercisePoint[] = []
  for (const w of history) {
    if (new Date(w.startedAt).getTime() < since) continue
    const ex = w.exercises.find((e) => e.name === name)
    if (!ex) continue
    const done = ex.sets.filter((s) => s.done && s.weight != null)
    if (!done.length) continue
    out.push({
      date: w.startedAt,
      label: formatShortDate(w.startedAt),
      maxWeight: Math.max(...done.map((s) => s.weight!)),
      e1rm: Math.round(Math.max(...done.map((s) => e1rm(s.weight!, s.reps ?? 1))) * 10) / 10,
      volume: Math.round(exerciseVolume(ex)),
      sets: done.length,
    })
  }
  return out
}

export function weeklyVolume(history: Workout[]): { week: number; volume: number; sets: number }[] {
  const byWeek = new Map<number, { volume: number; sets: number }>()
  for (const w of history) {
    const cur = byWeek.get(w.week) ?? { volume: 0, sets: 0 }
    cur.volume += workoutVolume(w)
    cur.sets += workoutSetCount(w)
    byWeek.set(w.week, cur)
  }
  return [...byWeek.entries()].sort((a, b) => a[0] - b[0]).map(([week, v]) => ({ week, ...v }))
}

export function formatShortDate(iso: string): string {
  const d = new Date(iso)
  return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}`
}

export function formatLongDate(iso: string): string {
  return new Date(iso).toLocaleDateString('ru-RU', { weekday: 'short', day: 'numeric', month: 'long' })
}

/** "3 окт" (no trailing dot that ru-RU puts after short months). */
export function formatDayMonth(iso: string): string {
  return new Date(iso).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }).replace(/\.$/, '')
}

export function formatKg(n: number): string {
  return Number.isInteger(n) ? `${n}` : n.toFixed(1).replace('.', ',')
}

export function formatTonnage(kg: number): string {
  return kg >= 1000 ? `${(kg / 1000).toFixed(1).replace('.', ',')} т` : `${Math.round(kg)} кг`
}
