// Day weight suggestions: the record (estimated 1RM) and double progression, the higher one wins.
// Pure functions: history and the program exercise come in as arguments, nothing is read from the store.
// Without any history the owner's own words (baselines from the bot chat) give a starting weight; without
// those a related exercise of the history does (`related`). The bot computes the same numbers in
// bot/src/gymbot/services/next_weights.py; data/progression_cases.json holds the shared vectors.
// History sets of a dropset are already the first set of each dropset: the server folds the drops
// (drop_index > 0) into their main set, so every SetEntry here is a working set.
import type { Intensity, Prescription, ProgramExercise } from './program'
import type { Baseline, SetEntry, WeightOverride, Workout } from './store'
import { e1rm, formatKg } from './stats'

export interface Record1rm {
  e1rm: number
  weight: number
  reps: number
  date: string // ISO, startedAt of the workout
}

/** Where a suggestion comes from; "hint" and "none" carry no weight. */
export type Source = 'override' | 'history' | 'baseline' | 'related' | 'hint' | 'none'

export interface Suggestion {
  /** Null for "hint" (start around `hintKg`) and "none": no number to fill in. */
  weight: number | null
  reason: string
  source: Source
  /** Dumbbells: the weight of one dumbbell («на руку»). */
  perHand: boolean
  /** "hint": the first set of the related exercise's last session, a starting point, not a computed weight. */
  hintKg: number | null
  /** The history exercise a transfer, hint or "none" refers to. */
  related: string | null
  /** The related exercise's record set (weight per hand for dumbbells) a transfer started from. */
  relatedSet?: { weight: number; reps: number }
  /** The owner set this weight for today himself: used as is, no plan factor, no rounding. */
  override?: WeightOverride
}

/** Genitive after "для": для 1 повтора, для 8 повторов. */
function repsWord(n: number): string {
  return `${n} ${n % 10 === 1 && n % 100 !== 11 ? 'повтора' : 'повторов'}`
}

/** Removes float noise (60 + 2.5 * 0.1 ...) so 62.5 is never printed as 62.4999. */
function clean(n: number): number {
  return Math.round(n * 100) / 100
}

function workingSets(history: Workout, name: string): SetEntry[] {
  const ex = history.exercises.find((e) => e.name === name)
  return ex?.sets.filter((s) => s.done && s.weight != null) ?? []
}

/** Best Epley 1RM over all completed weighted sets: the exercise record. Same rule as stats.exerciseSeries. */
export function bestE1rm(history: Workout[], name: string): Record1rm | null {
  let best: Record1rm | null = null
  for (const w of history) {
    for (const s of workingSets(w, name)) {
      if (s.weight! <= 0) continue
      const reps = s.reps ?? 1
      const value = e1rm(s.weight!, reps)
      if (!best || value > best.e1rm) best = { e1rm: value, weight: s.weight!, reps, date: w.startedAt }
    }
  }
  return best
}

/** Smallest weight increment: dumbbells go up by 1 kg, barbells and machines by 2.5 kg. */
export function equipmentStep(name: string): 1 | 2.5 {
  return name.toLowerCase().includes('гантел') ? 1 : 2.5
}

export function roundToStep(weight: number, step: number): number {
  return clean(Math.round(weight / step) * step)
}

/**
 * Share of 1RM for a working weight. Heavy = the weight that is a true max for that many reps
 * (inverse Epley), medium = 90 % of it, light = 80 %. No intensity counts as medium.
 */
export function percentOf1rm(repsMax: number | null, intensity: Intensity | null, repsMin: number | null = null): number {
  const reps = repsMax ?? repsMin ?? 10
  const heavy = 1 / (1 + reps / 30)
  if (intensity === 'heavy') return heavy
  if (intensity === 'light') return heavy * 0.8
  return heavy * 0.9
}

/** Completed weighted sets of the latest workout that has the exercise (same as store.lastSetsFor). */
export function lastSameSession(history: Workout[], name: string): SetEntry[] | null {
  for (let i = history.length - 1; i >= 0; i--) {
    const done = workingSets(history[i], name)
    if (done.length) return done
  }
  return null
}

/** Reps every working set must reach to earn the next step: first drop for dropsets, else the top of the range. */
export function progressionReps(p: Prescription): number | null {
  return p.drop_reps?.[0] ?? p.reps_max ?? p.reps_min
}

/**
 * Double progression: last time's top weight, plus one step when every working set was done at that
 * same weight with at least the top of the rep range. Missed reps keep the weight (never lowered).
 */
export function doubleProgression(last: SetEntry[] | null, prescription: Prescription, step: number): number | null {
  const sets = last?.filter((s) => s.weight != null) ?? []
  if (!sets.length) return null
  const top = Math.max(...sets.map((s) => s.weight!))
  const target = progressionReps(prescription)
  if (target == null) return top
  const allAtTop = sets.every((s) => s.weight === top && s.reps != null && s.reps >= target)
  return allAtTop ? clean(top + step) : top
}

/** Case, whitespace and ё/е do not matter for baseline names: "Жим  лёжа " is "жим лежа" (as on the server). */
function normName(name: string): string {
  return name.trim().replace(/\s+/g, ' ').toLowerCase().replace(/ё/g, 'е')
}

/**
 * Server baselines made safe: absent (older server) or not a list -> [], entries without a name or a
 * positive weight dropped, reps below 1 count as unknown.
 */
export function normalizeBaselines(raw: unknown): Baseline[] {
  if (!Array.isArray(raw)) return []
  const out: Baseline[] = []
  for (const b of raw as Partial<Baseline>[]) {
    if (!b || typeof b.exercise !== 'string' || !b.exercise.trim()) continue
    if (typeof b.weightKg !== 'number' || !Number.isFinite(b.weightKg) || b.weightKg <= 0) continue
    const reps = typeof b.reps === 'number' && Number.isFinite(b.reps) && b.reps >= 1 ? Math.round(b.reps) : null
    out.push({ exercise: b.exercise, weightKg: b.weightKg, reps, factId: typeof b.factId === 'number' ? b.factId : 0 })
  }
  return out
}

/** The owner's latest words about this exercise (highest factId when the server sends several). */
export function findBaseline(baselines: readonly Baseline[], name: string): Baseline | null {
  const key = normName(name)
  let best: Baseline | null = null
  for (const b of baselines) {
    if (normName(b.exercise) === key && (!best || b.factId > best.factId)) best = b
  }
  return best
}

/** Hint for a weight the owner set for today: «ты поставил на сегодня 85 кг». */
export function overrideReason(kg: number): string {
  return `ты поставил на сегодня ${formatKg(kg)} кг`
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/

/**
 * Server weight overrides made safe: absent or not a list -> [], entries without a name, a positive
 * weight or a YYYY-MM-DD date dropped.
 */
export function normalizeOverrides(raw: unknown): WeightOverride[] {
  if (!Array.isArray(raw)) return []
  const out: WeightOverride[] = []
  for (const o of raw as Partial<WeightOverride>[]) {
    if (!o || typeof o.exercise !== 'string' || !o.exercise.trim()) continue
    if (typeof o.weightKg !== 'number' || !Number.isFinite(o.weightKg) || o.weightKg <= 0) continue
    if (typeof o.date !== 'string' || !ISO_DAY.test(o.date)) continue
    out.push({ exercise: o.exercise, weightKg: o.weightKg, date: o.date })
  }
  return out
}

/** Overrides from a server answer: an absent field (older server) keeps what the app has, like baselines. */
export function mergeOverrides(raw: unknown, current: WeightOverride[]): WeightOverride[] {
  return raw === undefined ? current : normalizeOverrides(raw)
}

/**
 * The weight the owner set for this exercise on `today` (the app's local YYYY-MM-DD, the same key the day
 * plan uses). Names match like baselines (case, spaces, ё/е). Several for the same day: the last one wins.
 */
export function findOverride(overrides: readonly WeightOverride[], name: string, today: string): WeightOverride | null {
  const key = normName(name)
  let hit: WeightOverride | null = null
  for (const o of overrides) {
    if (o.date === today && normName(o.exercise) === key) hit = o
  }
  return hit
}

/** Reps the working weight must allow: the first drop for dropsets, else the top of the range. */
function targetReps(p: Prescription): number | null {
  return p.drop_reps?.[0] ?? p.reps_max ?? p.reps_min ?? null
}

/** A suggestion with the per-hand flag and empty transfer fields; `perHand` is set from the name. */
function make(name: string, weight: number | null, reason: string, source: Source, extra: Partial<Suggestion> = {}): Suggestion {
  return { weight, reason, source, perHand: perHand(name), hintKg: null, related: null, ...extra }
}

/**
 * Starting weight from the owner's words, the same share of 1RM as the record branch. Known reps: Epley
 * 1RM (reps 1 = the max itself). Unknown reps ("~100 смогу"): the stated weight counts as the max.
 */
function fromBaseline(b: Baseline, exercise: ProgramExercise, step: number): Suggestion | null {
  const reps = targetReps(exercise.prescription)
  const pct = percentOf1rm(reps, exercise.intensity)
  const max = b.reps == null ? b.weightKg : e1rm(b.weightKg, b.reps)
  const weight = roundToStep(max * pct, step)
  if (weight <= 0) return null
  const share = `${Math.round(pct * 100)} % для ${repsWord(reps ?? 10)}`
  const kg = formatKg(b.weightKg)
  let reason: string
  if (b.reps == null) reason = `по твоим словам ~${kg} кг (как максимум), ${share}`
  else if (b.reps === 1) reason = `от твоего максимума ${kg} кг, ${share}`
  else reason = `по твоим словам ${kg} × ${b.reps} (1ПМ ≈ ${formatKg(Math.round(max))} кг), ${share}`
  return make(exercise.name, weight, reason, 'baseline')
}

/** True when the history has a completed weighted set of the exercise: baselines no longer count. */
export function hasHistory(history: Workout[], name: string): boolean {
  return lastSameSession(history, name) != null || bestE1rm(history, name) != null
}

/** Own history: the higher of double progression and the record's share; null when neither gives a weight. */
function fromHistory(history: Workout[], exercise: ProgramExercise, step: number): Suggestion | null {
  const { name, prescription: p, intensity } = exercise
  const last = lastSameSession(history, name)
  const record = bestE1rm(history, name)
  const progressed = doubleProgression(last, p, step)
  let best: Suggestion | null = null
  if (progressed != null) {
    const top = Math.max(...last!.map((s) => s.weight!))
    const target = progressionReps(p)
    const reason =
      progressed > top
        ? `прошлый раз ${formatKg(top)} × ${target} во всех подходах, +${formatKg(step)} кг`
        : `как в прошлый раз ${formatKg(top)} кг`
    best = make(name, progressed, reason, 'history')
  }
  if (record) {
    // Dropsets have no rep range: the first drop is what the weight must allow.
    const reps = targetReps(p)
    const pct = percentOf1rm(reps, intensity)
    const fromRecord = roundToStep(record.e1rm * pct, step)
    if (fromRecord > 0 && (!best || best.weight == null || fromRecord > best.weight)) {
      const reason = `от рекорда ${formatKg(Math.round(record.e1rm))} кг (1ПМ), ${Math.round(pct * 100)} % для ${repsWord(reps ?? 10)}`
      best = make(name, fromRecord, reason, 'history')
    }
  }
  return best
}

/**
 * Weight for today's plan, in priority order: the owner's override for `today` (as is), else the max of
 * the record-based weight and double progression, else the baseline (only while the history has nothing
 * for the exercise), else a related exercise of the history (`related`: a converted weight, a hint
 * without a number, or "none"). Never null: "hint" and "none" have `weight` null and a reason to show.
 * The reason is in Russian. Without `today` overrides are not looked at. The day plan's factor is not
 * applied here (plan.ts suggestFor).
 */
export function suggestWeight(
  history: Workout[],
  exercise: ProgramExercise,
  baselines: readonly Baseline[] = [],
  overrides: readonly WeightOverride[] = [],
  today?: string,
): Suggestion {
  const { name } = exercise
  const o = today ? findOverride(overrides, name, today) : null
  if (o) return make(name, o.weightKg, overrideReason(o.weightKg), 'override', { override: o })
  const step = equipmentStep(name)
  let s: Suggestion | null
  if (!hasHistory(history, name)) {
    const b = findBaseline(baselines, name)
    s = b ? fromBaseline(b, exercise, step) : null
  } else s = fromHistory(history, exercise, step)
  return s ?? related(history, exercise)
}

// ---- names: equipment, movement, grip (next_weights.py, same patterns) ----

export type Equipment = 'smith' | 'ez' | 'cable' | 'machine' | 'dumbbell' | 'barbell' | 'bodyweight'
export type Movement =
  | 'rear_delt'
  | 'lateral_raise'
  | 'triceps_extension'
  | 'curl'
  | 'bench'
  | 'overhead_press'
  | 'row'
  | 'pulldown'
  | 'squat'
  | 'hinge'
export type Grip = 'pronated' | 'neutral' | 'supinated'

// Python's \w matches Cyrillic, JS's does not even with the u flag: W is the Unicode word character.
// No lookbehind (older iOS WebViews reject it): a word start is (?:^|[^а-яa-z]), the same for a test.
const W = String.raw`[\p{L}\p{N}_]`
const L = '(?:^|[^а-яa-z])'
const rx = (pattern: string) => new RegExp(pattern, 'u')

// Checked in order: "смит" before the bar words, EZ before "гриф", blocks before machines.
const EQUIPMENT_RX: [Equipment, RegExp][] = [
  ['smith', rx('смит')],
  ['ez', rx(`${L}ez(?![a-z])|изогнут`)],
  ['cable', rx('блок|кроссовер|канат')],
  ['machine', rx(String.raw`тренаж|пек[\s-]?дек|хаммер|${L}гакк?(?![а-я])|машин`)],
  ['dumbbell', rx('гантел')],
  ['barbell', rx('штанг|гриф')],
  ['bodyweight', rx(`подтягиван|отжиман|брусь|${L}в висе|без веса|собственн`)],
]
// Leg curls, leg extensions, leg press: no movement (never a biceps curl or a bench).
const LEGS_RX = rx(String.raw`(?:сгибан|разгибан|жим)${W}*\s+(?:${W}+\s+)?ног`)
// Checked in order: the French press before the bench, the rear delt before the lateral raise.
const MOVEMENT_RX: [Movement, RegExp][] = [
  [
    'rear_delt',
    rx(
      String.raw`задн${W}*\s+дельт|пек[\s-]?дек${W}*\s+на\s+задн|обратн${W}*\s+(?:развед|бабочк)|` +
        String.raw`развед${W}*\s+(?:${W}+\s+)?в\s+наклон`,
    ),
  ],
  ['lateral_raise', rx(`отведен|${L}мах(?:и|ов)(?![а-я])`)],
  ['triceps_extension', rx('француз|разгибан|трицепс')], // not a press "на трицепс": see movement()
  ['curl', rx('сгибан|бицепс|молот')],
  [
    'bench',
    rx(String.raw`жим${W}*\s+(?:${W}+\s+){0,2}?леж|бенч|жим${W}*\s+(?:${W}+\s+){0,3}?(?:наклонн|под\s+углом)`),
  ],
  ['overhead_press', rx(String.raw`жим${W}*\s+(?:${W}+\s+){0,3}?(?:сидя|стоя|над\s+голов)|армейск`)],
  ['row', rx(String.raw`тяг${W}*\s+(?:${W}+\s+){0,3}?(?:горизонт|к\s+поясу|в\s+наклон|нижн)`)],
  ['pulldown', rx(String.raw`тяг${W}*\s+(?:${W}+\s+){0,3}?(?:вертикал|верхн)|подтягиван`)],
  ['squat', rx('присед')],
  ['hinge', rx('румын|станов')],
]
const PRESS_RX = rx(`${L}жим`)
/** Variant words: two names transfer only with the same set (curls ignore seated / standing). */
export type Modifier =
  | 'incline'
  | 'decline'
  | 'front'
  | 'seated'
  | 'standing'
  | 'close'
  | 'wide'
  | 'sumo'
  | 'behind'
  | 'overhead'
  | 'lying'
  | 'scott'
  | 'arnold'
  | 'hack'
  | 'rdl'
  | 'deadlift'
  | 'deficit'
  | 'pause'
const MODIFIER_RX: [Modifier, RegExp][] = [
  ['incline', rx(String.raw`наклонн|под\s+углом|накл${W}*\s+скам`)],
  ['decline', rx(String.raw`головой\s+вниз|обратн${W}*\s+наклон|отрицательн${W}*\s+наклон`)],
  ['front', rx('фронтал')],
  ['seated', rx('сидя')],
  ['standing', rx('стоя|армейск')],
  ['close', rx('узк')],
  ['wide', rx('широк')],
  ['sumo', rx('сумо')],
  ['behind', rx(String.raw`из[\s-]за\s+голов|за\s+голов`)],
  ['overhead', rx(String.raw`над\s+голов`)],
  ['lying', rx('леж')],
  ['scott', rx('скотт|парт')],
  ['arnold', rx('арнольд')],
  ['hack', rx(`${L}(?:гакк?|хакк?)`)],
  ['rdl', rx('румын')],
  ['deadlift', rx('станов')],
  ['deficit', rx('дефицит')],
  ['pause', rx('пауз')],
]
const CURL_IGNORES: readonly Modifier[] = ['seated', 'standing']
// One arm / one leg, or one dumbbell held in both hands: no number from (or to) anything else.
const UNILATERAL_RX = rx(String.raw`болгарск|выпад|гоблет|одной\s+рук|одной\s+ног|одноруч|концентрир|сплит|пистолет|на\s+одну\s+`)
const SINGLE_DUMBBELL_RX = rx(String.raw`гоблет|одной\s+гантел|одну\s+гантел|гантелью`)
const PRONATED_RX = rx(String.raw`пронац|хват${W}*\s+сверху|обратн${W}*\s+хват`)
const NEUTRAL_RX = rx('молот|нейтрал')

/** Equipment from the name; null when the name does not say (e.g. «жим лёжа»). */
export function equipment(name: string): Equipment | null {
  const key = normName(name)
  return EQUIPMENT_RX.find(([, r]) => r.test(key))?.[0] ?? null
}

export function movement(name: string): Movement | null {
  const key = normName(name)
  if (LEGS_RX.test(key)) return null
  // "жим узким хватом на трицепс" is a press, not an extension; the French press is.
  const press = PRESS_RX.test(key) && !key.includes('француз')
  return MOVEMENT_RX.find(([m, r]) => r.test(key) && !(press && m === 'triceps_extension'))?.[0] ?? null
}

/** Variant words of the name, sorted (see MODIFIER_RX); curls ignore seated / standing. */
export function modifiers(name: string): Modifier[] {
  const key = normName(name)
  const curl = movement(name) === 'curl'
  return MODIFIER_RX.filter(([m, r]) => r.test(key) && !(curl && CURL_IGNORES.includes(m))).map(([m]) => m)
}

/** One arm or one leg (болгарские, выпады, гоблет, одной рукой, концентрированные): never a transferred number. */
export function unilateral(name: string): boolean {
  return UNILATERAL_RX.test(normName(name))
}

/** Curls only: pronated / neutral / supinated (the default curl); null for other movements. */
export function grip(name: string): Grip | null {
  if (movement(name) !== 'curl') return null
  const key = normName(name)
  if (PRONATED_RX.test(key)) return 'pronated'
  if (NEUTRAL_RX.test(key)) return 'neutral'
  return 'supinated'
}

/** Dumbbells are logged per hand; one dumbbell in both hands (гоблет, «гантелью») is not «на руку». */
export function perHand(name: string): boolean {
  return equipment(name) === 'dumbbell' && !SINGLE_DUMBBELL_RX.test(normName(name))
}

// ---- related exercises ----

/**
 * Free-weight 1RM transfer, "from>to" -> factor; the source 1RM is per hand for dumbbells. See
 * next_weights.py EQ_FACTORS for the reasoning: dumbbell -> bar/EZ 2 × 0.85, -> Smith 2 × 0.9, the reverse
 * 0.85 / 2 and 0.8 / 2, barbell <-> EZ 1, bar <-> Smith 0.9.
 */
const EQ_FACTORS: Record<string, number> = {
  'dumbbell>barbell': 1.7,
  'dumbbell>ez': 1.7,
  'dumbbell>smith': 1.8,
  'barbell>dumbbell': 0.425,
  'ez>dumbbell': 0.425,
  'smith>dumbbell': 0.4,
  'barbell>ez': 1.0,
  'ez>barbell': 1.0,
  'barbell>smith': 0.9,
  'ez>smith': 0.9,
  'smith>barbell': 0.9,
  'smith>ez': 0.9,
}
const EQ_FACTOR_TEXT: Record<string, string> = {
  '1.7': '× 2 × 0,85',
  '1.8': '× 2 × 0,9',
  '0.425': '÷ 2 × 0,85',
  '0.4': '÷ 2 × 0,8',
  '0.9': '× 0,9',
}
const HINT_EQUIPMENT: readonly (Equipment | null)[] = ['cable', 'machine']
/** Movement -> equipment between which numbers transfer; other movements never get a number. */
const TRANSFER_EQUIPMENT: Partial<Record<Movement, readonly Equipment[]>> = {
  curl: ['dumbbell', 'barbell', 'ez'],
  overhead_press: ['dumbbell', 'barbell', 'smith'],
  bench: ['dumbbell', 'barbell', 'smith'],
  lateral_raise: ['dumbbell'],
}
/** Reverse (pronated) curls are 60-70 % of supinated ones: the middle. */
const REVERSE_GRIP = 0.65
/** A transferred 1RM is an estimate: never the "heavy" share of it. */
const TRANSFER_INTENSITY: Intensity = 'medium'
const EQUIPMENT_NAMES: Record<Equipment, string> = {
  smith: 'смит',
  ez: 'EZ-гриф',
  cable: 'блок',
  machine: 'тренажёр',
  dumbbell: 'гантели',
  barbell: 'штанга',
  bodyweight: 'свой вес',
}

/** Compares like Python's tuple ordering: false < true, numbers and strings ascending (code units). */
function cmpKey(a: readonly (boolean | number | string)[], b: readonly (boolean | number | string)[]): number {
  for (let i = 0; i < a.length; i++) {
    const x = typeof a[i] === 'boolean' ? Number(a[i]) : a[i]
    const y = typeof b[i] === 'boolean' ? Number(b[i]) : b[i]
    if (x < y) return -1
    if (x > y) return 1
  }
  return 0
}

/** Exercise names in the history, most recently done first (ties by name). */
function historyNames(history: Workout[]): string[] {
  const seen = new Map<string, number>()
  history.forEach((w, i) => w.exercises.forEach((e) => seen.set(e.name, i)))
  return [...seen.keys()].sort((a, b) => cmpKey([-seen.get(a)!, a], [-seen.get(b)!, b]))
}

/** Reason numbers with one decimal: 25.333 -> '25,3', 43.0 -> '43'. */
function num1(n: number): string {
  return formatKg(Math.round(n * 10) / 10)
}

/** Content words of a name (3+ letters), cut to 6 letters: rough stems. */
function nameWords(name: string): string[] {
  return (normName(name).match(/[а-яa-z0-9]+/g) ?? []).filter((w) => w.length >= 3).map((w) => w.slice(0, 6))
}

/** Words of `a` also in `b` (equal, or one a prefix of the other when both have 4+ letters). */
function sharedWords(a: string, b: string): number {
  const wb = nameWords(b)
  return nameWords(a).filter((w) =>
    wb.some((x) => w === x || (Math.min(w.length, x.length) >= 4 && (w.startsWith(x) || x.startsWith(w)))),
  ).length
}

const NO_NUMBER = 'истории нет: вес по ощущениям, 2 повтора в запасе'

/**
 * The transfer from a related exercise, for an exercise without its own history and baseline (one hop,
 * real history only): "related" with a weight, "hint" with a starting point, or "none" with a reason.
 * The same checks, in the same order, as next_weights.py `related`:
 * - Same movement (from the name). Unilateral or single-dumbbell on either side: no number, no hint.
 * - Cable, machine or unknown equipment on either side: between two cables (or two machines) a hint, the
 *   first set of the related exercise's last session; otherwise no number.
 * - Different variant words (`modifiers`): no number.
 * - Whitelisted pairs only (TRANSFER_EQUIPMENT): the related exercise's best Epley 1RM × EQ_FACTORS; a
 *   pronated curl from a non-pronated one × REVERSE_GRIP; the share never "heavy".
 * - Candidates: same grip > same equipment > the most shared name words > the most recent > the name.
 */
export function related(history: Workout[], exercise: ProgramExercise): Suggestion {
  const { name } = exercise
  const mv = movement(name)
  const eqT = equipment(name)
  const gripT = grip(name)
  if (mv == null) return make(name, null, NO_NUMBER, 'none')
  type Key = (boolean | number | string)[]
  type Num = { key: Key; src: string; factor: number; rec: Record1rm; g: number }
  type Hint = { key: Key; src: string; first: number }
  const numbers: Num[] = []
  const hints: Hint[] = []
  const others: { src: string; why: string }[] = []
  const order = historyNames(history)
  const self = normName(name)
  const modsT = modifiers(name).join(' ')
  const oneSideT = unilateral(name)
  const allowed = TRANSFER_EQUIPMENT[mv] ?? []
  const what = (eq: Equipment | null) => (eq ? EQUIPMENT_NAMES[eq] : 'другой снаряд')
  for (const src of order) {
    if (normName(src) === self || movement(src) !== mv) continue
    const rec = bestE1rm(history, src)
    if (!rec) continue
    const eqS = equipment(src)
    const gripS = grip(src)
    const key: Key = [gripT !== gripS, eqT !== eqS, -sharedWords(name, src), order.indexOf(src), src]
    if (oneSideT || unilateral(src)) {
      others.push({ src, why: 'упражнение на одну руку или ногу' })
    } else if (HINT_EQUIPMENT.includes(eqT) || HINT_EQUIPMENT.includes(eqS) || eqT == null || eqS == null) {
      // Cables and machines: a starting point from the same kind of equipment (any variant), no number.
      const first = eqT === eqS && HINT_EQUIPMENT.includes(eqT) ? lastSameSession(history, src)?.[0]?.weight : null
      if (first) hints.push({ key, src, first })
      else others.push({ src, why: what(eqS) })
    } else if (modifiers(src).join(' ') !== modsT) {
      others.push({ src, why: 'другой вариант упражнения' })
    } else if (allowed.includes(eqT) && allowed.includes(eqS) && (eqT === eqS || `${eqS}>${eqT}` in EQ_FACTORS)) {
      const factor = eqT === eqS ? 1 : EQ_FACTORS[`${eqS}>${eqT}`]
      const g = gripT === 'pronated' && gripS !== 'pronated' ? REVERSE_GRIP : 1
      numbers.push({ key, src, factor, rec, g })
    } else if (eqT === eqS) {
      others.push({ src, why: 'другое упражнение' })
    } else {
      others.push({ src, why: what(eqS) })
    }
  }
  if (numbers.length) {
    const { src, factor, rec, g } = numbers.reduce((a, b) => (cmpKey(b.key, a.key) < 0 ? b : a))
    const reps = progressionReps(exercise.prescription)
    const intensity = exercise.intensity == null || exercise.intensity === 'heavy' ? TRANSFER_INTENSITY : exercise.intensity
    const pct = percentOf1rm(reps, intensity)
    const converted = rec.e1rm * factor * g
    const weight = roundToStep(converted * pct, equipmentStep(name))
    if (weight > 0) {
      const steps = [EQ_FACTOR_TEXT[String(factor)] ?? '', g !== 1 ? '× 0,65 (обратный хват слабее)' : '']
        .filter(Boolean)
        .join(' ')
      const calc = `1ПМ ≈ ${num1(rec.e1rm)}${steps ? ` ${steps} ≈ ${num1(converted)} кг` : ' кг'}`
      const hand = perHand(src) ? ' на руку' : ''
      const reason =
        `по «${src}» ${formatKg(rec.weight)}×${rec.reps}${hand}: ${calc}, ` +
        `${Math.round(pct * 100)} % для ${repsWord(reps ?? 10)}`
      return make(name, weight, reason, 'related', { related: src, relatedSet: { weight: rec.weight, reps: rec.reps } })
    }
  }
  if (hints.length) {
    const { src, first } = hints.reduce((a, b) => (cmpKey(b.key, a.key) < 0 ? b : a))
    const reason =
      `подбери по ощущениям: начни с ~${formatKg(first)} кг, как первый подход в «${src}» ` +
      `(${EQUIPMENT_NAMES[eqT!]}: числа разных тренажёров не сравнить)`
    return make(name, null, reason, 'hint', { hintKg: first, related: src })
  }
  if (others.length) {
    const { src, why } = others[0]
    const reason =
      `подбери по ощущениям: вес из «${src}» (${why}) сюда не переносится; ` +
      'начни легко, рабочий вес — с 2 повторами в запасе'
    return make(name, null, reason, 'none', { related: src })
  }
  return make(name, null, NO_NUMBER, 'none')
}

/**
 * The reason for the Today screen, short enough for a 360 px card: the related exercise without the
 * arithmetic, the hint as «подбери по ощущениям, начни с ~X кг», history without the step (the weight shows it).
 */
export function shortReason(s: Suggestion): string {
  const hand = (name: string) => (perHand(name) ? ' на руку' : '')
  switch (s.source) {
    case 'history':
      return s.reason.replace(/, \+[\d,]+ кг$/, '')
    case 'related':
      return s.related && s.relatedSet
        ? `по «${s.related}» ${formatKg(s.relatedSet.weight)}×${s.relatedSet.reps}${hand(s.related)}`
        : s.reason
    case 'hint':
      return s.hintKg != null
        ? `подбери по ощущениям, начни с ~${formatKg(s.hintKg)} кг${s.related ? ` (как в «${s.related}»)` : ''}`
        : s.reason
    case 'none':
      return s.related ? `подбери по ощущениям: вес из «${s.related}» сюда не переносится` : 'истории нет: вес по ощущениям'
    default:
      return s.reason
  }
}
