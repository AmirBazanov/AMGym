import { useState } from 'react'
import { IconChevron } from '../components/icons'
import { RecordToast } from '../components/RecordToast'
import { useWellbeing, useWellbeingSheet, WELLBEING_DAYS, WellbeingDaySheet } from '../components/Wellbeing'
import { capitalize, WEEKDAY_SHORT } from '../program'
import { actions, useStore, type Workout } from '../store'
import { exerciseVolume, formatKg, formatLongDate, formatTonnage, workoutSetCount, workoutVolume } from '../stats'
import { confirm, haptic } from '../telegram'
import { entriesCount, formatWellbeing, formatWellbeingDate, groupByDate, localISODate, type WellbeingDay } from '../wellbeing'

export function History() {
  const { history } = useStore()
  const [open, setOpen] = useState<string | null>(null)
  const sorted = [...history].reverse()
  const monthAgo = Date.now() - 30 * 86_400_000
  const recent = history.filter((w) => new Date(w.startedAt).getTime() >= monthAgo)
  const wb = useWellbeing()
  const sheet = useWellbeingSheet(wb.entries)
  const wbDays = groupByDate(wb.entries ?? [])

  // A day's wellbeing goes under its newest workout; days without a workout get their own block below.
  const inlineFor = new Map<string, WellbeingDay>()
  const restDays: WellbeingDay[] = []
  for (const d of wbDays) {
    const w = sorted.find((x) => localISODate(new Date(x.startedAt)) === d.date)
    if (w) inlineFor.set(w.id, d)
    else restDays.push(d)
  }

  const wellbeing = (
    <>
      {restDays.length > 0 && (
        <WellbeingBlock
          title={inlineFor.size ? 'Самочувствие в дни без тренировок' : `Самочувствие за ${WELLBEING_DAYS} дней`}
          days={restDays}
          onOpen={sheet.open}
        />
      )}
      <WellbeingDaySheet wb={wb} sheet={sheet} />
    </>
  )

  if (!history.length)
    return (
      <>
        <RecordToast />
        <div className="empty">
          <div className="big">🏋️</div>
          Тренировок пока нет.
          <br />
          Начни первую на вкладке «Сегодня».
        </div>
        {wellbeing}
      </>
    )

  // Group by program week, newest first.
  const groups: { week: number; items: Workout[] }[] = []
  for (const w of sorted) {
    const g = groups[groups.length - 1]
    if (g && g.week === w.week) g.items.push(w)
    else groups.push({ week: w.week, items: [w] })
  }

  return (
    <>
      <RecordToast />
      <div className="hero">
        <div className="eyebrow">История</div>
        <h1>За 30 дней</h1>
      </div>
      <div className="spacer" />
      <div className="tiles">
        <div className="tile">
          <div className="v">{recent.length}</div>
          <div className="k">тренировок</div>
        </div>
        <div className="tile">
          <div className="v">{recent.reduce((n, w) => n + workoutSetCount(w), 0)}</div>
          <div className="k">подходов</div>
        </div>
        <div className="tile">
          <div className="v">{formatTonnage(recent.reduce((n, w) => n + workoutVolume(w), 0))}</div>
          <div className="k">тоннаж</div>
        </div>
      </div>

      {groups.map((g) => (
        <div key={`${g.week}-${g.items[0].id}`}>
          <h2>Неделя {g.week}</h2>
          {g.items.map((w) => (
            <WorkoutCard
              key={w.id}
              w={w}
              open={open === w.id}
              onToggle={() => setOpen(open === w.id ? null : w.id)}
              wellbeing={inlineFor.get(w.id)}
              onWellbeing={sheet.open}
            />
          ))}
        </div>
      ))}
      {wellbeing}
    </>
  )
}

function WellbeingLine({ day }: { day: WellbeingDay }) {
  return (
    <>
      <span className="wb-text">{formatWellbeing(day.entries[0])}</span>
      {day.entries.length > 1 && <span className="hint num"> · {entriesCount(day.entries.length)}</span>}
    </>
  )
}

function WellbeingBlock({ title, days, onOpen }: { title: string; days: WellbeingDay[]; onOpen: (date: string) => void }) {
  return (
    <>
      <h2>{title}</h2>
      <div className="list">
        {days.map((d) => (
          <button
            type="button"
            className="row"
            key={d.date}
            onClick={() => {
              haptic.tap()
              onOpen(d.date)
            }}
          >
            <div className="grow">
              <div className="title">{capitalize(formatWellbeingDate(d.date))}</div>
              <div className="sub">
                <WellbeingLine day={d} />
              </div>
            </div>
            <IconChevron />
          </button>
        ))}
      </div>
    </>
  )
}

function WorkoutCard({
  w,
  open,
  onToggle,
  wellbeing,
  onWellbeing,
}: {
  w: Workout
  open: boolean
  onToggle: () => void
  wellbeing?: WellbeingDay
  onWellbeing: (date: string) => void
}) {
  const d = new Date(w.startedAt)
  const minutes = w.finishedAt ? Math.round((new Date(w.finishedAt).getTime() - d.getTime()) / 60_000) : null
  return (
    <div className="w-card">
      <button className="w-head" onClick={onToggle}>
        <div className="w-icon">
          {d.getDate()}
          <small>{WEEKDAY_SHORT[w.weekday]}</small>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="title" style={{ fontWeight: 600 }}>
            {capitalize(formatLongDate(w.startedAt))}
          </div>
          <div className="sub hint num">
            {w.exercises.length} упр. · {workoutSetCount(w)} подх. · {formatTonnage(workoutVolume(w))}
            {minutes != null && ` · ${minutes} мин`}
          </div>
        </div>
      </button>
      {wellbeing && (
        <button
          type="button"
          className="wb-line"
          onClick={() => {
            haptic.tap()
            onWellbeing(wellbeing.date)
          }}
        >
          <span className="grow">
            <span className="wb-line-label">Самочувствие</span>
            <WellbeingLine day={wellbeing} />
          </span>
          <IconChevron />
        </button>
      )}
      {open && (
        <div className="w-body">
          {w.exercises.map((ex) => (
            <div className="w-ex" key={ex.name}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ fontWeight: 500 }}>{capitalize(ex.name)}</span>
                <span className="hint num" style={{ whiteSpace: 'nowrap' }}>
                  {formatTonnage(exerciseVolume(ex))}
                </span>
              </div>
              <div className="w-sets">
                {ex.sets.map((s, i) => (
                  <span className="w-set" key={i}>
                    {formatKg(s.weight ?? 0)} × {s.reps ?? '—'}
                  </span>
                ))}
              </div>
            </div>
          ))}
          <button
            className="btn danger"
            onClick={async () => {
              if (await confirm('Удалить эту тренировку из истории?')) actions.deleteWorkout(w.id)
            }}
          >
            Удалить тренировку
          </button>
        </div>
      )}
    </div>
  )
}
