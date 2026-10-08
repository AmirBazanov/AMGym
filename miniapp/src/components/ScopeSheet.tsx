import { useEffect, useMemo, useRef, useState } from 'react'
import type { OpResult } from '../api'
import { capitalize, type Program, type ProgramDay } from '../program'
import {
  applyOps,
  defaultScope,
  draftOps,
  formatWeeks,
  opKinds,
  opLabel,
  scopeWeeks,
  summarize,
  type Draft,
  type EditOutcome,
  type OpKind,
  type ScopeChoice,
} from '../programEdit'
import { actions } from '../store'
import { confirm, haptic } from '../telegram'
import { Sheet } from './Sheet'

const KIND_TITLE: Record<OpKind, string> = {
  structure: 'Замена и состав дня',
  prescribe: 'Подходы и повторы',
}

/**
 * Where the day's edits go: per kind of edit only this week, from this week on, all weeks or chosen ones
 * (defaults: programEdit.defaultScope). Several weeks: the server's dryRun shows
 * where the edit lands and which weeks it skips; until it answers, the local estimate.
 */
export function ScopeSheet({
  program,
  week,
  base,
  draft,
  onClose,
  onDone,
}: {
  program: Program
  week: number
  base: ProgramDay
  draft: Draft
  onClose: () => void
  onDone: (outcome: EditOutcome) => void
}) {
  const weekday = base.weekday
  const plain = draftOps(week, weekday, base, draft)
  const kinds = opKinds(plain)
  const allWeeks = useMemo(() => program.weeks.map((w) => w.number), [program])
  const [choice, setChoice] = useState<Record<OpKind, ScopeChoice>>(() => defaultScope(plain))
  const [picked, setPicked] = useState<Record<OpKind, number[]>>({ structure: [], prescribe: [] })
  const weeksOf = (k: OpKind) => scopeWeeks(choice[k], week, allWeeks, picked[k])
  const structure = kinds.structure ? weeksOf('structure') : undefined
  const prescribe = kinds.prescribe ? weeksOf('prescribe') : undefined
  const ops = draftOps(week, weekday, base, draft, { structure, prescribe })
  const opsKey = JSON.stringify(ops)
  const several = (structure?.length ?? 1) > 1 || (prescribe?.length ?? 1) > 1
  const local = applyOps(program, ops)

  // The server's preview (dryRun) for several weeks; a counter drops answers to an older choice.
  const [server, setServer] = useState<{ key: string; results?: OpResult[]; error?: string } | null>(null)
  const seq = useRef(0)
  useEffect(() => {
    if (!several || !local.ok) return
    const n = ++seq.current
    void actions.editProgram(ops, true).then((out) => {
      if (n !== seq.current) return
      if (out.kind === 'saved') setServer({ key: opsKey, results: out.results })
      else if (out.kind === 'conflict' || out.kind === 'not_active') onDone(out)
      else if (out.kind === 'invalid') setServer({ key: opsKey, error: out.message })
      else setServer({ key: opsKey }) // offline: the local estimate stays
    })
    // `ops` is described by opsKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opsKey, several])
  const fresh = server?.key === opsKey ? server : null

  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fork = !program.editable

  const results = fresh?.results ?? (local.ok ? local.results : [])
  const summary = summarize(ops, results, (op) => opLabel(op, base, draft))
  const blocking = !local.ok ? local.error : (fresh?.error ?? null)

  // Set before the confirm: a double tap must not open two dialogs and send two saves.
  const busy = useRef(false)
  const save = async () => {
    if (busy.current) return
    busy.current = true
    try {
      if (fork && !(await confirm(`Создам твою копию «${program.name} · моя». Оригинал останется, история сохранится.`))) return
      setSaving(true)
      setError(null)
      const out = await actions.editProgram(ops)
      setSaving(false)
      if (out.kind === 'invalid' || out.kind === 'failed') {
        haptic.error()
        setError(out.message)
        return
      }
      onDone(out)
    } finally {
      busy.current = false
    }
  }

  const option = (k: OpKind, c: ScopeChoice, label: string) => (
    <button
      key={c}
      className="row"
      aria-pressed={choice[k] === c}
      onClick={() => {
        haptic.select()
        setChoice({ ...choice, [k]: c })
      }}
    >
      <div className="grow title">{label}</div>
      {choice[k] === c && <span className="check-mark">✓</span>}
    </button>
  )

  const lastWeek = allWeeks[allWeeks.length - 1]
  const section = (k: OpKind) => (
    <div key={k}>
      <h2>{KIND_TITLE[k]}</h2>
      <div className="list">
        {option(k, 'week', `Только неделя ${week}`)}
        {week < lastWeek && week > allWeeks[0] && option(k, 'from', `Неделя ${week} и дальше`)}
        {option(k, 'all', 'Все недели')}
        {option(k, 'pick', 'Выбрать недели')}
      </div>
      {choice[k] === 'pick' && (
        <>
          <div className="spacer" />
          <div className="chips wrap">
            {allWeeks.map((n) => {
              const on = n === week || picked[k].includes(n)
              return (
                <button
                  key={n}
                  className={`chip ${on ? 'active' : ''}`}
                  disabled={n === week}
                  onClick={() => {
                    haptic.select()
                    const cur = picked[k]
                    setPicked({ ...picked, [k]: cur.includes(n) ? cur.filter((x) => x !== n) : [...cur, n] })
                  }}
                >
                  {n}
                </button>
              )
            })}
          </div>
        </>
      )}
      {k === 'prescribe' && (prescribe?.length ?? 1) > 1 && (
        <div className="card notice warn" style={{ marginTop: 10 }}>
          Волны нагрузки по неделям выровняются: во всех выбранных неделях будет одно и то же предписание.
        </div>
      )}
    </div>
  )

  return (
    <Sheet onClose={saving ? () => {} : onClose}>
      <h1 style={{ fontSize: 22 }}>Куда применить</h1>
      <div className="hint">
        {capitalize(base.title)} · неделя {week}
      </div>
      {kinds.structure && section('structure')}
      {kinds.prescribe && section('prescribe')}

      <h2>Что изменится</h2>
      <div className="card scope-preview">
        {blocking ? (
          <div className="field-error" style={{ margin: 0 }}>
            {blocking}
          </div>
        ) : (
          <>
            <div>
              {summary.changed.length > 1 ? `Изменится: недели ${formatWeeks(summary.changed)}` : `Изменится только неделя ${week}`}
            </div>
            {summary.skipped.map((s, i) => (
              <div key={i} className="hint">
                Пропущено: {s.weeks.length > 1 ? 'недели' : 'неделя'} {formatWeeks(s.weeks)} — {s.what}: {s.reason}
              </div>
            ))}
            {several && !fresh && <div className="hint">Проверяю на сервере…</div>}
          </>
        )}
      </div>
      {fork && (
        <p className="hint" style={{ padding: '8px 4px 0' }}>
          Это оригинальная программа. Сохранение создаст твою копию «{program.name} · моя», она станет активной.
        </p>
      )}
      {error && <div className="field-error">{error}</div>}

      <div className="spacer" />
      <button className="btn" disabled={saving || !!blocking || !ops.length} onClick={save}>
        {saving ? 'Сохраняю…' : fork ? 'Создать копию и сохранить' : 'Сохранить'}
      </button>
      <button className="btn ghost" disabled={saving} onClick={onClose}>
        Назад к правкам
      </button>
    </Sheet>
  )
}
