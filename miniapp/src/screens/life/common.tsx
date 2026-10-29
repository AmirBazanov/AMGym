import { useState } from 'react'
import { ApiError, createLife, deleteLife, inTelegram, updateLife } from '../../api'
import { Sheet } from '../../components/Sheet'
import {
  errorDetail,
  formBody,
  formValues,
  LIFE_FORMS,
  WEEKDAYS_SHORT,
  type DoneKind,
  type FieldSpec,
  type FormValues,
  type LifeItems,
  type LifeKind,
  type LifeProject,
  type LifeView,
} from '../../life'
import { confirm, haptic } from '../../telegram'

/** What every «Дела» tab gets from the section. */
export interface LifeCtx {
  view: LifeView
  /** Reload the view (after a change; live updates do it for changes from the chat). */
  reload: () => void
  /** Show an error line above the tab. */
  fail: (text: string) => void
  /** Open the edit sheet of a new item (with defaults) or of an existing one. */
  edit: (req: EditRequest) => void
  /** Mark a task, plan, deadline or chore done (or not): shown at once, then reloaded. */
  toggleDone: (kind: DoneKind, id: number, done: boolean) => void
}

export interface EditRequest {
  kind: LifeKind
  item?: object | null
  defaults?: FormValues
}

const LOGIN_TEXT = inTelegram
  ? 'Не получилось войти. Закрой приложение и открой его заново из бота.'
  : 'Вход устарел. Напиши боту /web, он пришлёт новую ссылку.'

/** Russian text of a failed «Дела» request. */
export function lifeErrorText(err: unknown, what = 'сохранить'): string {
  const status = err instanceof ApiError ? err.status : null
  if (status === 401 || status === 403) return LOGIN_TEXT
  if (status === 422) return (err instanceof ApiError && errorDetail(err.message)) || 'Сервер не принял. Проверь поля.'
  if (status === 404) return 'Этого уже нет: обнови экран.'
  return `Не удалось ${what}. Проверь интернет и попробуй ещё раз.`
}

/** Outside Telegram without the /web token: nothing to show, only how to log in. */
export function LifeLogin({ expired }: { expired?: boolean }) {
  return (
    <div className="empty">
      <div className="big">Дела</div>
      <p>
        {expired ? 'Вход устарел. ' : ''}
        Открой /web в боте: он пришлёт ссылку для входа с компьютера.
      </p>
      <p className="hint">Ссылка действует 30 дней. В Telegram «Дела» открываются без неё.</p>
    </div>
  )
}

/** Load error of the view, with retry; a lost login shows the login screen instead. */
export function LifeLoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : null
  if ((status === 401 || status === 403) && !inTelegram) return <LifeLogin expired />
  return (
    <div className="card">
      <div className="hint">
        {status === 401 || status === 403
          ? LOGIN_TEXT
          : status === 404
            ? 'Сервер пока не умеет «Дела».'
            : 'Не удалось загрузить дела.'}
      </div>
      <div className="spacer" />
      <button
        className="btn secondary"
        onClick={() => {
          haptic.tap()
          onRetry()
        }}
      >
        Повторить
      </button>
    </div>
  )
}

/** The round check button of tasks, plans and deadlines (large target: 52x46). */
export function DoneCheck({ done, label, onToggle }: { done: boolean; label: string; onToggle: () => void }) {
  return (
    <button className={`check ${done ? 'on' : ''}`} aria-pressed={done} aria-label={label} onClick={onToggle}>
      <svg width={22} height={22} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.6} strokeLinecap="round" strokeLinejoin="round">
        <path d="M5 12.5l4.5 4.5L19 7.5" />
      </svg>
    </button>
  )
}

const SHEET_TITLE: Record<LifeKind, [string, string]> = {
  slots: ['Новое в расписании', 'Расписание'],
  events: ['Новый план', 'План'],
  deadlines: ['Новый дедлайн', 'Дедлайн'],
  tasks: ['Новая задача', 'Задача'],
  chores: ['Новое дело по дому', 'Дело по дому'],
  projects: ['Новый проект', 'Проект'],
  notes: ['Новая заметка', 'Заметка'],
}

/**
 * One sheet for adding and editing any kind: fields from life.LIFE_FORMS, the body from life.formBody
 * (an edit sends only the changed fields), deleting asks first.
 */
export function LifeEditSheet<K extends LifeKind>({
  kind,
  item,
  defaults,
  projects,
  onSaved,
  onClose,
}: {
  kind: K
  item: object | null
  defaults?: FormValues
  projects: readonly LifeProject[]
  onSaved: (saved: LifeItems[K] | null) => void
  onClose: () => void
}) {
  const [values, setValues] = useState<FormValues>(() => formValues(kind, item, defaults))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = item ? ((item as { id?: unknown }).id as number) : null

  const { body, error: formError } = formBody(kind, values, item)
  const changed = !item || Object.keys(body).length > 0
  const set = (key: string, v: string) => setValues((s) => ({ ...s, [key]: v }))

  async function save() {
    if (busy || !changed) return
    if (formError) {
      haptic.error()
      setError(formError)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const saved = id == null ? await createLife(kind, body) : await updateLife(kind, id, body)
      haptic.success()
      onSaved(saved)
    } catch (err) {
      haptic.error()
      setError(lifeErrorText(err))
      setBusy(false)
    }
  }

  async function remove() {
    if (id == null || busy) return
    const name = values.title || values.name || ''
    if (!(await confirm(`Удалить «${name}»?`))) return
    setBusy(true)
    setError(null)
    try {
      await deleteLife(kind, id)
    } catch (err) {
      // 404: already gone, which is what we wanted.
      if (!(err instanceof ApiError && err.status === 404)) {
        haptic.error()
        setError(lifeErrorText(err, 'удалить'))
        setBusy(false)
        return
      }
    }
    haptic.success()
    onSaved(null)
  }

  return (
    <Sheet onClose={onClose} className={kind === 'notes' ? 'tall' : undefined}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void save()
        }}
      >
        <h1 style={{ fontSize: 22 }}>{SHEET_TITLE[kind][item ? 1 : 0]}</h1>
        <div className="spacer" />
        <div className="list">
          {LIFE_FORMS[kind].map((f) => (
            <Field key={f.key} spec={f} value={values[f.key] ?? ''} projects={projects} autoFocus={!item && f.key === LIFE_FORMS[kind][0]?.key} onChange={(v) => set(f.key, v)} />
          ))}
        </div>
        {error && <p className="field-error">{error}</p>}
        <div className="spacer" />
        <button type="submit" className="btn" disabled={busy || !changed}>
          {busy ? 'Сохраняю…' : item ? 'Сохранить' : 'Добавить'}
        </button>
        {item && (
          <button type="button" className="btn danger" style={{ marginTop: 6 }} disabled={busy} onClick={remove}>
            Удалить
          </button>
        )}
      </form>
    </Sheet>
  )
}

function Field({
  spec,
  value,
  projects,
  autoFocus,
  onChange,
}: {
  spec: FieldSpec
  value: string
  projects: readonly LifeProject[]
  autoFocus: boolean
  onChange: (v: string) => void
}) {
  switch (spec.type) {
    case 'time':
    case 'date':
    case 'int':
      return (
        <label className="row">
          <div className="grow title">{spec.label}</div>
          <input
            type={spec.type === 'int' ? 'text' : spec.type}
            inputMode={spec.type === 'int' ? 'numeric' : undefined}
            className={`field life-short-field ${spec.type === 'date' ? 'date' : ''}`}
            value={value}
            placeholder={spec.placeholder}
            required={spec.required}
            onChange={(e) => onChange(spec.type === 'time' ? e.target.value.slice(0, 5) : e.target.value)}
          />
        </label>
      )
    case 'weekday':
      return (
        <div className="row profile-stack">
          <div className="title">{spec.label}</div>
          <div className="weekday-picker" role="group" aria-label={spec.label}>
            {WEEKDAYS_SHORT.map((d, i) => (
              <button
                type="button"
                key={d}
                className={value === String(i + 1) ? 'active' : ''}
                aria-pressed={value === String(i + 1)}
                onClick={() => {
                  haptic.select()
                  onChange(String(i + 1))
                }}
              >
                {d}
              </button>
            ))}
          </div>
        </div>
      )
    case 'choice':
      return (
        <div className="row profile-stack">
          <div className="title">{spec.label}</div>
          <div className="segmented profile-goal" role="group" aria-label={spec.label}>
            {spec.options?.map((o) => (
              <button
                type="button"
                key={o.value}
                className={value === o.value ? 'active' : ''}
                aria-pressed={value === o.value}
                onClick={() => {
                  haptic.select()
                  onChange(o.value)
                }}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
      )
    case 'project':
      return (
        <label className="row profile-stack">
          <div className="title">{spec.label}</div>
          <select className="select life-select" value={value} onChange={(e) => onChange(e.target.value)}>
            <option value="">Без проекта</option>
            {projects.map((p) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      )
    case 'textarea':
      return (
        <label className="row profile-stack">
          <div className="title">{spec.label}</div>
          <textarea
            className="field profile-about-field life-textarea"
            value={value}
            rows={spec.key === 'body' ? 12 : 3}
            placeholder={spec.placeholder}
            onChange={(e) => onChange(e.target.value)}
          />
        </label>
      )
    default:
      return (
        <label className="row profile-stack">
          <div className="title">{spec.label}</div>
          <input
            type="text"
            className="field life-text-field"
            value={value}
            autoFocus={autoFocus}
            placeholder={spec.placeholder}
            required={spec.required}
            enterKeyHint="done"
            onChange={(e) => onChange(e.target.value)}
          />
        </label>
      )
  }
}
