import { useEffect, useMemo, useState } from 'react'
import { DropBadge, IntensityBadge } from '../components/Badges'
import { ExercisePicker } from '../components/ExercisePicker'
import { PrescriptionSheet } from '../components/PrescriptionSheet'
import { ScopeSheet } from '../components/ScopeSheet'
import { Sheet } from '../components/Sheet'
import { IconPlus } from '../components/icons'
import { capitalize, dayFocus, findProgram, formatPrescription, getDay, isDropset, plural, WEEKDAY_LONG, type ProgramDay } from '../program'
import {
  addItem,
  draftFromDay,
  draftMatchesDay,
  draftOps,
  findEditedDay,
  isTempKey,
  itemChange,
  LIMITS,
  moveItem,
  normalizeName,
  prescriptionOf,
  rebaseDraft,
  removeItem,
  replaceItem,
  rxOf,
  sameRx,
  setRx,
  validateDraft,
  type Draft,
  type EditOutcome,
  type ItemKey,
} from '../programEdit'
import { useStore } from '../store'
import { confirm, haptic, setClosingConfirmation } from '../telegram'

type Picker = { kind: 'add' } | { kind: 'replace'; key: ItemKey }

const SAVED_ALREADY = 'Правки уже сохранены'

function useOnline(): boolean {
  const [online, setOnline] = useState(() => typeof navigator === 'undefined' || navigator.onLine !== false)
  useEffect(() => {
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    return () => {
      window.removeEventListener('online', on)
      window.removeEventListener('offline', off)
    }
  }, [])
  return online
}

/**
 * The day editor (spec «Экраны» 2): the day's exercises with ↑/↓, a card per exercise, «Добавить
 * упражнение», a local draft saved as ops after choosing the weeks (ScopeSheet). The first save of a
 * template creates the owner's copy. Server mode only; offline the draft can be edited but not saved.
 */
export function ProgramEdit({
  week,
  weekday,
  onClose,
  onSaved,
}: {
  week: number
  weekday: number
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const { programId, mode, history } = useStore()
  const program = findProgram(programId)
  const online = useOnline()

  // The program the editor was opened on. It may become its copy (the first save, or one made on another
  // device); any other program (409 not_active, a switch elsewhere) is not this draft's: saving is blocked.
  const [opened] = useState(programId)
  const switched = programId !== opened && program?.basedOn !== opened
  // The day the draft is based on and its program; a newer one (409, a live update) is taken over with rebaseDraft.
  const [base, setBase] = useState<ProgramDay | undefined>(() => (program ? getDay(program, week, weekday) : undefined))
  const [baseFrom, setBaseFrom] = useState(programId)
  const [draft, setDraft] = useState<Draft>(() => (base ? draftFromDay(base) : []))
  // The edited day in the current program, followed by its id: a move_day (the chat, another device) may
  // have put it on another weekday and another day on the one it was opened on. Ops name its weekday now.
  const at = base?.weekday ?? weekday
  const day = program ? findEditedDay(program, week, base, at) : undefined
  const [notice, setNotice] = useState<string | null>(null)
  const [open, setOpen] = useState<ItemKey | null>(null)
  const [picker, setPicker] = useState<Picker | null>(null)
  const [scope, setScope] = useState(false)

  useEffect(() => {
    if (!day || day === base || switched) return
    const edited = !!base && draftOps(week, base.weekday, base, draft).length > 0
    if (edited && draftMatchesDay(day, draft)) {
      setNotice(SAVED_ALREADY)
      setDraft(draftFromDay(day))
    } else {
      const rebased = base ? rebaseDraft(base, day, draft, week, baseFrom !== programId) : null
      if (edited && !rebased) setNotice('Программа изменилась, и часть правок к ней уже не подходит. Начни заново с новой версии.')
      setDraft(rebased ?? draftFromDay(day))
    }
    setBase(day)
    setBaseFrom(programId)
    setOpen(null)
    // Only a new day from the store triggers this; draft and base are read at that moment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day, switched])

  const ops = useMemo(() => (base ? draftOps(week, base.weekday, base, draft) : []), [base, draft, week])

  // Unsaved edits: Telegram asks before the Mini App is closed by a swipe or ✕.
  const dirty = ops.length > 0
  useEffect(() => {
    setClosingConfirmation(dirty)
    return () => setClosingConfirmation(false)
  }, [dirty])

  const check = useMemo(() => (base ? validateDraft(base, draft) : null), [base, draft])
  const done = useMemo(() => new Set(history.flatMap((w) => w.exercises.map((e) => normalizeName(e.name)))), [history])

  const editable = mode === 'server' && program?.version != null && !!base && base.exercises.every((e) => e.id != null)
  const blocked = switched
    ? 'Программа сменилась — закрой редактор'
    : mode !== 'server'
      ? 'Редактор работает в дневнике из Telegram'
      : !editable
        ? 'Программа ещё не загрузилась с сервера. Открой редактор чуть позже'
        : !online
          ? 'Нет связи: правки можно набросать, сохранить — когда появится сеть'
          : null

  const close = async () => {
    if (ops.length && !(await confirm('Выйти без сохранения? Правки пропадут.'))) return
    onClose()
  }

  const finish = (out: EditOutcome) => {
    setScope(false)
    if (out.kind === 'saved') {
      haptic.success()
      onSaved(out.switchedFrom ? 'Теперь активна твоя версия программы' : 'Программа сохранена')
      onClose()
    } else if (out.kind === 'conflict') {
      // The store has cached the server's program (switched to it if it is the copy) before answering.
      const p = out.program ? findProgram(out.program.id) : undefined
      const now = p ? findEditedDay(p, week, base, at) : undefined
      if (now && draftMatchesDay(now, draft)) {
        haptic.success()
        setNotice(SAVED_ALREADY)
        return
      }
      haptic.error()
      setNotice('Программа изменилась на другом устройстве. Проверь правки и сохрани ещё раз.')
    } else if (out.kind === 'not_active') {
      haptic.error()
      setNotice('Активная программа сменилась. Закрой редактор и открой его заново.')
    }
  }

  // Switched: the draft stays on screen (its own day), nothing goes to the other program.
  const shown = switched ? undefined : program
  const view = switched ? base : day
  if (!view || !base || (!switched && !program)) {
    return (
      <Sheet onClose={onClose} className="tall">
        <div className="empty">{switched ? 'Программа сменилась — закрой редактор' : 'Этого дня в программе больше нет'}</div>
      </Sheet>
    )
  }

  const item = open != null ? draft.find((i) => i.key === open) : undefined
  const baseOf = (key: ItemKey) => (isTempKey(key) ? undefined : base.exercises.find((e) => e.id === key))

  return (
    <Sheet onClose={() => void close()} className="tall editor">
      <div className="editor-head">
        <div className="grow">
          <div className="eyebrow">{shown?.editable ? 'Моя программа' : 'Редактор'}</div>
          <h1 style={{ fontSize: 22 }}>
            Неделя {week} · {WEEKDAY_LONG[view.weekday]}
          </h1>
          <div className="hint">{dayFocus(view)}</div>
        </div>
        <button className="btn ghost editor-close" onClick={() => void close()}>
          Закрыть
        </button>
      </div>
      {shown && !shown.editable && (
        <div className="card notice">Сохранение создаст твою копию «{shown.name} · моя». Оригинал останется, история сохранится.</div>
      )}
      {notice && <div className="card notice warn">{notice}</div>}
      {blocked && <div className="card notice">{blocked}</div>}

      <div className="list">
        {draft.map((it, i) => {
          const ch = itemChange(base, it)
          const err = check?.items[String(it.key)]
          const p = prescriptionOf(it.rx)
          return (
            <div className="row edit-row" key={String(it.key)}>
              <button
                className="grow edit-open"
                onClick={() => {
                  haptic.tap()
                  setOpen(it.key)
                }}
              >
                <div className="title">
                  <span className="muted num">{i + 1}. </span>
                  {capitalize(it.name)}
                </div>
                <div className="ex-meta">
                  <span className="ex-target num">{formatPrescription(p)}</span>
                  <IntensityBadge value={it.rx.intensity} />
                  {isDropset(p) && <DropBadge />}
                  {ch.added && <span className="badge mark">новое</span>}
                  {ch.replaced && <span className="badge mark">замена</span>}
                  {ch.prescribed && !ch.added && <span className="badge mark">изменено</span>}
                </div>
                {err && <div className="field-error">{err}</div>}
              </button>
              <div className="move">
                <button
                  className="icon-btn"
                  aria-label="Выше"
                  disabled={i === 0}
                  onClick={() => {
                    haptic.select()
                    setDraft(moveItem(draft, i, -1))
                  }}
                >
                  ↑
                </button>
                <button
                  className="icon-btn"
                  aria-label="Ниже"
                  disabled={i === draft.length - 1}
                  onClick={() => {
                    haptic.select()
                    setDraft(moveItem(draft, i, 1))
                  }}
                >
                  ↓
                </button>
              </div>
            </div>
          )
        })}
        <button
          className="row"
          disabled={draft.length >= LIMITS.dayMax}
          onClick={() => {
            haptic.tap()
            setPicker({ kind: 'add' })
          }}
        >
          <div className="grow" style={{ color: 'var(--link)', fontWeight: 500 }}>
            Добавить упражнение
          </div>
          <IconPlus />
        </button>
      </div>
      {check?.day && <div className="field-error">{check.day}</div>}

      <div className="sheet-footer">
        {ops.length > 0 && (
          <button
            className="btn ghost"
            onClick={async () => {
              if (await confirm('Отменить все правки этого дня?')) {
                haptic.tap()
                setDraft(draftFromDay(base))
                setNotice(null)
              }
            }}
          >
            Отменить правки
          </button>
        )}
        <button
          className="btn"
          disabled={!ops.length || !check?.ok || !!blocked}
          onClick={() => {
            haptic.tap()
            setScope(true)
          }}
        >
          {ops.length ? `Сохранить (${plural(ops.length, ['изменение', 'изменения', 'изменений'])})` : 'Сохранить'}
        </button>
      </div>

      {item && (
        <PrescriptionSheet
          key={String(item.key)}
          item={item}
          was={(() => {
            const e = baseOf(item.key)
            return e && !sameRx(rxOf(e), item.rx) ? formatPrescription(e.prescription) : null
          })()}
          isNew={isTempKey(item.key)}
          hasHistory={done.has(normalizeName(item.name))}
          canRemove={draft.length > 1}
          onChange={(rx) => setDraft((d) => setRx(d, item.key, rx))}
          onReplace={() => setPicker({ kind: 'replace', key: item.key })}
          onRemove={() => {
            setDraft((d) => removeItem(d, item.key))
            setOpen(null)
          }}
          onClose={() => setOpen(null)}
        />
      )}

      {picker && shown && (
        <ExercisePicker
          program={shown}
          title={picker.kind === 'add' ? 'Добавить упражнение' : 'Заменить на'}
          dayNames={draft.map((i) => i.name)}
          current={picker.kind === 'replace' ? draft.find((i) => i.key === picker.key)?.name : undefined}
          onClose={() => setPicker(null)}
          onPick={(name) => {
            if (picker.kind === 'add') {
              const res = addItem(draft, name)
              setDraft(res.draft)
              setOpen(res.key)
            } else {
              setDraft(replaceItem(draft, picker.key, name))
            }
            setPicker(null)
          }}
        />
      )}

      {scope && shown && (
        <ScopeSheet program={shown} week={week} base={base} draft={draft} onClose={() => setScope(false)} onDone={finish} />
      )}
    </Sheet>
  )
}
