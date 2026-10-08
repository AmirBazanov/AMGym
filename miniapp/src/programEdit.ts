// Pure parts of the day editor (PATCH /api/programs/{slug}, spec 2026-10-08-program-editor-design.md, phase 2):
// the draft (the day as it should become), draft -> ops, the scope -> weeks, client-side checks with the
// server's bounds, a local application of ops (preview, rebase after 409) and the response outcome.
// Node-safe: type-only imports from api/store (api.ts reads window); store.ts wires it to the request.
import type { ItemRef, OpPrescription, OpResult, ProgramOp, ProgramOut } from './api'
import type { Intensity, Prescription, Program, ProgramDay, ProgramExercise } from './program'
import type { Workout } from './store'

/** The server's bounds (spec «Проверки»); the editor checks them before a request. */
export const LIMITS = {
  setsMin: 1,
  setsMax: 20,
  repsMin: 1,
  repsMax: 100,
  dropMin: 2,
  dropMax: 5,
  nameMax: 200,
  dayMax: 20,
  opsMax: 50,
} as const

/** Sets, reps or drops and intensity of one exercise, as the editor holds them. */
export interface Rx {
  sets: number
  repsMin: number | null // null only for a dropset
  repsMax: number | null // null: exactly repsMin (or a dropset)
  dropReps: number[] | null // a dropset: 2..5 reps
  intensity: Intensity | null
}

/** ProgramItem.id of an exercise already in the day, or a tempId of one added in the draft. */
export type ItemKey = ItemRef

/** One exercise of the day as it should become. */
export interface DraftItem {
  key: ItemKey
  name: string
  rx: Rx
}

/** The day as it should become, in its order; a missing ProgramItem.id means removed. */
export type Draft = DraftItem[]

export const DEFAULT_RX: Rx = { sets: 3, repsMin: 10, repsMax: 12, dropReps: null, intensity: null }

// ---------- names and prescriptions ----------

/** Comparison key, as the server's programs.normalize: case, ё/е and extra spaces are ignored. */
export function normalizeName(name: string): string {
  return name.trim().toLowerCase().replace(/ё/g, 'е').split(/\s+/).filter(Boolean).join(' ')
}

/** The name as sent (and as the server stores a new exercise): spaces collapsed, lower case. */
export function cleanName(name: string): string {
  return name.trim().toLowerCase().split(/\s+/).filter(Boolean).join(' ')
}

export function isTempKey(key: ItemKey): key is string {
  return typeof key === 'string'
}

export function rxOf(e: ProgramExercise): Rx {
  const p = e.prescription
  const drop = p.drop_reps?.length ? [...p.drop_reps] : null
  return {
    sets: p.sets,
    repsMin: drop ? null : p.reps_min,
    repsMax: drop ? null : (p.reps_max ?? p.reps_min),
    dropReps: drop,
    intensity: e.intensity ?? null,
  }
}

/** Rx as sent: a dropset has no reps; otherwise repsMax defaults to repsMin (the server stores it so). */
export function opPrescription(rx: Rx): OpPrescription {
  if (rx.dropReps?.length) return { sets: rx.sets, repsMin: null, repsMax: null, dropReps: [...rx.dropReps], intensity: rx.intensity }
  return { sets: rx.sets, repsMin: rx.repsMin, repsMax: rx.repsMax ?? rx.repsMin, dropReps: null, intensity: rx.intensity }
}

export function sameRx(a: Rx, b: Rx): boolean {
  const x = opPrescription(a)
  const y = opPrescription(b)
  return (
    x.sets === y.sets &&
    x.repsMin === y.repsMin &&
    x.repsMax === y.repsMax &&
    x.intensity === y.intensity &&
    (x.dropReps ?? []).join('-') === (y.dropReps ?? []).join('-')
  )
}

/** The server's raw_prescription: "6х8-12", "4х10", "дропсет 3х 12-6-6" (Cyrillic х). */
export function rawOf(rx: Rx): string {
  const p = opPrescription(rx)
  if (p.dropReps?.length) return `дропсет ${p.sets}х ${p.dropReps.join('-')}`
  if (p.repsMin == null) return `${p.sets}х`
  if (p.repsMax == null || p.repsMax === p.repsMin) return `${p.sets}х${p.repsMin}`
  return `${p.sets}х${p.repsMin}-${p.repsMax}`
}

export function prescriptionOf(rx: Rx): Prescription {
  const p = opPrescription(rx)
  return { sets: p.sets, reps_min: p.repsMin, reps_max: p.repsMax, drop_reps: p.dropReps, raw: rawOf(rx) }
}

/** Default drops when the dropset switch is turned on: from the top reps, halving (12 -> 12-6-6). */
export function defaultDrops(rx: Rx): number[] {
  const top = rx.repsMax ?? rx.repsMin ?? 12
  const half = Math.max(1, Math.ceil(top / 2))
  return [top, half, half]
}

// ---------- the draft ----------

/** The day as it is: the starting draft. Exercises without a server id (bundled program) are not editable. */
export function draftFromDay(day: ProgramDay): Draft {
  return day.exercises.filter((e) => e.id != null).map((e) => ({ key: e.id!, name: e.name, rx: rxOf(e) }))
}

/** A tempId not used in the draft: "new1", "new2"… */
export function newTempId(draft: Draft): string {
  const used = new Set(draft.map((i) => i.key))
  let n = 1
  while (used.has(`new${n}`)) n++
  return `new${n}`
}

export function addItem(draft: Draft, name: string, rx: Rx = DEFAULT_RX): { draft: Draft; key: string } {
  const key = newTempId(draft)
  return { draft: [...draft, { key, name: cleanName(name), rx: { ...rx } }], key }
}

export function removeItem(draft: Draft, key: ItemKey): Draft {
  return draft.filter((i) => i.key !== key)
}

export function replaceItem(draft: Draft, key: ItemKey, name: string): Draft {
  return draft.map((i) => (i.key === key ? { ...i, name: cleanName(name) } : i))
}

export function setRx(draft: Draft, key: ItemKey, rx: Rx): Draft {
  return draft.map((i) => (i.key === key ? { ...i, rx: { ...rx } } : i))
}

/** Moves the item at `index` one place up (-1) or down (+1); out of range: unchanged. */
export function moveItem(draft: Draft, index: number, dir: -1 | 1): Draft {
  const to = index + dir
  if (index < 0 || index >= draft.length || to < 0 || to >= draft.length) return draft
  const next = [...draft]
  ;[next[index], next[to]] = [next[to], next[index]]
  return next
}

/** What changed for an item compared to the day: shown as marks in the editor. */
export function itemChange(base: ProgramDay, item: DraftItem): { added: boolean; replaced: boolean; prescribed: boolean } {
  if (isTempKey(item.key)) return { added: true, replaced: false, prescribed: false }
  const e = base.exercises.find((x) => x.id === item.key)
  if (!e) return { added: false, replaced: false, prescribed: false }
  return {
    added: false,
    replaced: normalizeName(e.name) !== normalizeName(item.name),
    prescribed: !sameRx(rxOf(e), item.rx),
  }
}

// ---------- checks (the server repeats them; spec «Проверки») ----------

export interface RxErrors {
  sets?: string
  reps?: string
  drop?: string
}

const inRange = (n: number | null | undefined, lo: number, hi: number) => n != null && Number.isInteger(n) && n >= lo && n <= hi

export function validateRx(rx: Rx): RxErrors {
  const err: RxErrors = {}
  if (!inRange(rx.sets, LIMITS.setsMin, LIMITS.setsMax)) err.sets = `Подходов от ${LIMITS.setsMin} до ${LIMITS.setsMax}`
  if (rx.dropReps) {
    if (rx.dropReps.length < LIMITS.dropMin || rx.dropReps.length > LIMITS.dropMax)
      err.drop = `В дропсете от ${LIMITS.dropMin} до ${LIMITS.dropMax} ступеней`
    else if (!rx.dropReps.every((r) => inRange(r, LIMITS.repsMin, LIMITS.repsMax)))
      err.drop = `Повторы от ${LIMITS.repsMin} до ${LIMITS.repsMax}`
  } else {
    const max = rx.repsMax ?? rx.repsMin
    if (!inRange(rx.repsMin, LIMITS.repsMin, LIMITS.repsMax) || !inRange(max, LIMITS.repsMin, LIMITS.repsMax))
      err.reps = `Повторы от ${LIMITS.repsMin} до ${LIMITS.repsMax}`
    else if (max! < rx.repsMin!) err.reps = '«От» больше, чем «до»'
  }
  return err
}

export function rxValid(rx: Rx): boolean {
  return Object.keys(validateRx(rx)).length === 0
}

export function validateName(name: string): string | null {
  const n = cleanName(name)
  if (!n) return 'Пустое название'
  if (n.length > LIMITS.nameMax) return `Название длиннее ${LIMITS.nameMax} символов`
  return null
}

export interface DraftCheck {
  ok: boolean
  day: string | null // about the whole day
  items: Record<string, string> // by String(key): the first problem of an item
}

/**
 * The draft against the server's rules. Only new and changed prescriptions are checked: an untouched
 * exercise is not sent, whatever the program holds.
 */
export function validateDraft(base: ProgramDay, draft: Draft): DraftCheck {
  const items: Record<string, string> = {}
  let day: string | null = null
  if (!draft.length) day = 'В дне должно остаться хотя бы одно упражнение'
  else if (draft.length > LIMITS.dayMax) day = `В дне не больше ${LIMITS.dayMax} упражнений`
  const seen = new Map<string, ItemKey>()
  for (const item of draft) {
    const k = String(item.key)
    const ch = itemChange(base, item)
    const nameErr = ch.added || ch.replaced ? validateName(item.name) : null
    if (nameErr) items[k] = nameErr
    else if (ch.added || ch.prescribed) {
      const e = validateRx(item.rx)
      const first = e.sets ?? e.reps ?? e.drop
      if (first) items[k] = first
    }
    const norm = normalizeName(item.name)
    if (seen.has(norm) && !items[k]) items[k] = 'Это упражнение уже есть в дне'
    seen.set(norm, item.key)
  }
  return { ok: !day && Object.keys(items).length === 0, day, items }
}

// ---------- draft -> ops ----------

/** Weeks of the two kinds of edits (absent: only the edited week). */
export interface ScopeWeeks {
  structure?: number[] // replace, add, remove, reorder
  prescribe?: number[] // sets, reps, drops, intensity
}

export type OpKind = 'structure' | 'prescribe'

export function opKind(op: ProgramOp): OpKind {
  return op.op === 'prescribe' ? 'prescribe' : 'structure'
}

/** `weeks` as sent: the edited week always in, sorted; undefined when it is the only one. */
export function weeksParam(weeks: readonly number[] | undefined, week: number): number[] | undefined {
  if (!weeks) return undefined
  const all = [...new Set([week, ...weeks])].sort((a, b) => a - b)
  return all.length > 1 ? all : undefined
}

/** The day's order after removes, then adds at their positions (1-based, clamped), as the server does it. */
function simulatedOrder(base: ProgramDay, draft: Draft): ItemKey[] {
  const kept = new Set(draft.map((i) => i.key))
  const cur: ItemKey[] = base.exercises.filter((e) => e.id != null && kept.has(e.id)).map((e) => e.id!)
  draft.forEach((item, i) => {
    if (isTempKey(item.key)) cur.splice(Math.min(i, cur.length), 0, item.key)
  })
  return cur
}

/**
 * The ops turning `base` (day `weekday` of `week`) into `draft`, folded: one replace and one prescribe
 * per exercise at most, an added one carries its prescription, add-then-remove is nothing. Order:
 * removes (free names first), replaces, prescribes, adds by final position, then one reorder if the
 * adds and removes alone do not give the draft's order.
 */
export function draftOps(week: number, weekday: number, base: ProgramDay, draft: Draft, scope: ScopeWeeks = {}): ProgramOp[] {
  const s = weeksParam(scope.structure, week)
  const p = weeksParam(scope.prescribe, week)
  const w = <T extends object>(o: T, weeks: number[] | undefined): T => (weeks ? { ...o, weeks } : o)
  const at = { week, weekday }
  const byId = new Map(base.exercises.filter((e) => e.id != null).map((e) => [e.id!, e]))
  const inDraft = new Set(draft.map((i) => i.key))
  const ops: ProgramOp[] = []
  for (const e of base.exercises) if (e.id != null && !inDraft.has(e.id)) ops.push(w({ op: 'remove', ...at, itemId: e.id }, s))
  for (const item of draft) {
    const e = isTempKey(item.key) ? undefined : byId.get(item.key)
    if (e && normalizeName(e.name) !== normalizeName(item.name))
      ops.push(w({ op: 'replace', ...at, itemId: e.id!, name: cleanName(item.name) }, s))
  }
  for (const item of draft) {
    const e = isTempKey(item.key) ? undefined : byId.get(item.key)
    if (e && !sameRx(rxOf(e), item.rx)) ops.push(w({ op: 'prescribe', ...at, itemId: e.id!, ...opPrescription(item.rx) }, p))
  }
  draft.forEach((item, i) => {
    if (isTempKey(item.key))
      ops.push(w({ op: 'add', ...at, tempId: item.key, name: cleanName(item.name), position: i + 1, ...opPrescription(item.rx) }, s))
  })
  // Only exercises the server knows (an id of the day or a tempId above).
  const keys = draft.filter((i) => isTempKey(i.key) || byId.has(i.key)).map((i) => i.key)
  const sim = simulatedOrder(base, draft)
  if (sim.length !== keys.length || sim.some((k, i) => k !== keys[i])) ops.push(w({ op: 'reorder', ...at, itemIds: keys }, s))
  return ops
}

/** Which kinds of edits the ops hold: the scope sheet asks about those only. */
export function opKinds(ops: readonly ProgramOp[]): Record<OpKind, boolean> {
  return { structure: ops.some((o) => opKind(o) === 'structure'), prescribe: ops.some((o) => opKind(o) === 'prescribe') }
}

// ---------- scope: which weeks ----------

export type ScopeChoice = 'week' | 'from' | 'all' | 'pick'

/** The weeks of a scope choice; the edited week is always in. */
export function scopeWeeks(choice: ScopeChoice, week: number, programWeeks: readonly number[], picked: readonly number[] = []): number[] {
  const all = [...programWeeks].sort((a, b) => a - b)
  switch (choice) {
    case 'week':
      return [week]
    case 'from':
      return all.filter((n) => n >= week)
    case 'all':
      return all
    case 'pick':
      return all.filter((n) => n === week || picked.includes(n))
  }
}

/**
 * Default scope per kind (the brief over spec §5): a draft whose exercise edits are replacements only goes
 * to all weeks (the server skips weeks without that exercise); added, removed or moved exercises and sets
 * and reps (the program's load waves) stay in this week unless the owner chooses more.
 */
export function defaultScope(ops: readonly ProgramOp[]): Record<OpKind, ScopeChoice> {
  const structure = ops.filter((o) => opKind(o) === 'structure')
  return { structure: structure.length && structure.every((o) => o.op === 'replace') ? 'all' : 'week', prescribe: 'week' }
}

// ---------- local application (preview, rebase after 409); mirrors the server's week matching ----------

export type ApplyResult = { ok: true; program: Program; results: OpResult[] } | { ok: false; op: number; error: string }

const SKIP = {
  noDay: 'нет этого дня',
  noItem: 'нет этого упражнения',
  exists: 'упражнение уже есть в дне',
  last: 'последнее упражнение дня',
  full: `в дне уже ${LIMITS.dayMax} упражнений`,
  otherSet: 'другой набор упражнений',
} as const

function cloneProgram(p: Program): Program {
  return {
    ...p,
    weeks: p.weeks.map((w) => ({
      ...w,
      days: w.days.map((d) => ({ ...d, exercises: d.exercises.map((e) => ({ ...e, prescription: { ...e.prescription } })) })),
    })),
  }
}

/** The weeks an op touches: its week and `weeks`, sorted. */
export function opWeeks(op: { week: number; weeks?: number[] }): number[] {
  return [...new Set([op.week, ...(op.weeks ?? [])])].sort((a, b) => a - b)
}

/**
 * Applies `ops` to a copy of `program` the way the server does: in order, each sees the previous; in
 * other weeks the same weekday and the exercise with the same name (the server: same exercise_id) as the
 * edited one before the op; no match is a skip, not an error. The edited week's failures are errors.
 * Names stay as typed (the server may map an alias to its canonical name).
 */
export function applyOps(program: Program, ops: readonly ProgramOp[]): ApplyResult {
  const prog = cloneProgram(program)
  const temp = new Map<string, ProgramExercise>()
  const results: OpResult[] = []
  const dayOf = (week: number, weekday: number): ProgramDay | undefined =>
    prog.weeks.find((x) => x.number === week)?.days.find((d) => d.weekday === weekday)
  const known = new Set(prog.weeks.map((x) => x.number))
  const resolve = (day: ProgramDay, ref: ItemRef) =>
    typeof ref === 'string' ? (temp.has(ref) && day.exercises.includes(temp.get(ref)!) ? temp.get(ref) : undefined) : day.exercises.find((e) => e.id === ref)
  const fail = (op: number, error: string): ApplyResult => ({ ok: false, op, error })
  if (ops.length > LIMITS.opsMax) return fail(LIMITS.opsMax, `Не больше ${LIMITS.opsMax} правок за раз`)

  for (let i = 0; i < ops.length; i++) {
    const op = ops[i]
    const weeks = opWeeks(op)
    const missing = weeks.find((n) => !known.has(n))
    if (missing != null) return fail(i, `Нет недели ${missing}`)
    const src = dayOf(op.week, op.weekday)
    if (!src) return fail(i, 'Нет такого дня')
    const res: OpResult = { op: i, weeks: [], skipped: [] }
    const skip = (week: number, reason: string) => res.skipped.push({ week, reason })

    if (op.op === 'add') {
      const name = cleanName(op.name)
      for (const n of weeks) {
        const day: ProgramDay | undefined = n === op.week ? src : dayOf(n, op.weekday)
        if (!day) {
          skip(n, SKIP.noDay)
          continue
        }
        const reason = day.exercises.some((e) => normalizeName(e.name) === normalizeName(name))
          ? SKIP.exists
          : day.exercises.length >= LIMITS.dayMax
            ? SKIP.full
            : null
        if (reason) {
          if (n === op.week) return fail(i, capitalizeFirst(reason))
          skip(n, reason)
          continue
        }
        const e: ProgramExercise = { name, intensity: op.intensity, order: 0, prescription: prescriptionOf(rxFromOp(op)) }
        day.exercises.splice(Math.max(0, Math.min(op.position - 1, day.exercises.length)), 0, e)
        renumber(day)
        if (n === op.week) temp.set(op.tempId, e)
        res.weeks.push(n)
      }
    } else if (op.op === 'reorder') {
      const items = op.itemIds.map((r) => resolve(src, r))
      if (items.some((e) => !e) || new Set(items).size !== items.length || items.length !== src.exercises.length)
        return fail(i, 'Порядок должен включать все упражнения дня по одному разу')
      const names = items.map((e) => normalizeName(e!.name))
      for (const n of weeks) {
        const day: ProgramDay | undefined = n === op.week ? src : dayOf(n, op.weekday)
        if (!day) {
          skip(n, SKIP.noDay)
          continue
        }
        if (day === src) day.exercises = items as ProgramExercise[]
        else {
          const by = new Map(day.exercises.map((e) => [normalizeName(e.name), e]))
          if (by.size !== names.length || !names.every((nm) => by.has(nm))) {
            skip(n, SKIP.otherSet)
            continue
          }
          day.exercises = names.map((nm) => by.get(nm)!)
        }
        renumber(day)
        res.weeks.push(n)
      }
    } else {
      const item = resolve(src, op.itemId)
      if (!item) return fail(i, 'Упражнения уже нет в дне')
      const ref = normalizeName(item.name)
      for (const n of weeks) {
        const day: ProgramDay | undefined = n === op.week ? src : dayOf(n, op.weekday)
        if (!day) {
          skip(n, SKIP.noDay)
          continue
        }
        const e = day === src ? item : day.exercises.find((x) => normalizeName(x.name) === ref)
        if (!e) {
          skip(n, SKIP.noItem)
          continue
        }
        if (op.op === 'replace') {
          const name = cleanName(op.name)
          if (day.exercises.some((x) => x !== e && normalizeName(x.name) === normalizeName(name))) {
            if (day === src) return fail(i, 'Упражнение уже есть в дне')
            skip(n, SKIP.exists)
            continue
          }
          e.name = name
        } else if (op.op === 'prescribe') {
          e.prescription = prescriptionOf(rxFromOp(op))
          e.intensity = op.intensity
        } else {
          if (day.exercises.length <= 1) {
            if (day === src) return fail(i, 'Нельзя убрать последнее упражнение дня')
            skip(n, SKIP.last)
            continue
          }
          day.exercises = day.exercises.filter((x) => x !== e)
          renumber(day)
        }
        res.weeks.push(n)
      }
    }
    results.push(res)
  }
  return { ok: true, program: prog, results }
}

function rxFromOp(op: OpPrescription): Rx {
  return { sets: op.sets, repsMin: op.repsMin, repsMax: op.repsMax, dropReps: op.dropReps, intensity: op.intensity }
}

/** Orders 1..n in place: the exercises are applyOps' own clones, and tempIds refer to these objects. */
function renumber(day: ProgramDay) {
  day.exercises.forEach((e, i) => {
    e.order = i + 1
  })
}

function capitalizeFirst(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/**
 * The draft carried over to a newer version of the day (409: the program changed elsewhere): what the
 * owner changed (the same ops, this week only) is applied to the new day. Null when it no longer fits
 * (an edited exercise is gone): the editor starts over from the new day.
 */
export function rebaseDraft(oldDay: ProgramDay, newDay: ProgramDay, draft: Draft, week: number): Draft | null {
  const ops = draftOps(week, newDay.weekday, oldDay, draft)
  if (!ops.length) return draftFromDay(newDay)
  const program: Program = { id: '', name: '', source: '', weeks: [{ number: week, days: [newDay] }] }
  const applied = applyOps(program, ops)
  if (!applied.ok) return null
  const day = applied.program.weeks[0].days[0]
  // Back to keys: server ids stay, added exercises get their tempIds again (by name, unique in a day).
  const added = new Map(draft.filter((i) => isTempKey(i.key)).map((i) => [normalizeName(i.name), i.key]))
  const out: Draft = []
  for (const e of day.exercises) {
    const key = e.id ?? added.get(normalizeName(e.name))
    if (key == null) return null
    out.push({ key, name: e.name, rx: rxOf(e) })
  }
  return out
}

// ---------- results for the scope sheet ----------

/** "1, 2, 6–8": runs of three and more as a range. */
export function formatWeeks(weeks: readonly number[]): string {
  const ns = [...new Set(weeks)].sort((a, b) => a - b)
  const parts: string[] = []
  for (let i = 0; i < ns.length; ) {
    let j = i
    while (j + 1 < ns.length && ns[j + 1] === ns[j] + 1) j++
    if (j - i >= 2) parts.push(`${ns[i]}–${ns[j]}`)
    else for (let k = i; k <= j; k++) parts.push(String(ns[k]))
    i = j + 1
  }
  return parts.join(', ')
}

export interface Summary {
  changed: number[] // weeks where at least one op landed
  skipped: { weeks: number[]; reason: string; what: string }[] // grouped by op and reason
}

/** What a save does across weeks, for «Изменится: недели 1, 2, 6–8. Пропущено: 3–5 — там другое упражнение». */
export function summarize(ops: readonly ProgramOp[], results: readonly OpResult[], nameOf: (op: ProgramOp) => string): Summary {
  const changed = new Set<number>()
  const groups = new Map<string, { weeks: number[]; reason: string; what: string }>()
  for (const r of results) {
    r.weeks.forEach((n) => changed.add(n))
    const op = ops[r.op]
    for (const s of r.skipped) {
      const what = op ? nameOf(op) : ''
      const k = `${what}\u0000${s.reason}`
      const g = groups.get(k) ?? { weeks: [], reason: s.reason, what }
      g.weeks.push(s.week)
      groups.set(k, g)
    }
  }
  return {
    changed: [...changed].sort((a, b) => a - b),
    skipped: [...groups.values()].map((g) => ({ ...g, weeks: [...new Set(g.weeks)].sort((a, b) => a - b) })),
  }
}

/** Short Russian label of an op for the preview («замена: жим лёжа → жим под 30°»). */
export function opLabel(op: ProgramOp, base: ProgramDay, draft: Draft): string {
  const nameOf = (ref: ItemRef) =>
    (typeof ref === 'string' ? draft.find((i) => i.key === ref)?.name : base.exercises.find((e) => e.id === ref)?.name) ?? ''
  switch (op.op) {
    case 'replace':
      return `замена ${nameOf(op.itemId)} → ${op.name}`
    case 'prescribe':
      return `подходы: ${nameOf(op.itemId)}`
    case 'add':
      return `добавить ${op.name}`
    case 'remove':
      return `убрать ${nameOf(op.itemId)}`
    case 'reorder':
      return 'порядок упражнений'
  }
}

// ---------- the server's answer ----------

export type EditOutcome =
  | { kind: 'saved'; program: ProgramOut; switchedFrom: string | null; results: OpResult[] }
  | { kind: 'conflict'; program: ProgramOut | null } // 409 version: the program changed elsewhere
  | { kind: 'not_active' } // 409: the program is not the active one any more
  | { kind: 'invalid'; message: string } // 422 with the server's reason
  | { kind: 'failed'; message: string } // offline, 404, 5xx, unexpected body

function isProgramOut(x: unknown): x is ProgramOut {
  const p = x as ProgramOut | null
  return !!p && typeof p === 'object' && typeof p.id === 'string' && Array.isArray(p.weeks)
}

/** The PATCH response classified; `body` is the parsed JSON (or null). */
export function editOutcome(status: number, body: unknown): EditOutcome {
  const b = (body ?? {}) as { detail?: unknown; program?: unknown; switchedFrom?: unknown; results?: unknown }
  if (status === 200 && isProgramOut(b.program))
    return {
      kind: 'saved',
      program: b.program,
      switchedFrom: typeof b.switchedFrom === 'string' ? b.switchedFrom : null,
      results: Array.isArray(b.results) ? (b.results as OpResult[]) : [],
    }
  if (status === 409 && b.detail === 'not_active') return { kind: 'not_active' }
  if (status === 409) return { kind: 'conflict', program: isProgramOut(b.program) ? b.program : null }
  if (status === 422) return { kind: 'invalid', message: detailText(b.detail) ?? 'Сервер не принял правку' }
  if (status === 404) return { kind: 'failed', message: 'Программа не найдена на сервере' }
  return { kind: 'failed', message: 'Не получилось сохранить. Правки остались, попробуй ещё раз' }
}

/** 422 detail: our string, or FastAPI's validation list (first message). */
function detailText(detail: unknown): string | null {
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail) && detail.length) {
    const msg = (detail[0] as { msg?: unknown }).msg
    if (typeof msg === 'string') return msg
  }
  return null
}

// ---------- a copy took over the template (switchedFrom) ----------

export interface RetargetInput {
  active: Workout | null
  pending: Workout[]
  history: Workout[]
  startDate: string // YYYY-MM-DD, the current run's start
}

/**
 * Workouts of the current run moved from the template `from` to its new copy `to`, as the server rebinds
 * them: the active one, the offline queue and the history since the run's start (so ✓ marks and
 * currentRun keep working until the next /api/state). Earlier runs stay on the template. Idempotent.
 */
export function retargetWorkouts<T extends RetargetInput>(s: T, from: string, to: string): T {
  if (from === to) return s
  const since = new Date(s.startDate + 'T00:00:00').getTime()
  const move = (w: Workout) => (w.programId === from ? { ...w, programId: to } : w)
  const inRun = (w: Workout) => new Date(w.startedAt).getTime() >= since
  return {
    ...s,
    active: s.active ? move(s.active) : s.active,
    pending: s.pending.map((w) => (inRun(w) ? move(w) : w)),
    history: s.history.map((w) => (inRun(w) ? move(w) : w)),
  }
}

/**
 * The state after a saved edit of `program` (the server's answer), before it is cached: on a fork
 * (`switchedFrom`, the template) the copy becomes the active program and the current run moves to it; the
 * active program's version is the new one. Another active program is left alone. Idempotent: the live
 * «program» sync may have switched to the copy already.
 */
export function afterEdit<T extends RetargetInput & { programId: string; programVersion: number | null }>(
  s: T,
  program: { id: string; version?: number },
  switchedFrom: string | null,
): T {
  let next = s
  if (switchedFrom && switchedFrom !== program.id && (s.programId === switchedFrom || s.programId === program.id))
    next = { ...retargetWorkouts(next, switchedFrom, program.id), programId: program.id }
  if (next.programId === program.id && program.version != null && next.programVersion !== program.version)
    next = { ...next, programVersion: program.version }
  return next
}
