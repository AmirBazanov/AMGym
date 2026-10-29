import { useState } from 'react'
import { putLifeSettings } from '../../api'
import { IconPlus } from '../../components/icons'
import { Sheet } from '../../components/Sheet'
import { Switch } from '../../components/Switch'
import {
  agendaDoneKind,
  agendaLabel,
  dueChores,
  dueLabel,
  inAgenda,
  longDate,
  openTasks,
  soonDeadlines,
  timeRange,
  TIME_RE,
  type AgendaItem,
  type LifeSettings,
  type LifeView,
} from '../../life'
import { haptic } from '../../telegram'
import { DoneCheck, lifeErrorText, type LifeCtx } from './common'

const TOP_TASKS = 5

/** The object behind an agenda point, for its edit sheet. */
function agendaSource(view: LifeView, a: AgendaItem): { kind: 'slots' | 'events' | 'deadlines' | 'chores'; item: object } | null {
  const find = <T extends { id: number }>(list: T[]) => list.find((x) => x.id === a.id) ?? null
  const item =
    a.kind === 'slot' ? find(view.slots) : a.kind === 'event' ? find(view.events) : a.kind === 'deadline' ? find(view.deadlines) : find(view.chores)
  if (!item) return null
  const kind = a.kind === 'slot' ? 'slots' : a.kind === 'event' ? 'events' : a.kind === 'deadline' ? 'deadlines' : 'chores'
  return { kind, item }
}

/** «Сегодня»: the day's agenda by time, chores due, deadlines within 3 days, the top open tasks. */
export function LifeToday({ ctx, openTasks: showTasks }: { ctx: LifeCtx; openTasks: () => void }) {
  const { view } = ctx
  const [settingsOpen, setSettingsOpen] = useState(false)
  const deadlines = soonDeadlines(view.deadlines, view.today, 3).filter((d) => !inAgenda(view.agenda, 'deadline', d.id))
  const chores = dueChores(view.chores).filter((c) => !inAgenda(view.agenda, 'chore', c.id))
  const tasks = openTasks(view.tasks)

  return (
    <>
      <div className="hero">
        <div className="eyebrow">{longDate(view.today)}</div>
        <h1>Сегодня</h1>
      </div>

      <h2>Повестка</h2>
      {view.agenda.length ? (
        <div className="list">
          {view.agenda.map((a) => {
            const doneKind = agendaDoneKind(a.kind)
            const src = agendaSource(view, a)
            return (
              <div className={`row life-row ${a.done ? 'done' : ''}`} key={`${a.kind}:${a.id}`}>
                <button className="life-open" disabled={!src} onClick={() => src && ctx.edit({ kind: src.kind, item: src.item })}>
                  <span className="life-time num">{timeRange(a.start, a.end) || '—'}</span>
                  <span className="grow">
                    <span className="title life-title">{a.title}</span>
                    <span className={`sub ${a.overdueDays > 0 ? 'life-overdue' : ''}`}>{agendaLabel(a)}</span>
                  </span>
                </button>
                {a.kind === 'chore' ? (
                  <button className="btn secondary life-did" onClick={() => ctx.toggleDone('chores', a.id, true)}>
                    Сделано
                  </button>
                ) : (
                  doneKind && <DoneCheck done={a.done} label={`Сделано: ${a.title}`} onToggle={() => ctx.toggleDone(doneKind, a.id, !a.done)} />
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="card hint">Сегодня свободно.</div>
      )}

      {deadlines.length > 0 && (
        <>
          <h2>Дедлайны</h2>
          <div className="list">
            {deadlines.map((d) => (
              <div className="row life-row" key={d.id}>
                <button className="life-open" onClick={() => ctx.edit({ kind: 'deadlines', item: d })}>
                  <span className="grow">
                    <span className="title life-title">{d.title}</span>
                    <span className={`sub ${d.days < 0 ? 'life-overdue' : ''}`}>{[d.course, dueLabel(d.days)].filter(Boolean).join(' · ')}</span>
                  </span>
                </button>
                <DoneCheck done={false} label={`Сдано: ${d.title}`} onToggle={() => ctx.toggleDone('deadlines', d.id, true)} />
              </div>
            ))}
          </div>
        </>
      )}

      {chores.length > 0 && (
        <>
          <h2>Дом</h2>
          <div className="list">
            {chores.map((c) => (
              <div className="row life-row" key={c.id}>
                <button className="life-open" onClick={() => ctx.edit({ kind: 'chores', item: c })}>
                  <span className="grow">
                    <span className="title life-title">{c.title}</span>
                    <span className={`sub ${c.left < 0 ? 'life-overdue' : ''}`}>{dueLabel(c.left)}</span>
                  </span>
                </button>
                <button className="btn secondary life-did" onClick={() => ctx.toggleDone('chores', c.id, true)}>
                  Сделано
                </button>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="day-head">
        <h2>Задачи</h2>
        {tasks.length > TOP_TASKS && (
          <button
            className="link-btn"
            onClick={() => {
              haptic.select()
              showTasks()
            }}
          >
            Все {tasks.length}
          </button>
        )}
      </div>
      {tasks.length ? (
        <div className="list">
          {tasks.slice(0, TOP_TASKS).map((t) => (
            <div className="row life-row" key={t.id}>
              <DoneCheck done={t.done} label={`Сделано: ${t.title}`} onToggle={() => ctx.toggleDone('tasks', t.id, !t.done)} />
              <button className="life-open" onClick={() => ctx.edit({ kind: 'tasks', item: t })}>
                <span className="grow">
                  <span className="title life-title">{t.title}</span>
                  {t.tag && <span className="sub">{t.tag}</span>}
                </span>
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="card hint">Открытых задач нет.</div>
      )}

      <div className="spacer" />
      <div className="life-actions">
        <button
          className="btn secondary"
          onClick={() => {
            haptic.tap()
            ctx.edit({ kind: 'events', defaults: { day: view.today } })
          }}
        >
          <IconPlus />
          План
        </button>
        <button
          className="btn secondary"
          onClick={() => {
            haptic.tap()
            ctx.edit({ kind: 'tasks' })
          }}
        >
          <IconPlus />
          Задача
        </button>
      </div>
      <p className="hint" style={{ margin: '8px 4px 0' }}>
        Добавить можно и в чате: «в учёбу добавь завтра в 11 позаниматься C», «добавь задачу купить билеты».
      </p>

      {view.settings && (
        <>
          <h2>Напоминания</h2>
          <div className="list">
            <button
              className="row"
              onClick={() => {
                haptic.tap()
                setSettingsOpen(true)
              }}
            >
              <span className="grow">
                <span className="title">Напоминания и сводка</span>
                <span className="sub" style={{ display: 'block' }}>
                  за {view.settings.remindBefore} мин до плана · сводка {view.settings.summaryEnabled ? `в ${view.settings.summaryTime}` : 'выключена'}
                </span>
              </span>
            </button>
          </div>
        </>
      )}

      {settingsOpen && view.settings && (
        <SettingsSheet
          settings={view.settings}
          onSaved={() => {
            setSettingsOpen(false)
            ctx.reload()
          }}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </>
  )
}

function SettingsSheet({ settings, onSaved, onClose }: { settings: LifeSettings; onSaved: () => void; onClose: () => void }) {
  const [before, setBefore] = useState(String(settings.remindBefore))
  const [time, setTime] = useState(settings.summaryTime)
  const [enabled, setEnabled] = useState(settings.summaryEnabled)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const n = Number(before)
  const beforeOk = before.trim() !== '' && Number.isInteger(n) && n >= 0 && n <= 1440
  const timeOk = TIME_RE.test(time)

  async function save() {
    if (busy) return
    if (!beforeOk || !timeOk) {
      haptic.error()
      setError(!beforeOk ? 'Напоминание: от 0 до 1440 минут' : 'Время сводки ЧЧ:ММ')
      return
    }
    const patch: Partial<LifeSettings> = {}
    if (n !== settings.remindBefore) patch.remindBefore = n
    if (time !== settings.summaryTime) patch.summaryTime = time
    if (enabled !== settings.summaryEnabled) patch.summaryEnabled = enabled
    if (!Object.keys(patch).length) return onClose()
    setBusy(true)
    setError(null)
    try {
      await putLifeSettings(patch)
      haptic.success()
      onSaved()
    } catch (err) {
      haptic.error()
      setError(lifeErrorText(err))
      setBusy(false)
    }
  }

  return (
    <Sheet onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void save()
        }}
      >
        <h1 style={{ fontSize: 22 }}>Напоминания и сводка</h1>
        <div className="spacer" />
        <div className="list">
          <label className="row">
            <div className="grow title">Напоминать за, мин</div>
            <input
              type="text"
              inputMode="numeric"
              className={`field life-short-field ${beforeOk ? '' : 'invalid'}`}
              value={before}
              onChange={(e) => setBefore(e.target.value)}
            />
          </label>
          <div className="row">
            <div className="grow title">Утренняя сводка</div>
            <Switch on={enabled} label="Утренняя сводка" onToggle={() => setEnabled((x) => !x)} />
          </div>
          <label className="row">
            <div className="grow title">Время сводки</div>
            <input
              type="time"
              className={`field life-short-field ${timeOk ? '' : 'invalid'}`}
              value={time}
              disabled={!enabled}
              onChange={(e) => setTime(e.target.value.slice(0, 5))}
            />
          </label>
        </div>
        {error && <p className="field-error">{error}</p>}
        <div className="spacer" />
        <button type="submit" className="btn" disabled={busy}>
          {busy ? 'Сохраняю…' : 'Сохранить'}
        </button>
      </form>
    </Sheet>
  )
}
