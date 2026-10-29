import { useState } from 'react'
import { ApiError, deleteLife, getLifeNote } from '../../api'
import { IconChevron, IconPlus } from '../../components/icons'
import { Markdown } from '../../components/Markdown'
import { Sheet } from '../../components/Sheet'
import { dayMonth, sortNotes, type LifeNote, type LifeNoteHead } from '../../life'
import { emitRemoteRefresh } from '../../liveCore'
import { confirm, haptic } from '../../telegram'
import { useRemote } from '../../useRemote'
import { LifeLoadError, lifeErrorText, type LifeCtx } from './common'

function noteSub(n: LifeNoteHead): string {
  return [n.author === 'bot' ? 'от бота' : null, n.tags.length ? n.tags.map((t) => `#${t}`).join(' ') : null, n.updatedAt ? dayMonth(n.updatedAt.slice(0, 10)) : null]
    .filter(Boolean)
    .join(' · ')
}

/** «Заметки»: the latest notes; a note opens as Markdown, with editing and deleting. */
export function LifeNotes({ ctx }: { ctx: LifeCtx }) {
  const notes = sortNotes(ctx.view.notes)
  const [openId, setOpenId] = useState<number | null>(null)

  return (
    <>
      <h2>Заметки</h2>
      {notes.length ? (
        <div className="list">
          {notes.map((n) => (
            <button
              className="row"
              key={n.id}
              onClick={() => {
                haptic.tap()
                setOpenId(n.id)
              }}
            >
              <span className="grow">
                <span className="title life-title" style={{ display: 'block' }}>
                  {n.title}
                </span>
                <span className="sub" style={{ display: 'block' }}>
                  {noteSub(n)}
                </span>
              </span>
              <IconChevron />
            </button>
          ))}
        </div>
      ) : (
        <div className="card hint">Пока пусто. Попроси бота: «составь памятку по epoll» или «запиши заметку: …».</div>
      )}
      <div className="spacer" />
      <button
        className="btn secondary"
        onClick={() => {
          haptic.tap()
          ctx.edit({ kind: 'notes' })
        }}
      >
        <IconPlus />
        Заметка
      </button>
      {openId != null && (
        <NoteSheet
          id={openId}
          onEdit={(note) => {
            setOpenId(null)
            ctx.edit({ kind: 'notes', item: note })
          }}
          onClose={() => setOpenId(null)}
        />
      )}
    </>
  )
}

function NoteSheet({ id, onEdit, onClose }: { id: number; onEdit: (n: LifeNote) => void; onClose: () => void }) {
  const r = useRemote<LifeNote>(`life:note:${id}`, () => getLifeNote(id), ['life'])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const note = r.data

  async function remove() {
    if (!note || busy) return
    if (!(await confirm(`Удалить заметку «${note.title}»?`))) return
    setBusy(true)
    setError(null)
    try {
      await deleteLife('notes', note.id)
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 404)) {
        haptic.error()
        setError(lifeErrorText(err, 'удалить'))
        setBusy(false)
        return
      }
    }
    haptic.success()
    onClose()
    emitRemoteRefresh(['life'])
  }

  return (
    <Sheet onClose={onClose} className="tall">
      {!note ? (
        r.error != null ? (
          r.error instanceof ApiError && r.error.status === 404 ? (
            <div className="card hint">Этой заметки уже нет.</div>
          ) : (
            <LifeLoadError error={r.error} onRetry={r.reload} />
          )
        ) : (
          <div className="card hint">Загрузка…</div>
        )
      ) : (
        <>
          <h1 style={{ fontSize: 22 }}>{note.title}</h1>
          <div className="hint">{noteSub(note)}</div>
          <div className="spacer" />
          <div className="card">
            <Markdown text={note.body} />
          </div>
          {error && <p className="field-error">{error}</p>}
          <div className="spacer" />
          <button
            className="btn"
            disabled={busy}
            onClick={() => {
              haptic.tap()
              onEdit(note)
            }}
          >
            Править
          </button>
          <button className="btn danger" style={{ marginTop: 6 }} disabled={busy} onClick={remove}>
            Удалить
          </button>
        </>
      )}
    </Sheet>
  )
}
