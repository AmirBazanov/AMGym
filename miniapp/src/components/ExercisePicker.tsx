import { useMemo, useState } from 'react'
import { getExercises } from '../api'
import { capitalize, type Program } from '../program'
import { normalizeName, validateName } from '../programEdit'
import { pickerNames } from '../programSync'
import { exactName, searchNames } from '../pickerSearch'
import { useStore } from '../store'
import { haptic } from '../telegram'
import { useRemote } from '../useRemote'
import { IconChevron, IconPlus } from './icons'
import { Sheet } from './Sheet'

/**
 * Choosing an exercise for the day editor: this day's program first, then the catalog (GET /api/exercises,
 * most done first); offline only the program's. Similar names come first, so a typo does not create a twin;
 * a name nobody has goes last as «Добавить «…»» (a new exercise without history).
 */
export function ExercisePicker({
  program,
  title,
  dayNames,
  current,
  onPick,
  onClose,
}: {
  program: Program
  title: string
  dayNames: readonly string[] // already in the day: not offered (one exercise once per day)
  current?: string // the one being replaced
  onPick: (name: string) => void
  onClose: () => void
}) {
  const { mode, history } = useStore()
  const [q, setQ] = useState('')
  const catalog = useRemote(`exercises:${mode}`, () => (mode === 'server' ? getExercises() : Promise.resolve(null)))
  const inDay = useMemo(() => new Set(dayNames.map(normalizeName)), [dayNames])
  const all = useMemo(() => pickerNames(catalog.data, program, []), [catalog.data, program])
  const offered = all.filter((n) => !inDay.has(normalizeName(n)))
  const found = searchNames(offered, q)
  const exact = exactName(all, q)
  const typed = q.trim()
  const tooLong = typed ? validateName(typed) : null
  const done = useMemo(() => new Set(history.flatMap((w) => w.exercises.map((e) => normalizeName(e.name)))), [history])

  const pick = (name: string) => {
    haptic.tap()
    onPick(name)
  }

  return (
    <Sheet onClose={onClose} className="tall">
      <h1 style={{ fontSize: 22 }}>{title}</h1>
      {current && <div className="hint">Сейчас: {capitalize(current)}</div>}
      <div className="spacer" />
      <input
        className="search"
        autoFocus
        enterKeyHint="done"
        placeholder="Найти или вписать новое"
        value={q}
        maxLength={240}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="spacer" />
      <div className="list">
        {found.map((n) => (
          <button className="row" key={n} onClick={() => pick(n)}>
            <div className="grow">
              <div className="title">{capitalize(n)}</div>
              {!done.has(normalizeName(n)) && <div className="sub">ещё не делал</div>}
            </div>
            <IconChevron />
          </button>
        ))}
        {exact && inDay.has(normalizeName(exact)) && (
          <div className="row">
            <div className="grow">
              <div className="title muted">{capitalize(exact)}</div>
              <div className="sub">уже есть в этом дне</div>
            </div>
          </div>
        )}
        {typed && !exact && (
          <button className="row" disabled={!!tooLong} onClick={() => pick(typed)}>
            <div className="grow">
              <div className="title" style={{ color: 'var(--link)' }}>
                Добавить «{typed}»
              </div>
              <div className="sub">{tooLong ?? 'новое упражнение, истории нет'}</div>
            </div>
            <IconPlus />
          </button>
        )}
        {!found.length && !typed && <div className="empty">Все упражнения уже в этом дне</div>}
      </div>
      {catalog.loading && mode === 'server' && <p className="hint" style={{ padding: '8px 4px' }}>Загружаю каталог…</p>}
    </Sheet>
  )
}
