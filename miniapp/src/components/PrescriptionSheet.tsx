import { useState } from 'react'
import { capitalize, formatPrescription, INTENSITY_LABEL, type Intensity } from '../program'
import { defaultDrops, LIMITS, prescriptionOf, rxValid, validateRx, type DraftItem, type Rx } from '../programEdit'
import { confirm, haptic } from '../telegram'
import { NumField } from './NumField'
import { Sheet } from './Sheet'
import { Stepper } from './Stepper'
import { Switch } from './Switch'

const INTENSITIES: [Intensity | null, string][] = [
  ['heavy', INTENSITY_LABEL.heavy],
  ['medium', INTENSITY_LABEL.medium],
  ['light', INTENSITY_LABEL.light],
  [null, '—'],
]

/**
 * The exercise card of the day editor: replace, sets, reps or drops, intensity, remove. Every valid change
 * goes to the draft at once (closing the card keeps it); an invalid one stays here with its error.
 */
export function PrescriptionSheet({
  item,
  was,
  isNew,
  hasHistory,
  canRemove,
  onChange,
  onReplace,
  onRemove,
  onClose,
}: {
  item: DraftItem
  was: string | null // the program's prescription before the edit, when it differs
  isNew: boolean
  hasHistory: boolean
  canRemove: boolean
  onChange: (rx: Rx) => void
  onReplace: () => void
  onRemove: () => void
  onClose: () => void
}) {
  const [rx, setLocal] = useState<Rx>(item.rx)
  // Reps of the plain sets, kept while the dropset switch is on, so turning it off brings them back.
  const [plain, setPlain] = useState({ repsMin: item.rx.repsMin ?? 10, repsMax: item.rx.repsMax ?? item.rx.repsMin ?? 12 })
  const errors = validateRx(rx)
  const drop = rx.dropReps != null

  const update = (next: Rx) => {
    setLocal(next)
    if (rxValid(next)) onChange(next)
  }

  const toggleDrop = () => {
    haptic.select()
    if (drop) update({ ...rx, dropReps: null, repsMin: plain.repsMin, repsMax: plain.repsMax })
    else {
      if (rx.repsMin != null) setPlain({ repsMin: rx.repsMin, repsMax: rx.repsMax ?? rx.repsMin })
      update({ ...rx, dropReps: defaultDrops(rx), repsMin: null, repsMax: null })
    }
  }

  const setDrop = (i: number, v: number | null) =>
    update({ ...rx, dropReps: rx.dropReps!.map((r, j) => (j === i ? (v == null ? 0 : Math.round(v)) : r)) })

  return (
    <Sheet onClose={onClose}>
      <h1 style={{ fontSize: 22 }}>{capitalize(item.name)}</h1>
      <div className="hint num">
        {rxValid(rx) ? formatPrescription(prescriptionOf(rx)) : '…'}
        {was && ` · было ${was}`}
      </div>
      {!hasHistory && (
        <div className="hint">{isNew ? 'Новое в дне' : 'Новое упражнение'}: истории нет, вес подберёшь на тренировке</div>
      )}
      <div className="spacer" />
      <button className="btn secondary" onClick={onReplace}>
        Заменить упражнение
      </button>

      <h2>Подходы</h2>
      <Stepper
        label="Подходы"
        value={rx.sets}
        min={LIMITS.setsMin}
        max={LIMITS.setsMax}
        invalid={!!errors.sets}
        onChange={(v) => update({ ...rx, sets: v ?? 0 })}
      />
      {errors.sets && <div className="field-error">{errors.sets}</div>}

      <div className="list" style={{ marginTop: 16 }}>
        <div className="row">
          <div className="grow">
            <div className="title">Дропсет</div>
            <div className="sub">Подход со снижением веса без отдыха</div>
          </div>
          <Switch on={drop} label="Дропсет" onToggle={toggleDrop} />
        </div>
      </div>

      {drop ? (
        <>
          <h2>Повторы в дропе</h2>
          <div className="drop-row">
            {rx.dropReps!.map((r, i) => (
              <div className="drop-cell" key={i}>
                <NumField value={r || null} invalid={!!errors.drop && !(r >= LIMITS.repsMin && r <= LIMITS.repsMax)} onChange={(v) => setDrop(i, v)} />
              </div>
            ))}
            <button
              className="icon-btn"
              aria-label="Убрать ступень"
              disabled={rx.dropReps!.length <= LIMITS.dropMin}
              onClick={() => {
                haptic.select()
                update({ ...rx, dropReps: rx.dropReps!.slice(0, -1) })
              }}
            >
              −
            </button>
            <button
              className="icon-btn"
              aria-label="Добавить ступень"
              disabled={rx.dropReps!.length >= LIMITS.dropMax}
              onClick={() => {
                haptic.select()
                const last = rx.dropReps![rx.dropReps!.length - 1] || 6
                update({ ...rx, dropReps: [...rx.dropReps!, last] })
              }}
            >
              +
            </button>
          </div>
          {errors.drop && <div className="field-error">{errors.drop}</div>}
        </>
      ) : (
        <>
          <div className="reps-pair">
            <div>
              <h2>Повторы от</h2>
              <Stepper
                label="Повторы от"
                value={rx.repsMin}
                min={LIMITS.repsMin}
                max={LIMITS.repsMax}
                invalid={!!errors.reps}
                onChange={(v) => {
                  // «до» follows «от» upwards, so 12 -> 13 with «до» 12 stays valid.
                  const max = rx.repsMax != null && v != null && v > rx.repsMax ? v : rx.repsMax
                  update({ ...rx, repsMin: v, repsMax: max })
                }}
              />
            </div>
            <div>
              <h2>до</h2>
              <Stepper
                label="Повторы до"
                value={rx.repsMax}
                min={LIMITS.repsMin}
                max={LIMITS.repsMax}
                invalid={!!errors.reps}
                onChange={(v) => update({ ...rx, repsMax: v })}
              />
            </div>
          </div>
          {errors.reps && <div className="field-error">{errors.reps}</div>}
        </>
      )}

      <h2>Интенсивность</h2>
      <div className="segmented">
        {INTENSITIES.map(([v, label]) => (
          <button
            key={label}
            className={rx.intensity === v ? 'active' : ''}
            onClick={() => {
              haptic.select()
              update({ ...rx, intensity: v })
            }}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="spacer" />
      <div className="spacer" />
      <button className="btn" disabled={!rxValid(rx)} onClick={onClose}>
        Готово
      </button>
      <button
        className="btn danger"
        disabled={!canRemove}
        onClick={async () => {
          if (await confirm(`Убрать «${capitalize(item.name)}» из дня?`)) {
            haptic.tap()
            onRemove()
          }
        }}
      >
        Убрать из дня
      </button>
      {!canRemove && <div className="hint" style={{ textAlign: 'center' }}>Это последнее упражнение дня</div>}
    </Sheet>
  )
}
