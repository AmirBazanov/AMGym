import { useEffect, useMemo, useState } from 'react'
import { useScrollActive } from '../components/useScrollActive'
import { DropBadge, IntensityBadge } from '../components/Badges'
import { ExerciseSheet } from '../components/ExerciseSheet'
import { ProgramEdit } from './ProgramEdit'
import { IconChevron } from '../components/icons'
import {
  capitalize,
  dayFocus,
  findProgram,
  formatPrescription,
  getProgram,
  isDropset,
  programExerciseNames,
  programPosition,
  plural,
  PROGRAMS,
  WEEKDAY_LONG,
} from '../program'
import { exerciseNamesWithHistory } from '../programSync'
import { actions, currentRun, useStore } from '../store'
import { confirm, haptic } from '../telegram'
import type { ProgramSummary } from '../api'

type Tab = 'plan' | 'exercises' | 'settings'

export function ProgramScreen() {
  const [tab, setTab] = useState<Tab>('plan')
  const [sheet, setSheet] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const { programId } = useStore()
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3500)
    return () => clearTimeout(t)
  }, [toast])
  const program = getProgram(programId)

  return (
    <>
      <div className="hero">
        <div className="eyebrow">Программа</div>
        <h1 style={{ fontSize: 24 }}>{program.name}</h1>
        <div className="hint">
          {plural(program.weeks.length, ['неделя', 'недели', 'недель'])} ·{' '}
          {plural(program.weeks[0]?.days.length ?? 0, ['тренировка', 'тренировки', 'тренировок'])} в неделю ·{' '}
          {plural(programExerciseNames(program).length, ['упражнение', 'упражнения', 'упражнений'])}
        </div>
      </div>
      <div className="spacer" />
      <div className="segmented">
        {(
          [
            ['plan', 'План'],
            ['exercises', 'Упражнения'],
            ['settings', 'Выбор'],
          ] as const
        ).map(([k, l]) => (
          <button
            key={k}
            className={tab === k ? 'active' : ''}
            onClick={() => {
              haptic.select()
              setTab(k)
            }}
          >
            {l}
          </button>
        ))}
      </div>

      {tab === 'plan' && <Plan onOpen={setSheet} onSaved={setToast} />}
      {tab === 'exercises' && <Exercises onOpen={setSheet} />}
      {tab === 'settings' && <Choose />}

      {sheet && <ExerciseSheet name={sheet} onClose={() => setSheet(null)} />}
      {toast && (
        <div className="card notice record-toast" role="status">
          {toast}
        </div>
      )}
    </>
  )
}

function Plan({ onOpen, onSaved }: { onOpen: (name: string) => void; onSaved: (message: string) => void }) {
  const state = useStore()
  const { programId, startDate, mode } = state
  const [editing, setEditing] = useState<{ week: number; weekday: number } | null>(null)
  // The editor needs the server's program (item ids and a version), not the bundled fallback.
  const canEdit = mode === 'server' && findProgram(programId)?.version != null
  const run = currentRun(state)
  const program = getProgram(programId)
  const current = programPosition(program, startDate).week
  const [picked, setWeek] = useState(current)
  // A shorter program may have replaced the one the week was picked in: fall back to its first week.
  const w = program.weeks.find((x) => x.number === picked) ?? program.weeks[0]
  const week = w.number
  const chipsRef = useScrollActive<HTMLDivElement>(week)

  return (
    <>
      <div className="spacer" />
      <div className="chips" ref={chipsRef}>
        {program.weeks.map((x) => (
          <button
            key={x.number}
            className={`chip ${x.number === week ? 'active' : ''}`}
            onClick={() => {
              haptic.select()
              setWeek(x.number)
            }}
          >
            Неделя {x.number}
            {x.number === current && <span className="dot" />}
          </button>
        ))}
      </div>
      {w.days.map((d) => {
        const done = run.some((h) => h.week === week && h.weekday === d.weekday)
        return (
          <div key={d.weekday}>
            <div className="day-head">
              <h2>
                {WEEKDAY_LONG[d.weekday]} · {dayFocus(d)}
                {done && ' · ✓'}
              </h2>
              {canEdit && d.id != null && (
                <button
                  className="link-btn"
                  onClick={() => {
                    haptic.tap()
                    setEditing({ week, weekday: d.weekday })
                  }}
                >
                  Изменить
                </button>
              )}
            </div>
            <div className="list">
              {d.exercises.map((e) => (
                <button className="row" key={e.order} onClick={() => onOpen(e.name)}>
                  <div className="grow">
                    <div className="title">{capitalize(e.name)}</div>
                    <div className="ex-meta">
                      <span className="ex-target num">{formatPrescription(e.prescription)}</span>
                      <IntensityBadge value={e.intensity} />
                      {isDropset(e.prescription) && <DropBadge />}
                    </div>
                  </div>
                  <IconChevron />
                </button>
              ))}
            </div>
          </div>
        )
      })}
      {mode === 'demo' && (
        <p className="hint" style={{ padding: '12px 4px' }}>
          Редактор программы работает в дневнике из Telegram.
        </p>
      )}
      {editing && (
        <ProgramEdit week={editing.week} weekday={editing.weekday} onClose={() => setEditing(null)} onSaved={onSaved} />
      )}
    </>
  )
}

function Exercises({ onOpen }: { onOpen: (name: string) => void }) {
  const { programId, history } = useStore()
  const program = getProgram(programId)
  const [q, setQ] = useState('')
  // The program's exercises, then those only in the history (a replaced one stays findable).
  const names = useMemo(() => exerciseNamesWithHistory(program, history), [program, history])
  const filtered = names.filter((n) => n.includes(q.trim().toLowerCase()))

  const count = (name: string) => history.filter((w) => w.exercises.some((e) => e.name === name)).length

  return (
    <>
      <div className="spacer" />
      <input className="search" placeholder="Поиск упражнения" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="spacer" />
      <div className="list">
        {filtered.map((n) => (
          <button className="row" key={n} onClick={() => onOpen(n)}>
            <div className="grow">
              <div className="title">{capitalize(n)}</div>
              <div className="sub">{count(n) ? `${count(n)} тренировок в истории` : 'ещё не делал'}</div>
            </div>
            <IconChevron />
          </button>
        ))}
        {!filtered.length && <div className="empty">Ничего не нашлось</div>}
      </div>
    </>
  )
}

/** «Выбор»: the server's list (templates and own copies), the bundled programs until it is loaded. */
function choices(list: ProgramSummary[] | null): { id: string; name: string; weeks: number; source: string | null; own: boolean }[] {
  if (list?.length) return list.map((p) => ({ id: p.id, name: p.name, weeks: p.weeks, source: p.source, own: p.editable }))
  return PROGRAMS.map((p) => ({ id: p.id, name: p.name, weeks: p.weeks.length, source: p.source, own: false }))
}

function Choose() {
  const { programId, programVersion, programList, startDate, restSeconds, mode } = useStore()
  // Refreshed when shown and after the active program changed (a new copy appears in the list).
  useEffect(() => {
    void actions.loadProgramList()
  }, [mode, programId, programVersion])
  return (
    <>
      <h2>Активная программа</h2>
      <div className="list">
        {choices(programList).map((p) => (
          <button className="row" key={p.id} onClick={() => actions.setProgram(p.id, startDate)}>
            <div className="grow">
              <div className="title">{p.name}</div>
              <div className="sub">
                {plural(p.weeks, ['неделя', 'недели', 'недель'])}{p.source ? ` · из ${p.source}` : ''}
                {p.own ? ' · моя' : ''}
              </div>
            </div>
            {p.id === programId && <span style={{ color: 'var(--link)', fontWeight: 600 }}>✓</span>}
          </button>
        ))}
      </div>

      <h2>Дата старта (понедельник 1-й недели)</h2>
      <input
        className="search"
        type="date"
        value={startDate}
        onChange={(e) => e.target.value && actions.setProgram(programId, e.target.value)}
      />

      <h2>Отдых между подходами</h2>
      <div className="segmented">
        {[60, 90, 120, 180].map((s) => (
          <button key={s} className={s === restSeconds ? 'active' : ''} onClick={() => actions.setRestSeconds(s)}>
            {s < 120 ? `${s} с` : `${s / 60} мин`}
          </button>
        ))}
      </div>

      {mode === 'demo' ? (
        <>
          <h2>Данные</h2>
          <div className="list">
            <button
              className="row"
              onClick={async () => {
                if (await confirm('Заменить историю демо-данными? Дата старта станет демо-понедельником.'))
                  actions.resetDemo()
              }}
            >
              <div className="grow" style={{ color: 'var(--link)' }}>
                Заполнить демо-историей
              </div>
            </button>
            <button
              className="row"
              onClick={async () => {
                if (await confirm('Удалить всю историю тренировок?')) actions.clearAll()
              }}
            >
              <div className="grow" style={{ color: 'var(--danger)' }}>
                Очистить историю
              </div>
            </button>
          </div>
          <p className="hint" style={{ padding: '8px 4px' }}>
            Это демо: данные хранятся только в этом браузере. Открой дневник из бота в Telegram, и тренировки
            будут сохраняться на сервере вместе с записями из чата.
          </p>
        </>
      ) : (
        <p className="hint" style={{ padding: '16px 4px' }}>
          Тренировки сохраняются на сервере бота. Записи текстом в чате бота тоже попадают сюда.
        </p>
      )}
    </>
  )
}
