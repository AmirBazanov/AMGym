import { useMemo, useState } from 'react'
import { useScrollActive } from '../components/useScrollActive'
import { DropBadge, IntensityBadge } from '../components/Badges'
import { ExerciseSheet } from '../components/ExerciseSheet'
import { IconChevron } from '../components/icons'
import {
  capitalize,
  dayFocus,
  formatPrescription,
  getProgram,
  isDropset,
  programExerciseNames,
  programPosition,
  plural,
  PROGRAMS,
  WEEKDAY_LONG,
} from '../program'
import { actions, useStore } from '../store'
import { haptic } from '../telegram'

type Tab = 'plan' | 'exercises' | 'settings'

export function ProgramScreen() {
  const [tab, setTab] = useState<Tab>('plan')
  const [sheet, setSheet] = useState<string | null>(null)
  const { programId } = useStore()
  const program = getProgram(programId)

  return (
    <>
      <div className="hero">
        <div className="eyebrow">Программа</div>
        <h1 style={{ fontSize: 24 }}>{program.name}</h1>
        <div className="hint">
          {plural(program.weeks.length, ['неделя', 'недели', 'недель'])} · {program.weeks[0].days.length} тренировки в
          неделю · {plural(programExerciseNames(program).length, ['упражнение', 'упражнения', 'упражнений'])}
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

      {tab === 'plan' && <Plan onOpen={setSheet} />}
      {tab === 'exercises' && <Exercises onOpen={setSheet} />}
      {tab === 'settings' && <Choose />}

      {sheet && <ExerciseSheet name={sheet} onClose={() => setSheet(null)} />}
    </>
  )
}

function Plan({ onOpen }: { onOpen: (name: string) => void }) {
  const { programId, startDate, history } = useStore()
  const program = getProgram(programId)
  const current = programPosition(program, startDate).week
  const [week, setWeek] = useState(current)
  const chipsRef = useScrollActive<HTMLDivElement>(week)
  const w = program.weeks.find((x) => x.number === week)!

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
        const done = history.some((h) => h.week === week && h.weekday === d.weekday)
        return (
          <div key={d.weekday}>
            <h2>
              {WEEKDAY_LONG[d.weekday]} · {dayFocus(d)}
              {done && ' · ✓'}
            </h2>
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
    </>
  )
}

function Exercises({ onOpen }: { onOpen: (name: string) => void }) {
  const { programId, history } = useStore()
  const program = getProgram(programId)
  const [q, setQ] = useState('')
  const names = useMemo(() => programExerciseNames(program), [program])
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

function Choose() {
  const { programId, startDate, restSeconds } = useStore()
  return (
    <>
      <h2>Активная программа</h2>
      <div className="list">
        {PROGRAMS.map((p) => (
          <button className="row" key={p.id} onClick={() => actions.setProgram(p.id, startDate)}>
            <div className="grow">
              <div className="title">{p.name}</div>
              <div className="sub">
                {p.weeks.length} недель · из {p.source}
              </div>
            </div>
            {p.id === programId && <span style={{ color: 'var(--link)', fontWeight: 600 }}>✓</span>}
          </button>
        ))}
        <div className="row">
          <div className="grow">
            <div className="title muted">Своя программа</div>
            <div className="sub">Загрузка xlsx появится вместе с сервером</div>
          </div>
        </div>
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

      <h2>Данные</h2>
      <div className="list">
        <button className="row" onClick={() => actions.resetDemo()}>
          <div className="grow" style={{ color: 'var(--link)' }}>
            Заполнить демо-историей
          </div>
        </button>
        <button className="row" onClick={() => actions.clearAll()}>
          <div className="grow" style={{ color: 'var(--danger)' }}>
            Очистить историю
          </div>
        </button>
      </div>
      <p className="hint" style={{ padding: '8px 4px' }}>
        Пока всё хранится только на этом устройстве. Когда появится сервер, тренировки будут синхронизироваться с
        ботом.
      </p>
    </>
  )
}
