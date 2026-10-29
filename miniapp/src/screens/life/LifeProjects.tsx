import { IconPlus } from '../../components/icons'
import { openTasks, PROJECT_STATUS_LABEL, projectGroups, safeHref, type LifeProject } from '../../life'
import { haptic } from '../../telegram'
import { type LifeCtx } from './common'
import { TaskRow } from './LifeTasks'

/** «Проекты» by kind (Работа, Обучение, Своё): status, next step, link and the project's open tasks. */
export function LifeProjects({ ctx }: { ctx: LifeCtx }) {
  const groups = projectGroups(ctx.view.projects)
  return (
    <>
      {groups.length ? (
        groups.map((g) => (
          <section key={g.kind}>
            <h2>{g.label}</h2>
            <div className="stack">
              {g.projects.map((p) => (
                <ProjectCard key={p.id} ctx={ctx} project={p} />
              ))}
            </div>
          </section>
        ))
      ) : (
        <div className="card hint">Проектов пока нет.</div>
      )}
      <div className="spacer" />
      <button
        className="btn secondary"
        onClick={() => {
          haptic.tap()
          ctx.edit({ kind: 'projects' })
        }}
      >
        <IconPlus />
        Проект
      </button>
    </>
  )
}

function ProjectCard({ ctx, project: p }: { ctx: LifeCtx; project: LifeProject }) {
  const tasks = openTasks(ctx.view.tasks.filter((t) => t.projectId === p.id))
  const href = p.link ? safeHref(p.link) : null
  return (
    <div className={`card life-project ${p.status}`}>
      <button className="life-project-head" onClick={() => ctx.edit({ kind: 'projects', item: p })}>
        <span className="title">{p.name}</span>
        <span className={`badge life-status ${p.status}`}>{PROJECT_STATUS_LABEL[p.status]}</span>
      </button>
      {p.nextStep && (
        <div className="life-next">
          <span className="muted">Дальше: </span>
          {p.nextStep}
        </div>
      )}
      {p.description && <div className="hint life-desc">{p.description}</div>}
      {href && (
        <a className="life-link" href={href} target="_blank" rel="noopener noreferrer">
          {href.replace(/^https?:\/\//, '')}
        </a>
      )}
      {tasks.length > 0 && (
        <div className="list life-project-tasks">
          {tasks.map((t) => (
            <TaskRow key={t.id} ctx={ctx} task={t} />
          ))}
        </div>
      )}
      <button
        className="btn ghost life-add-task"
        onClick={() => {
          haptic.tap()
          ctx.edit({ kind: 'tasks', defaults: { projectId: String(p.id) } })
        }}
      >
        <IconPlus />
        Задача в проект
      </button>
    </div>
  )
}
