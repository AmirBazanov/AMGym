// Fact labels and list helpers. Type-only imports keep this module free of browser globals (tested in node).
import type { Fact, FactCategory } from './api'

/** Picker order: the most common categories first, "other" last. */
export const FACT_CATEGORIES: { key: FactCategory; label: string }[] = [
  { key: 'food', label: 'Еда' },
  { key: 'training', label: 'Тренировки' },
  { key: 'health', label: 'Здоровье' },
  { key: 'schedule', label: 'Расписание' },
  { key: 'other', label: 'Прочее' },
]

/** Russian badge label; an unknown category from a newer server reads as "Прочее". */
export function categoryLabel(category: string | null | undefined): string {
  return FACT_CATEGORIES.find((c) => c.key === category)?.label ?? 'Прочее'
}

/** Normalizes a category for the picker: anything unknown becomes "other". */
export function knownCategory(category: string | null | undefined): FactCategory {
  return FACT_CATEGORIES.find((c) => c.key === category)?.key ?? 'other'
}

/** Newest first, as the server sends them; ties by id so edits never reshuffle the list. */
export function sortFacts(facts: Fact[]): Fact[] {
  return [...facts].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.id - a.id)
}

export function activeCount(facts: Fact[]): number {
  return facts.filter((f) => f.active).length
}

/** Text as it will be sent: trimmed, inner whitespace runs collapsed. */
export function cleanFactText(text: string): string {
  return text.trim().replace(/\s+/g, ' ')
}

/** null when the text can be saved, otherwise why not. */
export function factTextError(text: string, max: number): 'empty' | 'long' | null {
  const t = cleanFactText(text)
  if (!t) return 'empty'
  return t.length > max ? 'long' : null
}
