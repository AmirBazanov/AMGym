import { useEffect, useState } from 'react'
import { getLifeView, hasAuth, setLifeDone } from '../../api'
import { ErrorBoundary } from '../../components/ErrorBoundary'
import { IconCalendar, IconNotes, IconProjects, IconSun, IconTasks } from '../../components/icons'
import { markDone, type DoneKind, type LifeView } from '../../life'
import { emitRemoteRefresh } from '../../liveCore'
import { haptic } from '../../telegram'
import { useRemote } from '../../useRemote'
import { LifeEditSheet, LifeLoadError, LifeLogin, lifeErrorText, type EditRequest, type LifeCtx } from './common'
import { LifeNotes } from './LifeNotes'
import { LifeProjects } from './LifeProjects'
import { LifeSchedule } from './LifeSchedule'
import { LifeTasks } from './LifeTasks'
import { LifeToday } from './LifeToday'

export type LifeTab = 'today' | 'schedule' | 'tasks' | 'projects' | 'notes'

const TABS: { key: LifeTab; label: string; Icon: () => React.JSX.Element }[] = [
  { key: 'today', label: 'Сегодня', Icon: IconSun },
  { key: 'schedule', label: 'Расписание', Icon: IconCalendar },
  { key: 'tasks', label: 'Задачи', Icon: IconTasks },
  { key: 'projects', label: 'Проекты', Icon: IconProjects },
  { key: 'notes', label: 'Заметки', Icon: IconNotes },
]

/** After a change here: every «Дела» view reloads (the section's and an open week or note), as on live "life". */
const refresh = () => emitRemoteRefresh(['life'])

function initialTab(): LifeTab {
  const t = new URLSearchParams(location.search).get('tab')
  return TABS.some((x) => x.key === t) ? (t as LifeTab) : 'today'
}

/** «Дела»: schedule, plans, deadlines, tasks, chores, projects and notes from /api/life. */
export function LifeSection() {
  // Outside Telegram without the /web token there is nobody to load data for.
  return hasAuth ? <LifeTabs /> : <LifeLogin />
}

function LifeTabs() {
  const [tab, setTab] = useState<LifeTab>(initialTab)
  const r = useRemote<LifeView>('life:view', () => getLifeView(), ['life'])
  // Local copy so checks show at once; re-seeded whenever the server view arrives.
  const [view, setView] = useState<LifeView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<EditRequest | null>(null)

  useEffect(() => {
    if (r.data) setView(r.data)
  }, [r.data])

  async function toggleDone(kind: DoneKind, id: number, done: boolean) {
    haptic.select()
    setError(null)
    setView((v) => v && markDone(v, kind, id, done))
    try {
      await setLifeDone(kind, id, done)
      if (done) haptic.success()
    } catch (err) {
      haptic.error()
      setError(lifeErrorText(err, 'отметить'))
    }
    refresh()
  }

  const ctx: LifeCtx | null = view && {
    view,
    reload: refresh,
    fail: setError,
    edit: (req) => {
      setError(null)
      setEditing(req)
    },
    toggleDone,
  }

  return (
    <>
      {error && <p className="field-error life-error">{error}</p>}
      {!ctx ? (
        r.error != null ? (
          <LifeLoadError error={r.error} onRetry={r.reload} />
        ) : (
          <div className="card hint">Загрузка…</div>
        )
      ) : (
        <ErrorBoundary key={tab}>
          {tab === 'today' && <LifeToday ctx={ctx} openTasks={() => setTab('tasks')} />}
          {tab === 'schedule' && <LifeSchedule ctx={ctx} />}
          {tab === 'tasks' && <LifeTasks ctx={ctx} />}
          {tab === 'projects' && <LifeProjects ctx={ctx} />}
          {tab === 'notes' && <LifeNotes ctx={ctx} />}
        </ErrorBoundary>
      )}
      {editing && view && (
        <LifeEditSheet
          kind={editing.kind}
          item={editing.item ?? null}
          defaults={editing.defaults}
          projects={view.projects}
          onSaved={() => {
            setEditing(null)
            refresh()
          }}
          onClose={() => setEditing(null)}
        />
      )}
      <nav className="tabbar">
        <div className="tabbar-inner">
          {TABS.map(({ key, label, Icon }) => (
            <button
              key={key}
              className={`tab ${tab === key ? 'active' : ''}`}
              onClick={() => {
                if (tab !== key) haptic.select()
                setTab(key)
                window.scrollTo({ top: 0 })
              }}
            >
              <Icon />
              {label}
            </button>
          ))}
        </div>
      </nav>
    </>
  )
}
