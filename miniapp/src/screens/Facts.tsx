import { useEffect, useState } from 'react'
import {
  ApiError,
  createFact,
  deleteFact,
  FACT_TEXT_MAX,
  FACTS_ACTIVE_MAX,
  getFacts,
  inTelegram,
  updateFact,
  type Fact,
  type FactCategory,
} from '../api'
import { IconPlus } from '../components/icons'
import { Sheet } from '../components/Sheet'
import { Switch } from '../components/Switch'
import {
  activeCount,
  categoryLabel,
  cleanFactText,
  duplicateFactText,
  FACT_CATEGORIES,
  factTextError,
  knownCategory,
  sortFacts,
} from '../facts'
import { confirm, haptic } from '../telegram'
import { useRemote } from '../useRemote'

const LIMIT_TEXT = `Активных фактов уже ${FACTS_ACTIVE_MAX}. Выключи или удали ненужные.`

/** "Бот помнит": long-lived facts from the chat that the bot mixes into parsing and advice. */
export function Facts() {
  const r = useRemote<Fact[]>('facts', getFacts)
  // Local copy so toggles and edits show at once; re-seeded whenever the server list arrives.
  const [items, setItems] = useState<Fact[] | null>(null)
  const [editing, setEditing] = useState<Fact | 'new' | null>(null)
  const [pending, setPending] = useState<ReadonlySet<number>>(new Set())
  const [toggleError, setToggleError] = useState<string | null>(null)

  useEffect(() => {
    if (r.data) setItems(sortFacts(r.data))
  }, [r.data])

  const upsert = (f: Fact) => setItems((list) => sortFacts([...(list ?? []).filter((x) => x.id !== f.id), f]))
  const drop = (id: number) => setItems((list) => list && list.filter((x) => x.id !== id))

  async function toggle(f: Fact) {
    if (pending.has(f.id)) return
    haptic.select()
    setToggleError(null)
    setPending((p) => new Set(p).add(f.id))
    const active = !f.active
    upsert({ ...f, active })
    try {
      upsert(await updateFact(f.id, { active }))
    } catch (err) {
      haptic.error()
      const status = err instanceof ApiError ? err.status : null
      // 404: deleted elsewhere; anything else: roll back.
      if (status === 404) drop(f.id)
      else {
        upsert(f)
        setToggleError(status === 409 ? LIMIT_TEXT : 'Не удалось переключить факт. Попробуй ещё раз.')
      }
    } finally {
      setPending((p) => {
        const next = new Set(p)
        next.delete(f.id)
        return next
      })
    }
  }

  const full = items != null && activeCount(items) >= FACTS_ACTIVE_MAX

  return (
    <>
      <h2>Бот помнит</h2>
      {r.error != null && !items ? (
        <FactsError error={r.error} onRetry={r.reload} />
      ) : !items ? (
        <div className="card hint">Загрузка…</div>
      ) : (
        <>
          {items.length ? (
            <div className="list">
              {items.map((f) => (
                <div className={`row fact ${f.active ? '' : 'off'}`} key={f.id}>
                  <button className="fact-open" onClick={() => setEditing(f)}>
                    <span className="fact-text">{f.text}</span>
                    <span className={`badge fact-cat ${knownCategory(f.category)}`}>{categoryLabel(f.category)}</span>
                  </button>
                  <Switch
                    on={f.active}
                    disabled={pending.has(f.id)}
                    label={`Учитывать: ${f.text}`}
                    onToggle={() => toggle(f)}
                  />
                </div>
              ))}
            </div>
          ) : (
            <div className="card hint">Пока ничего. Например: «не ем творог», «самса у нас 150 г», «по пятницам тренируюсь утром».</div>
          )}
          {toggleError && (
            <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
              {toggleError}
            </p>
          )}
          <div className="spacer" />
          <button
            className="btn secondary"
            disabled={full}
            onClick={() => {
              haptic.tap()
              setEditing('new')
            }}
          >
            <IconPlus />
            Добавить
          </button>
          <p className="hint" style={{ margin: '8px 4px 0' }}>
            {full && `${LIMIT_TEXT} `}
            Факты учитываются при разборе записей и в советах. Добавить можно и в чате: «запомни: …»
          </p>
        </>
      )}

      {editing && (
        <FactSheet
          fact={editing === 'new' ? null : editing}
          onSaved={(f) => {
            upsert(f)
            setEditing(null)
          }}
          onExisting={upsert}
          onGone={(id) => {
            drop(id)
            setEditing(null)
          }}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  )
}

function FactsError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : null
  if (status === 401 || status === 403)
    return (
      <div className="card hint">
        {inTelegram
          ? 'Не получилось войти. Закрой дневник и открой его заново из бота.'
          : 'Факты хранятся на сервере бота. Открой дневник из бота в Telegram, чтобы их посмотреть.'}
      </div>
    )
  return (
    <div className="card">
      <div className="hint">{status === 404 ? 'Сервер пока не умеет запоминать факты.' : 'Не удалось загрузить факты.'}</div>
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

function saveError(err: unknown): string {
  const status = err instanceof ApiError ? err.status : null
  if (status === 401 || status === 403) return 'Не получилось войти. Закрой дневник и открой его заново из бота.'
  if (status === 409) return LIMIT_TEXT
  if (status != null && status >= 400 && status < 500) return 'Сервер не принял факт. Проверь текст.'
  return 'Не удалось сохранить. Проверь интернет и попробуй ещё раз.'
}

function FactSheet({
  fact,
  onSaved,
  onExisting,
  onGone,
  onClose,
}: {
  fact: Fact | null
  onSaved: (f: Fact) => void
  /** POST hit an existing fact: show it in the list, but keep the sheet open with the explanation. */
  onExisting: (f: Fact) => void
  onGone: (id: number) => void
  onClose: () => void
}) {
  const [text, setText] = useState(fact?.text ?? '')
  const [category, setCategory] = useState<FactCategory>(knownCategory(fact?.category ?? 'other'))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const textError = factTextError(text, FACT_TEXT_MAX)
  const clean = cleanFactText(text)
  const changed = !fact || clean !== fact.text || category !== knownCategory(fact.category)
  const canSave = !textError && changed && !busy

  async function save() {
    if (!canSave) return
    setBusy(true)
    setError(null)
    try {
      if (fact) {
        const saved = await updateFact(fact.id, { text: clean, category })
        haptic.success()
        return onSaved(saved)
      }
      const res = await createFact({ text: clean, category })
      if (!res.created) {
        // The server kept the old fact as it was: no success, say what it really is.
        haptic.error()
        onExisting(res.fact)
        setError(duplicateFactText(res.fact))
        setBusy(false)
        return
      }
      haptic.success()
      onSaved(res.fact)
    } catch (err) {
      haptic.error()
      if (fact && err instanceof ApiError && err.status === 404) return onGone(fact.id)
      if (fact && err instanceof ApiError && err.status === 422) {
        setError(duplicateFactText(null))
        setBusy(false)
        return
      }
      setError(saveError(err))
      setBusy(false)
    }
  }

  async function remove() {
    if (!fact || busy) return
    if (!(await confirm(`Забыть «${fact.text}»?`))) return
    setBusy(true)
    setError(null)
    try {
      await deleteFact(fact.id)
    } catch (err) {
      // 404: already gone, which is what we wanted.
      if (!(err instanceof ApiError && err.status === 404)) {
        haptic.error()
        setError('Не удалось удалить. Проверь интернет и попробуй ещё раз.')
        setBusy(false)
        return
      }
    }
    haptic.success()
    onGone(fact.id)
  }

  return (
    <Sheet onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          save()
        }}
      >
        <h1 style={{ fontSize: 22 }}>{fact ? 'Факт' : 'Новый факт'}</h1>
        <div className="spacer" />
        <div className="list">
          <label className="row profile-stack">
            <div className="title">Что запомнить</div>
            <textarea
              className="field profile-about-field"
              value={text}
              maxLength={FACT_TEXT_MAX}
              rows={2}
              autoFocus={!fact}
              placeholder="Например, «не ем творог»"
              enterKeyHint="done"
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                // One-line facts: Enter saves instead of adding a line break.
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  save()
                }
              }}
            />
            {text.length > FACT_TEXT_MAX - 40 && (
              <div className="hint num">
                {text.length} / {FACT_TEXT_MAX}
              </div>
            )}
          </label>
          <div className="row profile-stack">
            <div className="title">Категория</div>
            <div className="fact-cat-picker" role="group" aria-label="Категория">
              {FACT_CATEGORIES.map((c) => (
                <button
                  type="button"
                  key={c.key}
                  className={category === c.key ? 'active' : ''}
                  aria-pressed={category === c.key}
                  onClick={() => {
                    haptic.select()
                    setCategory(c.key)
                  }}
                >
                  {c.label}
                </button>
              ))}
            </div>
          </div>
        </div>
        {error && (
          <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
            {error}
          </p>
        )}
        <div className="spacer" />
        <button type="submit" className="btn" disabled={!canSave}>
          {busy ? 'Сохраняю…' : fact ? 'Сохранить' : 'Добавить'}
        </button>
        {fact && (
          <button type="button" className="btn danger" style={{ marginTop: 6 }} disabled={busy} onClick={remove}>
            Забыть факт
          </button>
        )}
      </form>
    </Sheet>
  )
}
