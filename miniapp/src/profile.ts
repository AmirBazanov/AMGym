// Profile form helpers. Type-only imports keep this module free of browser globals (tested in node).
import type { Goal, Profile } from './api'

export const GOALS: { key: Goal; label: string }[] = [
  { key: 'mass', label: 'Масса' },
  { key: 'cut', label: 'Сушка' },
  { key: 'strength', label: 'Сила' },
  { key: 'health', label: 'Здоровье' },
]

export const ABOUT_MAX = 500

/** Same limits as the server; the birth year bound moves with the calendar. */
export function profileLimits(now = new Date()) {
  return {
    weightKg: { min: 30, max: 300 },
    heightCm: { min: 120, max: 250 },
    birthYear: { min: 1930, max: now.getFullYear() - 10 },
  } as const
}

/** What would be stored: weight to 0.1 kg, whole cm and years, trimmed text with "" meaning not set. */
export function normalizeProfile(p: Profile): Profile {
  const about = p.about?.trim() ?? ''
  return {
    weightKg: p.weightKg == null ? null : Math.round(p.weightKg * 10) / 10,
    heightCm: p.heightCm == null ? null : Math.round(p.heightCm),
    birthYear: p.birthYear == null ? null : Math.round(p.birthYear),
    goal: p.goal,
    about: about ? about : null,
  }
}

export type ProfileErrors = Partial<Record<'weightKg' | 'heightCm' | 'birthYear' | 'about', true>>

/** Fields out of the server's range. Empty (null) values are valid: they clear the field. */
export function profileErrors(p: Profile, now = new Date()): ProfileErrors {
  const n = normalizeProfile(p)
  const lim = profileLimits(now)
  const errors: ProfileErrors = {}
  for (const k of ['weightKg', 'heightCm', 'birthYear'] as const) {
    const v = n[k]
    if (v != null && (v < lim[k].min || v > lim[k].max)) errors[k] = true
  }
  if (n.about != null && n.about.length > ABOUT_MAX) errors.about = true
  return errors
}

/** Only the keys that differ from the saved profile, normalized; empty when nothing changed. */
export function profilePatch(draft: Profile, saved: Profile): Partial<Profile> {
  const d = normalizeProfile(draft)
  const s = normalizeProfile(saved)
  const patch: Partial<Profile> = {}
  for (const k of Object.keys(d) as (keyof Profile)[]) {
    if (d[k] !== s[k]) Object.assign(patch, { [k]: d[k] })
  }
  return patch
}
