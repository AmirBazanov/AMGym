import { useEffect, useState } from 'react'
import { ApiError, deleteBodyWeight, getBodyWeights, inTelegram, saveBodyWeight, type BodyWeight } from '../api'
import {
  chartPoints,
  dropWeight,
  filterPeriod,
  formatDelta,
  formatWeight,
  parseBodyWeight,
  parseBodyWeights,
  SOURCE_LABEL,
  upsertWeight,
  WEIGHT_DAYS_ALL,
  WEIGHT_EMPTY_TEXT,
  WEIGHT_PERIODS,
  weightChange,
  weightInputError,
  weightSaveErrorText,
  type WeightChange,
} from '../bodyWeight'
import { IconClose } from '../components/icons'
import { LineSeries } from '../components/LazyCharts'
import { NumField } from '../components/NumField'
import { confirm, haptic } from '../telegram'
import { useRemote } from '../useRemote'
import { formatWellbeingDate, localISODate } from '../wellbeing'

const LIST_STEP = 10
const loadWeights = () => getBodyWeights(WEIGHT_DAYS_ALL).then(parseBodyWeights)

/** «Вес тела»: chart, latest value with 7/30-day change, quick input for today and recent entries. */
export function BodyWeightSection() {
  // Everything at once (one value per day, small): period switches never refetch, and the 30-day
  // change has its reference even when the chart shows one month.
  const r = useRemote<BodyWeight[]>('body-weight', loadWeights, ['weight'])
  // Local copy so saves and deletes show at once; re-seeded whenever the server list arrives.
  const [items, setItems] = useState<BodyWeight[] | null>(null)
  const [period, setPeriod] = useState<number | null>(90)
  const [shown, setShown] = useState(LIST_STEP)
  const [listError, setListError] = useState<string | null>(null)

  useEffect(() => {
    if (r.data) setItems(r.data)
  }, [r.data])

  async function remove(item: BodyWeight) {
    if (!(await confirm(`Удалить вес ${formatWeight(item.weightKg)} кг за ${formatWellbeingDate(item.date)}?`))) return
    setListError(null)
    setItems((list) => list && dropWeight(list, item.date))
    try {
      await deleteBodyWeight(item.date)
      haptic.success()
    } catch (err) {
      // 404: already gone (deleted from the chat); anything else: show it again.
      if (!(err instanceof ApiError && err.status === 404)) {
        haptic.error()
        setListError('Не удалось удалить. Попробуй ещё раз.')
        setItems((list) => list && upsertWeight(list, item))
      }
    }
    r.reload()
  }

  if (r.error != null && !items)
    return (
      <>
        <h2>Вес тела</h2>
        <WeightLoadError error={r.error} onRetry={r.reload} />
      </>
    )
  if (!items)
    return (
      <>
        <h2>Вес тела</h2>
        <div className="card hint">Загрузка…</div>
      </>
    )

  const today = localISODate()
  const latest = items[items.length - 1]
  const inPeriod = filterPeriod(items, period, today)
  const recent = [...items].reverse().slice(0, shown)

  return (
    <>
      <h2>Вес тела</h2>
      {latest ? (
        <>
          <div className="tiles">
            <div className="tile">
              <div className="v">
                {formatWeight(latest.weightKg)}
                <small>кг</small>
              </div>
              <div className="k">{latest.date === today ? 'сегодня' : formatWellbeingDate(latest.date)}</div>
            </div>
            <ChangeTile change={weightChange(items, 7)} label="за 7 дней" />
            <ChangeTile change={weightChange(items, 30)} label="за 30 дней" />
          </div>
          <div className="spacer" />
          <div className="segmented" style={{ marginBottom: 10 }}>
            {WEIGHT_PERIODS.map((p) => (
              <button
                key={p.label}
                className={p.days === period ? 'active' : ''}
                aria-pressed={p.days === period}
                onClick={() => {
                  haptic.select()
                  setPeriod(p.days)
                }}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="chart-card">
            {inPeriod.length > 1 ? (
              <LineSeries data={chartPoints(inPeriod)} x="label" y="kg" unit="кг" decimals />
            ) : (
              <div className="empty">Нужно хотя бы два взвешивания за период</div>
            )}
          </div>
        </>
      ) : (
        <div className="card hint">{WEIGHT_EMPTY_TEXT}</div>
      )}

      <div className="spacer" />
      <WeightInput
        todayValue={items.find((x) => x.date === today)?.weightKg ?? null}
        onSaved={(item) => {
          setItems((list) => upsertWeight(list ?? [], item))
          r.reload()
        }}
      />

      {recent.length > 0 && (
        <>
          <div className="spacer" />
          <div className="list">
            {recent.map((x) => (
              <div className="row" key={x.date}>
                <div className="grow">
                  <div className="title">{formatWellbeingDate(x.date)}</div>
                  <div className="sub">{SOURCE_LABEL[x.source]}</div>
                </div>
                <div className="num weight-value">{formatWeight(x.weightKg)} кг</div>
                <button className="icon-btn" aria-label={`Удалить вес за ${formatWellbeingDate(x.date)}`} onClick={() => remove(x)}>
                  <IconClose />
                </button>
              </div>
            ))}
          </div>
          {items.length > shown && (
            <button className="btn ghost" onClick={() => setShown((n) => n + LIST_STEP * 2)}>
              Показать ещё
            </button>
          )}
        </>
      )}
      {listError && (
        <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
          {listError}
        </p>
      )}
    </>
  )
}

function ChangeTile({ change, label }: { change: WeightChange | null; label: string }) {
  // Neutral color on purpose: whether gaining is good depends on the goal (mass or cut).
  return (
    <div className="tile">
      <div className="v">
        {change ? formatDelta(change.delta) : '—'}
        {change && <small>кг</small>}
      </div>
      <div className="k">{label}</div>
    </div>
  )
}

/** Today's weight; the server keeps one value per day, so a new one replaces today's. */
function WeightInput({ todayValue, onSaved }: { todayValue: number | null; onSaved: (item: BodyWeight) => void }) {
  const [value, setValue] = useState<number | null>(todayValue)
  const [touched, setTouched] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Today's value may arrive (or change from the chat) after the screen opened; adopt it unless typing.
  useEffect(() => {
    if (!touched) setValue(todayValue)
  }, [todayValue, touched])

  const inputError = weightInputError(value)
  const same = todayValue != null && value != null && Math.abs(value - todayValue) < 1e-9

  async function save() {
    if (inputError || saving || value == null) return
    setSaving(true)
    setError(null)
    try {
      const item = parseBodyWeight(await saveBodyWeight(Math.round(value * 10) / 10))
      haptic.success()
      setTouched(false)
      if (item) onSaved(item)
    } catch (err) {
      haptic.error()
      setError(weightSaveErrorText(err instanceof ApiError ? err.status : null))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <div className="weight-input">
        <NumField
          decimal
          value={value}
          placeholder="кг"
          invalid={touched && value != null && inputError != null}
          onChange={(v) => {
            setTouched(true)
            setError(null)
            setValue(v)
          }}
        />
        <button className="btn" disabled={inputError != null || saving || same} onClick={save}>
          {saving ? 'Сохраняю…' : 'Записать вес'}
        </button>
      </div>
      <p className="hint" style={{ margin: '8px 4px 0', color: error || (touched && value != null && inputError) ? 'var(--danger)' : undefined }}>
        {error ??
          (touched && value != null && inputError
            ? inputError
            : todayValue != null
              ? `Сегодня записано ${formatWeight(todayValue)} кг. Новое значение заменит его.`
              : 'Вес за сегодня, в кг с шагом 0,1.')}
      </p>
    </>
  )
}

function WeightLoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : null
  if (status === 401 || status === 403 || !inTelegram)
    return (
      <div className="card hint">
        {inTelegram
          ? 'Не получилось войти. Закрой дневник и открой его заново из бота.'
          : 'Вес хранится на сервере бота. Открой дневник из бота в Telegram, чтобы его посмотреть.'}
      </div>
    )
  return (
    <div className="card">
      <div className="hint">Не удалось загрузить вес.</div>
      <div className="spacer" />
      <button className="btn secondary" onClick={onRetry}>
        Повторить
      </button>
    </div>
  )
}
