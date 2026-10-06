import { useEffect, useState } from 'react'
import {
  ApiError,
  deleteFood,
  getNutritionDay,
  getNutritionWeek,
  inTelegram,
  type FoodEntry,
  type MacroKey,
  type NutritionDay,
  type NutritionWeek,
  type Targets,
} from '../api'
import { BarSeries } from '../components/LazyCharts'
import { IconChevronLeft, IconChevronRight, IconClose } from '../components/icons'
import { NumField } from '../components/NumField'
import { capitalize, WEEKDAY_SHORT } from '../program'
import { actions, useStore } from '../store'
import { confirm, haptic } from '../telegram'

// Food data is never cached in the offline store: every view loads it from the server when shown.

type View = 'day' | 'week' | 'settings'

const MACROS: { key: MacroKey; label: string; short: string; unit: string; max: number }[] = [
  { key: 'kcal', label: 'Калории', short: 'Ккал', unit: 'ккал', max: 10000 },
  { key: 'protein', label: 'Белки', short: 'Б', unit: 'г', max: 1000 },
  { key: 'fat', label: 'Жиры', short: 'Ж', unit: 'г', max: 1000 },
  { key: 'carbs', label: 'Углеводы', short: 'У', unit: 'г', max: 1000 },
]

const HOW_TO_LOG = 'Пиши еду в чат боту: «200 г гречки и 2 яйца»'

export function Nutrition() {
  const [view, setView] = useState<View>('day')
  // null = the server's "today" (it alone knows the day boundary in TIMEZONE).
  const [date, setDate] = useState<string | null>(null)
  const [weekEnd, setWeekEnd] = useState<string | null>(null)
  const [today, setToday] = useState<string | null>(null)

  return (
    <>
      <div className="hero">
        <div className="eyebrow">Питание</div>
      </div>
      <div className="spacer" />
      <div className="segmented">
        {(
          [
            ['day', 'День'],
            ['week', 'Неделя'],
            ['settings', 'Настройки'],
          ] as const
        ).map(([k, l]) => (
          <button
            key={k}
            className={view === k ? 'active' : ''}
            onClick={() => {
              haptic.select()
              setView(k)
            }}
          >
            {l}
          </button>
        ))}
      </div>

      {view === 'day' && (
        <Day date={date} today={today} onDate={setDate} onToday={setToday} onSetTargets={() => setView('settings')} />
      )}
      {view === 'week' && (
        <Week
          end={weekEnd}
          today={today}
          onEnd={setWeekEnd}
          onToday={setToday}
          onOpenDay={(d) => {
            setDate(d === today ? null : d)
            setView('day')
          }}
        />
      )}
      {view === 'settings' && <Settings />}
    </>
  )
}

// ---------- data loading ----------

interface Remote<T> {
  data: T | undefined
  error: unknown
  loading: boolean
  reload: () => void
}

/** Loads `key` with `load`; keeps showing the last data of the same key while reloading. */
function useRemote<T>(key: string, load: () => Promise<T>): Remote<T> {
  const [res, setRes] = useState<{ key: string; data?: T; error?: unknown } | null>(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let alive = true
    load().then(
      (data) => alive && setRes({ key, data }),
      (error: unknown) => alive && setRes({ key, error }),
    )
    return () => {
      alive = false
    }
    // `load` is rebuilt every render; `key` fully describes the request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce])

  // The app may stay open across midnight or while food is logged in the chat: refresh on return.
  useEffect(() => {
    const onVisible = () => document.visibilityState === 'visible' && setNonce((n) => n + 1)
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [])

  const cur = res?.key === key ? res : null
  return { data: cur?.data, error: cur?.error, loading: !cur, reload: () => setNonce((n) => n + 1) }
}

function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : null
  if (status === 401 || status === 403)
    return (
      <div className="empty">
        <div className="big">🍽️</div>
        {inTelegram ? (
          <>
            Не получилось войти.
            <br />
            Закрой дневник и открой его заново из бота.
          </>
        ) : (
          <>
            Питание хранится на сервере бота.
            <br />
            Открой дневник из бота в Telegram, чтобы увидеть записи.
          </>
        )}
      </div>
    )
  return (
    <div className="empty">
      <div className="big">⚠️</div>
      {status === 404 ? 'Сервер пока не умеет показывать питание.' : 'Не удалось загрузить данные.'}
      <div className="spacer" />
      <button className="btn secondary" onClick={onRetry}>
        Повторить
      </button>
    </div>
  )
}

function Loading() {
  return <div className="empty">Загрузка…</div>
}

// ---------- dates (YYYY-MM-DD, calendar arithmetic in UTC to dodge DST) ----------

function addDays(iso: string, n: number): string {
  const d = new Date(iso + 'T00:00:00Z')
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}

function weekdayOf(iso: string): string {
  return WEEKDAY_SHORT[((new Date(iso + 'T00:00:00Z').getUTCDay() + 6) % 7) + 1]
}

function dayMonth(iso: string): string {
  return new Date(iso + 'T00:00:00Z').toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', timeZone: 'UTC' })
}

function dayLabel(iso: string, today: string | null): string {
  if (iso === today) return `Сегодня, ${dayMonth(iso)}`
  if (today && iso === addDays(today, -1)) return `Вчера, ${dayMonth(iso)}`
  return `${weekdayOf(iso)}, ${dayMonth(iso)}`
}

function shortDate(iso: string): string {
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}`
}

// ---------- numbers ----------

const int = (n: number) => Math.round(n).toLocaleString('ru-RU')
const oneDecimal = (n: number) => (Number.isInteger(n) ? `${n}` : n.toFixed(1).replace('.', ','))

function hasTargets(t: Targets): boolean {
  return MACROS.some((m) => t[m.key] != null)
}

// ---------- day ----------

function DateNav({
  label,
  onPrev,
  onNext,
  canPrev,
  canNext,
}: {
  label: string
  onPrev: () => void
  onNext: () => void
  canPrev: boolean
  canNext: boolean
}) {
  return (
    <div className="date-nav">
      <button className="icon-btn" aria-label="Назад" disabled={!canPrev} onClick={onPrev}>
        <IconChevronLeft />
      </button>
      <div className="date-nav-label">{label}</div>
      <button className="icon-btn" aria-label="Вперёд" disabled={!canNext} onClick={onNext}>
        <IconChevronRight />
      </button>
    </div>
  )
}

function Day({
  date,
  today,
  onDate,
  onToday,
  onSetTargets,
}: {
  date: string | null
  today: string | null
  onDate: (d: string | null) => void
  onToday: (d: string) => void
  onSetTargets: () => void
}) {
  const r = useRemote<NutritionDay>(`day:${date ?? 'today'}`, () => getNutritionDay(date ?? undefined))
  const [hidden, setHidden] = useState<Set<number>>(new Set())
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (date == null && r.data) onToday(r.data.date)
  }, [date, r.data, onToday])

  const shown = r.data?.date ?? date
  const go = (n: number) => {
    if (!shown) return
    haptic.select()
    const next = addDays(shown, n)
    onDate(today && next >= today ? null : next)
  }

  async function remove(e: FoodEntry) {
    if (!(await confirm(`Удалить «${e.description}» из дневника?`))) return
    setFailed(false)
    setHidden((h) => new Set(h).add(e.id))
    try {
      await deleteFood(e.id)
      haptic.success()
    } catch (err) {
      // 404: already gone (deleted elsewhere); anything else: show it again.
      if (!(err instanceof ApiError && err.status === 404)) {
        haptic.error()
        setFailed(true)
        setHidden((h) => {
          const next = new Set(h)
          next.delete(e.id)
          return next
        })
      }
    }
    r.reload()
  }

  return (
    <>
      <div className="spacer" />
      <DateNav
        label={shown ? dayLabel(shown, today) : 'Сегодня'}
        canPrev={!!shown}
        canNext={!!shown && !!today && shown < today}
        onPrev={() => go(-1)}
        onNext={() => go(1)}
      />
      {r.error != null && !r.data ? (
        <LoadError error={r.error} onRetry={r.reload} />
      ) : !r.data ? (
        <Loading />
      ) : (
        <DayBody
          day={r.data}
          entries={r.data.entries.filter((e) => !hidden.has(e.id))}
          failed={failed}
          onRemove={remove}
          onSetTargets={onSetTargets}
        />
      )}
    </>
  )
}

function DayBody({
  day,
  entries,
  failed,
  onRemove,
  onSetTargets,
}: {
  day: NutritionDay
  entries: FoodEntry[]
  failed: boolean
  onRemove: (e: FoodEntry) => void
  onSetTargets: () => void
}) {
  return (
    <>
      <div className="spacer" />
      {hasTargets(day.targets) ? (
        <div className="card stack">
          {MACROS.map((m) => (
            <MacroBar
              key={m.key}
              label={m.label}
              unit={m.unit}
              total={day.totals[m.key]}
              target={day.targets[m.key]}
              remaining={day.remaining[m.key]}
            />
          ))}
        </div>
      ) : (
        <>
          <div className="macro-totals">
            {MACROS.map((m) => (
              <div className="tile" key={m.key}>
                <div className="v">
                  {int(day.totals[m.key])}
                  {m.key !== 'kcal' && <small>г</small>}
                </div>
                <div className="k">{m.key === 'kcal' ? 'ккал' : m.label.toLowerCase()}</div>
              </div>
            ))}
          </div>
          <button className="btn ghost" onClick={onSetTargets}>
            Задать норму
          </button>
        </>
      )}

      {failed && (
        <div className="card notice" style={{ marginTop: 10, color: 'var(--danger)' }}>
          Не удалось удалить запись. Попробуй ещё раз.
        </div>
      )}

      <h2>Записи{entries.length ? ` · ${entries.length}` : ''}</h2>
      {entries.length ? (
        <div className="list">
          {entries.map((e) => (
            <div className="row" key={e.id}>
              <div className="food-time num">{e.time}</div>
              <div className="grow">
                <div className="title">
                  {capitalize(e.description)}
                  {e.grams != null && <span className="muted num"> · {oneDecimal(e.grams)} г</span>}
                </div>
                <div className="sub num">
                  {e.estimated ? '≈' : ''}
                  {int(e.kcal)} ккал · Б {int(e.protein)} · Ж {int(e.fat)} · У {int(e.carbs)}
                </div>
              </div>
              <button className="icon-btn" aria-label="Удалить запись" onClick={() => onRemove(e)}>
                <IconClose />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty">
          <div className="big">🍽️</div>
          {HOW_TO_LOG}
        </div>
      )}
    </>
  )
}

function MacroBar({
  label,
  unit,
  total,
  target,
  remaining,
}: {
  label: string
  unit: string
  total: number
  target: number | null
  remaining: number | null
}) {
  if (target == null)
    return (
      <div>
        <div className="macro-head">
          <span>{label}</span>
          <span className="num">
            {int(total)} {unit}
          </span>
        </div>
        <div className="macro-note">норма не задана</div>
      </div>
    )
  const left = remaining ?? target - total
  const over = left < 0
  const pct = target > 0 ? Math.min(total / target, 1) * 100 : total > 0 ? 100 : 0
  return (
    <div>
      <div className="macro-head">
        <span>{label}</span>
        <span className="num">
          {int(total)} / {int(target)} {unit}
        </span>
      </div>
      <div className={`progress ${over ? 'over' : ''}`}>
        <div style={{ width: `${pct}%` }} />
      </div>
      <div className={`macro-note num ${over ? 'over' : ''}`}>
        {over ? `перебор ${int(-left)} ${unit}` : `осталось ${int(left)} ${unit}`}
      </div>
    </div>
  )
}

// ---------- week ----------

function Week({
  end,
  today,
  onEnd,
  onToday,
  onOpenDay,
}: {
  end: string | null
  today: string | null
  onEnd: (d: string | null) => void
  onToday: (d: string) => void
  onOpenDay: (d: string) => void
}) {
  const r = useRemote<NutritionWeek>(`week:${end ?? 'today'}`, () => getNutritionWeek(end ?? undefined))
  const days = r.data?.days ?? []
  const last = days[days.length - 1]?.date ?? end

  useEffect(() => {
    if (end == null && last) onToday(last)
  }, [end, last, onToday])

  const go = (n: number) => {
    if (!last) return
    haptic.select()
    const next = addDays(last, n)
    onEnd(today && next >= today ? null : next)
  }

  return (
    <>
      <div className="spacer" />
      <DateNav
        label={last ? `${shortDate(addDays(last, -6))} – ${shortDate(last)}` : 'Последние 7 дней'}
        canPrev={!!last}
        canNext={!!last && !!today && last < today}
        onPrev={() => go(-7)}
        onNext={() => go(7)}
      />
      {r.error != null && !r.data ? (
        <LoadError error={r.error} onRetry={r.reload} />
      ) : !r.data ? (
        <Loading />
      ) : (
        <WeekBody week={r.data} today={today} onOpenDay={onOpenDay} />
      )}
    </>
  )
}

function WeekBody({ week, today, onOpenDay }: { week: NutritionWeek; today: string | null; onOpenDay: (d: string) => void }) {
  const logged = week.days.filter((d) => d.entries > 0)
  if (!logged.length)
    return (
      <div className="empty">
        <div className="big">🍽️</div>
        За эти 7 дней записей нет.
        <br />
        {HOW_TO_LOG}
      </div>
    )

  const avg = (k: MacroKey) => logged.reduce((s, d) => s + d[k], 0) / logged.length
  const target = week.targets.kcal
  const chart = week.days.map((d) => ({ label: `${weekdayOf(d.date)} ${Number(d.date.slice(8, 10))}`, kcal: Math.round(d.kcal) }))
  const overCell = (d: (typeof week.days)[number], k: MacroKey) =>
    week.targets[k] != null && d[k] > week.targets[k]! ? 'over' : ''

  return (
    <>
      <div className="spacer" />
      <div className="tiles">
        <div className="tile">
          <div className="v">{int(avg('kcal'))}</div>
          <div className="k">ккал в среднем</div>
        </div>
        <div className="tile">
          <div className="v">
            {int(avg('protein'))}
            <small>г</small>
          </div>
          <div className="k">белка в среднем</div>
        </div>
        <div className="tile">
          <div className="v">
            {logged.length}
            <small>/ 7</small>
          </div>
          <div className="k">дней с записями</div>
        </div>
      </div>

      <h2>Калории по дням</h2>
      <div className="chart-card">
        <div className="chart-title">
          <span className="hint">ккал</span>
          {target != null && (
            <span className="hint num">
              <span className="legend-dash" />
              норма {int(target)}
            </span>
          )}
        </div>
        <BarSeries data={chart} x="label" y="kcal" unit="ккал" target={target} />
      </div>
      <p className="hint" style={{ margin: '6px 4px 0' }}>
        Среднее считается по дням с записями.
      </p>

      <h2>По дням</h2>
      <div className="card">
        <table className="points nutrition-table">
          <thead>
            <tr>
              <th>День</th>
              <th>Ккал</th>
              <th>Б, г</th>
              <th>Ж, г</th>
              <th>У, г</th>
            </tr>
          </thead>
          <tbody>
            {[...week.days].reverse().map((d) => (
              <tr key={d.date} className={d.entries ? 'clickable' : ''} onClick={() => d.entries && onOpenDay(d.date)}>
                <td style={d.date === today ? { fontWeight: 600 } : undefined}>
                  {weekdayOf(d.date)} {shortDate(d.date)}
                </td>
                {d.entries ? (
                  MACROS.map((m) => (
                    <td key={m.key} className={overCell(d, m.key)}>
                      {int(d[m.key])}
                    </td>
                  ))
                ) : (
                  <td colSpan={4} className="muted">
                    нет записей
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hasTargets(week.targets) && (
        <p className="hint" style={{ margin: '6px 4px 0' }}>
          Красным — выше нормы. Нажми на день, чтобы увидеть записи.
        </p>
      )}
    </>
  )
}

// ---------- settings ----------

function Settings() {
  const { targets } = useStore()
  const [draft, setDraft] = useState<Targets>(targets)
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved' | 'local' | 'failed'>('idle')
  const dirty = MACROS.some((m) => draft[m.key] !== targets[m.key])

  // Server state may arrive after the screen opened; adopt it unless the user is editing.
  useEffect(() => {
    if (!dirty) setDraft(targets)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targets])

  const invalid = (m: (typeof MACROS)[number]) => draft[m.key] != null && draft[m.key]! > m.max
  const anyInvalid = MACROS.some(invalid)
  const { protein, fat, carbs } = draft
  const fromMacros = protein != null && fat != null && carbs != null ? protein * 4 + fat * 9 + carbs * 4 : null

  async function save() {
    if (anyInvalid || status === 'saving') return
    setStatus('saving')
    const res = await actions.setTargets(draft)
    if (res === 'failed') haptic.error()
    else haptic.success()
    setStatus(res)
  }

  return (
    <>
      <h2>Норма в день</h2>
      <div className="list">
        {MACROS.map((m) => (
          <label className="row" key={m.key}>
            <div className="grow title">{m.label}</div>
            <div className="target-field">
              <NumField
                value={draft[m.key]}
                invalid={invalid(m)}
                onChange={(v) => {
                  setStatus('idle')
                  setDraft((d) => ({ ...d, [m.key]: v == null ? null : Math.round(v) }))
                }}
              />
            </div>
            <div className="target-unit muted">{m.unit}</div>
          </label>
        ))}
      </div>
      <p className="hint" style={{ margin: '8px 4px 0' }}>
        {anyInvalid
          ? 'Слишком большое значение: калории до 10 000, граммы до 1000.'
          : 'Пустое поле — норма не задана.'}
        {fromMacros != null && !anyInvalid && ` По БЖУ выходит ${int(fromMacros)} ккал.`}
      </p>
      <div className="spacer" />
      <button
        className="btn"
        disabled={(!dirty && status !== 'failed') || anyInvalid || status === 'saving'}
        onClick={save}
      >
        {status === 'saving' ? 'Сохраняю…' : 'Сохранить норму'}
      </button>
      {status === 'saved' && <p className="hint" style={{ margin: '8px 4px 0' }}>Норма сохранена.</p>}
      {status === 'local' && (
        <p className="hint" style={{ margin: '8px 4px 0' }}>
          Сохранено только в этом браузере. Открой дневник из бота, чтобы норма попала на сервер.
        </p>
      )}
      {status === 'failed' && (
        <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
          Сервер не сохранил норму. Проверь интернет и попробуй ещё раз.
        </p>
      )}

      <h2>Напоминания</h2>
      {/* Reminders block (spec section 2) goes here. */}
      <div className="card hint">Скоро здесь можно будет настроить напоминания: креатин, вода, вечерняя сводка КБЖУ.</div>
    </>
  )
}
