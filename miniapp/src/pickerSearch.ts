// Search in the exercise picker (the day editor): similar names first, so a typo does not create a twin
// exercise with its own empty history. Pure, node-safe.
import { normalizeName } from './programEdit'

/** Edit distance with an early exit above `max` (names are short, the lists a few hundred at most). */
export function editDistance(a: string, b: string, max = Infinity): number {
  if (Math.abs(a.length - b.length) > max) return max + 1
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    const cur = [i]
    let best = i
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
      best = Math.min(best, cur[j])
    }
    if (best > max) return max + 1
    prev = cur
  }
  return prev[b.length]
}

/** Typos allowed in a word of this length: none in short words, one from 5 letters, two from 9. */
function tolerance(len: number): number {
  return len >= 9 ? 2 : len >= 5 ? 1 : 0
}

/**
 * How well `name` matches the normalized query `q`: 0 the same, 1 starts with it, 2 a word starts with
 * it, 3 contains it, 4 every query word starts a word of the name, 5 the same with typos; null: no match.
 */
export function matchScore(name: string, q: string): number | null {
  const n = normalizeName(name)
  if (!q) return 3
  if (n === q) return 0
  if (n.startsWith(q)) return 1
  const words = n.split(' ')
  if (words.some((w) => w.startsWith(q))) return 2
  if (n.includes(q)) return 3
  const qs = q.split(' ')
  if (qs.every((x) => words.some((w) => w.startsWith(x)))) return 4
  const typo = (x: string) => {
    const tol = tolerance(x.length)
    return tol > 0 && words.some((w) => editDistance(x, w.slice(0, x.length), tol) <= tol || editDistance(x, w, tol) <= tol)
  }
  if (qs.every((x) => words.some((w) => w.startsWith(x)) || typo(x))) return 5
  return null
}

/** Names matching `query`, best first; ties keep the given order (program first, then most done). */
export function searchNames(names: readonly string[], query: string): string[] {
  const q = normalizeName(query)
  if (!q) return [...names]
  return names
    .map((name, i) => ({ name, i, s: matchScore(name, q) }))
    .filter((x): x is { name: string; i: number; s: number } => x.s != null)
    .sort((a, b) => a.s - b.s || a.i - b.i)
    .map((x) => x.name)
}

/** The name in `names` the query means exactly (case, ё/е, spaces ignored), if any. */
export function exactName(names: readonly string[], query: string): string | undefined {
  const q = normalizeName(query)
  return q ? names.find((n) => normalizeName(n) === q) : undefined
}
