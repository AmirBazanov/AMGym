import { useEffect, useState } from 'react'
import { getLifeView } from '../../api'
import { IconChevronLeft, IconChevronRight, IconPlus } from '../../components/icons'
import {
  dayHeading,
  dueLabel,
  openDeadlines,
  scheduleWeek,
  timeRange,
  weekDates,
  weekdayOf,
  weekLabel,
  type LifeView,
  type ScheduleItem,
} from '../../life'
import { haptic } from '../../telegram'
import { useRemote } from '../../useRemote'
import { DoneCheck, LifeLoadError, type LifeCtx } from './common'

/** How far the week arrows go, in weeks each way. */
const MAX_OFFSET = 52

/**
 * «Расписание»: a week of weekly slots, plans by date and deadlines, with week paging. Each week is loaded
 * with ?date=<its Monday> (the view's plans cover a window around that date); «today» stays the server's.
 */
export function LifeSchedule({ ctx }: { ctx: LifeCtx }) {
  const { view } = ctx
  const [offset, setOffset] = useState(0)
  const dates = weekDates(view.today, offset)
  const monday = dates[0]!
  const week = useRemote<LifeView>(`life:week:${monday}`, () => getLifeView(monday), ['life'])
  // The section's view already has this week's slots; its plans start from yesterday, so it is only a
  // placeholder while the week itself loads.
  const data = week.data ?? (offset === 0 ? view : null)
  // Checks show at once; the next week view (after the server answers) replaces them.
  const [checked, setChecked] = useState<Record<string, boolean>>({})
  useEffect(() => setChecked({}), [week.data])
  const days = (data ? scheduleWeek(data, dates, view.today) : []).map((d) => ({
    ...d,
    items: d.items.map((it) => ({ ...it, done: checked[`${it.kind}:${it.id}`] ?? it.done })),
  }))
  const deadlines = openDeadlines(view.deadlines, view.today)

  const open = (it: ScheduleItem) => {
    const src = data ?? view
    const item =
      it.kind === 'slot'
        ? src.slots.find((s) => s.id === it.id)
        : it.kind === 'event'
          ? src.events.find((e) => e.id === it.id)
          : src.deadlines.find((d) => d.id === it.id)
    if (item) ctx.edit({ kind: `${it.kind}s` as 'slots' | 'events' | 'deadlines', item })
  }
  const toggle = (it: ScheduleItem) => {
    if (it.kind === 'slot') return
    setChecked((c) => ({ ...c, [`${it.kind}:${it.id}`]: !it.done }))
    // The week's own view reloads with the section's once the server answers (refresh bus, topic "life").
    ctx.toggleDone(it.kind === 'event' ? 'events' : 'deadlines', it.id, !it.done)
  }
  const go = (d: number) => {
    haptic.select()
    setOffset((o) => Math.max(-MAX_OFFSET, Math.min(MAX_OFFSET, o + d)))
  }
  // A new plan defaults to today in this week, else its Monday.
  const newDay = dates.includes(view.today) ? view.today : monday

  return (
    <>
      <div className="date-nav">
        <button className="icon-btn" aria-label="Прошлая неделя" disabled={offset <= -MAX_OFFSET} onClick={() => go(-1)}>
          <IconChevronLeft />
        </button>
        <button className="date-nav-label" onClick={() => offset !== 0 && go(-offset)}>
          {weekLabel(view.today, offset)}
        </button>
        <button className="icon-btn" aria-label="Следующая неделя" disabled={offset >= MAX_OFFSET} onClick={() => go(1)}>
          <IconChevronRight />
        </button>
      </div>

      {!data ? (
        week.error != null ? (
          <>
            <div className="spacer" />
            <LifeLoadError error={week.error} onRetry={week.reload} />
          </>
        ) : (
          <div className="card hint" style={{ marginTop: 12 }}>
            Загрузка…
          </div>
        )
      ) : (
        days.map((d) => (
          <section key={d.date}>
            <h2 className={d.isToday ? 'life-today' : undefined}>{dayHeading(d.date, view.today)}</h2>
            {d.items.length ? (
              <div className="list">
                {d.items.map((it) => (
                  <div className={`row life-row ${it.done ? 'done' : ''}`} key={`${it.kind}:${it.id}`}>
                    <button className="life-open" onClick={() => open(it)}>
                      <span className="life-time num">{timeRange(it.start, it.end) || '—'}</span>
                      <span className="grow">
                        <span className="title life-title">{it.title}</span>
                        <span className="sub">{it.sub}</span>
                      </span>
                    </button>
                    {it.kind !== 'slot' && <DoneCheck done={it.done} label={`Сделано: ${it.title}`} onToggle={() => toggle(it)} />}
                  </div>
                ))}
              </div>
            ) : (
              <div className="card hint life-free">Свободно</div>
            )}
          </section>
        ))
      )}

      <div className="spacer" />
      <div className="life-actions">
        <button
          className="btn secondary"
          onClick={() => {
            haptic.tap()
            ctx.edit({ kind: 'events', defaults: { day: newDay } })
          }}
        >
          <IconPlus />
          План
        </button>
        <button
          className="btn secondary"
          onClick={() => {
            haptic.tap()
            ctx.edit({ kind: 'slots', defaults: { weekday: String(weekdayOf(newDay)) } })
          }}
        >
          <IconPlus />
          Урок, пара
        </button>
      </div>

      <div className="day-head">
        <h2>Дедлайны</h2>
        <button
          className="link-btn"
          onClick={() => {
            haptic.tap()
            ctx.edit({ kind: 'deadlines' })
          }}
        >
          Добавить
        </button>
      </div>
      {deadlines.length ? (
        <div className="list">
          {deadlines.map((d) => (
            <div className="row life-row" key={d.id}>
              <button className="life-open" onClick={() => ctx.edit({ kind: 'deadlines', item: d })}>
                <span className="grow">
                  <span className="title life-title">{d.title}</span>
                  <span className={`sub ${d.days < 0 ? 'life-overdue' : ''}`}>
                    {[d.course, `${dayHeading(d.day, view.today)} · ${dueLabel(d.days)}`].filter(Boolean).join(' · ')}
                  </span>
                </span>
              </button>
              <DoneCheck done={false} label={`Сдано: ${d.title}`} onToggle={() => ctx.toggleDone('deadlines', d.id, true)} />
            </div>
          ))}
        </div>
      ) : (
        <div className="card hint">Открытых дедлайнов нет.</div>
      )}
    </>
  )
}
