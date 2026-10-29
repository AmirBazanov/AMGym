import { useState } from 'react'
import { IconPlus } from '../../components/icons'
import { doneTasks, dueLabel, everyLabel, openTasks, sortChores, type LifeTask, type LifeView } from '../../life'
import { haptic } from '../../telegram'
import { DoneCheck, type LifeCtx } from './common'

type Part = 'tasks' | 'home'

function taskSub(t: LifeTask, view: LifeView): string {
  const project = t.projectId != null ? view.projects.find((p) => p.id === t.projectId)?.name : null
  return [t.tag, project].filter(Boolean).join(' · ')
}

export function TaskRow({ ctx, task }: { ctx: LifeCtx; task: LifeTask }) {
  const sub = taskSub(task, ctx.view)
  return (
    <div className={`row life-row ${task.done ? 'done' : ''}`}>
      <DoneCheck done={task.done} label={`Сделано: ${task.title}`} onToggle={() => ctx.toggleDone('tasks', task.id, !task.done)} />
      <button className="life-open" onClick={() => ctx.edit({ kind: 'tasks', item: task })}>
        <span className="grow">
          <span className="title life-title">{task.title}</span>
          {sub && <span className="sub">{sub}</span>}
        </span>
      </button>
    </div>
  )
}

/** «Задачи»: tasks with checks (the done ones of the last week below) and «Дом», the household chores. */
export function LifeTasks({ ctx }: { ctx: LifeCtx }) {
  const { view } = ctx
  const [part, setPart] = useState<Part>('tasks')
  const open = openTasks(view.tasks)
  const done = doneTasks(view.tasks)
  const chores = sortChores(view.chores)

  return (
    <>
      <div className="segmented" role="group" aria-label="Раздел">
        {(
          [
            ['tasks', `Задачи${open.length ? ` · ${open.length}` : ''}`],
            ['home', 'Дом'],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            className={part === key ? 'active' : ''}
            aria-pressed={part === key}
            onClick={() => {
              if (part !== key) haptic.select()
              setPart(key)
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {part === 'tasks' ? (
        <>
          <h2>Открытые</h2>
          {open.length ? (
            <div className="list">
              {open.map((t) => (
                <TaskRow key={t.id} ctx={ctx} task={t} />
              ))}
            </div>
          ) : (
            <div className="card hint">Открытых задач нет.</div>
          )}
          <div className="spacer" />
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
          {done.length > 0 && (
            <>
              <h2>Сделано за неделю</h2>
              <div className="list">
                {done.map((t) => (
                  <TaskRow key={t.id} ctx={ctx} task={t} />
                ))}
              </div>
            </>
          )}
        </>
      ) : (
        <>
          <h2>Дела по дому</h2>
          {chores.length ? (
            <div className="list">
              {chores.map((c) => (
                <div className="row life-row" key={c.id}>
                  <button className="life-open" onClick={() => ctx.edit({ kind: 'chores', item: c })}>
                    <span className="grow">
                      <span className="title life-title">{c.title}</span>
                      <span className={`sub ${c.left < 0 ? 'life-overdue' : ''}`}>
                        {everyLabel(c.everyDays)} · {c.lastDone == null ? 'ещё не отмечено' : dueLabel(c.left)}
                      </span>
                    </span>
                  </button>
                  <button className="btn secondary life-did" disabled={c.left > 0 && c.lastDone === view.today} onClick={() => ctx.toggleDone('chores', c.id, true)}>
                    Сделано
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <div className="card hint">Пока пусто. Например, «Уборка» раз в неделю.</div>
          )}
          <div className="spacer" />
          <button
            className="btn secondary"
            onClick={() => {
              haptic.tap()
              ctx.edit({ kind: 'chores' })
            }}
          >
            <IconPlus />
            Дело по дому
          </button>
          <p className="hint" style={{ margin: '8px 4px 0' }}>
            Отметить можно и в чате: «вынес мусор», «убрался».
          </p>
        </>
      )}
    </>
  )
}
