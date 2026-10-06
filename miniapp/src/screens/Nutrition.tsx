import { useEffect, useState } from 'react'
import {
  ApiError,
  createReminder,
  deleteFood,
  deleteReminder,
  getNutritionDay,
  getNutritionWeek,
  getReminders,
  inTelegram,
  REMINDER_TEXT_MAX,
  REMINDERS_MAX,
  updateReminder,
  type FoodEntry,
  type MacroKey,
  type NutritionDay,
  type NutritionWeek,
  type Profile,
  type Reminder,
  type ReminderInput,
  type ReminderKind,
  type Targets,
} from '../api'
import { BarSeries } from '../components/LazyCharts'
import { IconChevronLeft, IconChevronRight, IconClose, IconPlus } from '../components/icons'
import { NumField } from '../components/NumField'
import { Sheet } from '../components/Sheet'
import { capitalize, WEEKDAY_SHORT } from '../program'
import { ABOUT_MAX, GOALS, profileErrors, profileLimits, profilePatch } from '../profile'
import { KIND_DEFAULTS, REMINDER_WEEKDAYS, reminderTitle, reminderWhen, repeatPhrase, weekdayShort } from '../reminders'
import { actions, getState, useStore } from '../store'
import { confirm, haptic } from '../telegram'

// Food data and reminders are never cached in the offline store: every view loads them from the server when shown.

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

type SaveStatus = 'idle' | 'saving' | 'saved' | 'local' | 'failed'

/** Profile for the bot's AI advice. Like the targets, the store changes only from the server's answer. */
function ProfileForm() {
  const { profile } = useStore()
  const [draft, setDraft] = useState<Profile>(profile)
  const [status, setStatus] = useState<SaveStatus>('idle')
  const patch = profilePatch(draft, profile)
  const dirty = Object.keys(patch).length > 0
  const errors = profileErrors(draft)
  const anyInvalid = Object.keys(errors).length > 0
  const lim = profileLimits()

  // Server state may arrive after the screen opened; adopt it unless the user is editing.
  useEffect(() => {
    if (!dirty) setDraft(profile)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile])

  function edit(p: Partial<Profile>) {
    setStatus('idle')
    setDraft((d) => ({ ...d, ...p }))
  }

  async function save() {
    if (!dirty || anyInvalid || status === 'saving') return
    setStatus('saving')
    const sent = draft
    const res = await actions.setProfile(patch)
    if (res === 'failed') haptic.error()
    else {
      haptic.success()
      // Take the stored (normalized) values so the form is clean, unless the user kept typing meanwhile.
      setDraft((d) => (d === sent ? getState().profile : d))
    }
    setStatus(res)
  }

  const about = draft.about ?? ''

  return (
    <>
      <h2>О себе</h2>
      <div className="list">
        <label className="row">
          <div className="grow title">Вес</div>
          <div className="target-field">
            <NumField decimal value={draft.weightKg} invalid={errors.weightKg} onChange={(v) => edit({ weightKg: v })} />
          </div>
          <div className="target-unit muted">кг</div>
        </label>
        <label className="row">
          <div className="grow title">Рост</div>
          <div className="target-field">
            <NumField value={draft.heightCm} invalid={errors.heightCm} onChange={(v) => edit({ heightCm: v })} />
          </div>
          <div className="target-unit muted">см</div>
        </label>
        <label className="row">
          <div className="grow title">Год рождения</div>
          <div className="target-field">
            <NumField value={draft.birthYear} invalid={errors.birthYear} onChange={(v) => edit({ birthYear: v })} />
          </div>
          <div className="target-unit" />
        </label>
        <div className="row profile-stack">
          <div className="title">Цель</div>
          <div className="segmented profile-goal" role="group" aria-label="Цель">
            {GOALS.map((g) => (
              <button
                type="button"
                key={g.key}
                className={draft.goal === g.key ? 'active' : ''}
                aria-pressed={draft.goal === g.key}
                onClick={() => {
                  haptic.select()
                  // Tapping the chosen goal again clears it.
                  edit({ goal: draft.goal === g.key ? null : g.key })
                }}
              >
                {g.label}
              </button>
            ))}
          </div>
        </div>
        <label className="row profile-stack">
          <div className="title">Травмы, сон, ограничения</div>
          <textarea
            className={`field profile-about-field ${errors.about ? 'invalid' : ''}`}
            value={about}
            maxLength={ABOUT_MAX}
            rows={3}
            placeholder="Например: болит левое плечо, сплю 6 часов, не ем молочное"
            onChange={(e) => edit({ about: e.target.value })}
          />
          <div className="hint">
            Нейросеть учитывает это в советах.
            {about.length > ABOUT_MAX - 100 && ` ${about.length} / ${ABOUT_MAX}`}
          </div>
        </label>
      </div>
      {anyInvalid && (
        <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
          Проверь значения: вес {lim.weightKg.min}–{lim.weightKg.max} кг, рост {lim.heightCm.min}–{lim.heightCm.max} см, год
          рождения {lim.birthYear.min}–{lim.birthYear.max}.
        </p>
      )}
      <div className="spacer" />
      <button className="btn" disabled={!dirty || anyInvalid || status === 'saving'} onClick={save}>
        {status === 'saving' ? 'Сохраняю…' : 'Сохранить профиль'}
      </button>
      {status === 'saved' && <p className="hint" style={{ margin: '8px 4px 0' }}>Профиль сохранён.</p>}
      {status === 'local' && (
        <p className="hint" style={{ margin: '8px 4px 0' }}>
          Сохранено только в этом браузере. Открой дневник из бота, чтобы профиль попал на сервер.
        </p>
      )}
      {status === 'failed' && (
        <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
          Сервер не сохранил профиль. Проверь интернет и попробуй ещё раз.
        </p>
      )}
      <p className="hint" style={{ margin: '8px 4px 0' }}>
        Используется для советов от ИИ: команда /advice в боте.
      </p>
    </>
  )
}

function Settings() {
  const { targets } = useStore()
  const [draft, setDraft] = useState<Targets>(targets)
  const [status, setStatus] = useState<SaveStatus>('idle')
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
      <ProfileForm />

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

      <Reminders />
    </>
  )
}

// ---------- reminders (spec section 2) ----------

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/

/** By time of day; at the same time daily reminders first, then Monday..Sunday. */
const byTime = (a: Reminder, b: Reminder) =>
  a.time.localeCompare(b.time) || (a.weekday ?? -1) - (b.weekday ?? -1) || a.id - b.id

function Reminders() {
  const r = useRemote<Reminder[]>('reminders', getReminders)
  // Local copy so toggles and edits show at once; re-seeded whenever the server list arrives.
  const [items, setItems] = useState<Reminder[] | null>(null)
  const [editing, setEditing] = useState<Reminder | 'new' | null>(null)
  const [pending, setPending] = useState<ReadonlySet<number>>(new Set())
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (r.data) setItems([...r.data].sort(byTime))
  }, [r.data])

  const upsert = (rem: Reminder) => setItems((list) => [...(list ?? []).filter((x) => x.id !== rem.id), rem].sort(byTime))
  const drop = (id: number) => setItems((list) => list && list.filter((x) => x.id !== id))

  async function toggle(rem: Reminder) {
    if (pending.has(rem.id)) return
    haptic.select()
    setFailed(false)
    setPending((p) => new Set(p).add(rem.id))
    const enabled = !rem.enabled
    upsert({ ...rem, enabled })
    try {
      upsert(await updateReminder(rem.id, { enabled }))
    } catch (err) {
      haptic.error()
      // 404: deleted elsewhere; anything else: roll back.
      if (err instanceof ApiError && err.status === 404) drop(rem.id)
      else {
        upsert(rem)
        setFailed(true)
      }
    } finally {
      setPending((p) => {
        const next = new Set(p)
        next.delete(rem.id)
        return next
      })
    }
  }

  const full = (items?.length ?? 0) >= REMINDERS_MAX

  return (
    <>
      <h2>Напоминания</h2>
      {r.error != null && !items ? (
        <RemindersError error={r.error} onRetry={r.reload} />
      ) : !items ? (
        <div className="card hint">Загрузка…</div>
      ) : (
        <>
          {items.length ? (
            <div className="list">
              {items.map((rem) => (
                <div className={`row reminder ${rem.enabled ? '' : 'off'}`} key={rem.id}>
                  <button className="reminder-open" onClick={() => setEditing(rem)}>
                    <span className="reminder-time num">
                      {rem.weekday != null && <span className="reminder-day">{weekdayShort(rem.weekday)}</span>}
                      {rem.time}
                    </span>
                    <span className="reminder-title">{reminderTitle(rem)}</span>
                  </button>
                  <Switch
                    on={rem.enabled}
                    disabled={pending.has(rem.id)}
                    label={`${reminderWhen(rem)} ${reminderTitle(rem)}`}
                    onToggle={() => toggle(rem)}
                  />
                </div>
              ))}
            </div>
          ) : (
            <div className="card hint">Пока нет напоминаний. Например: «Креатин» в 09:00, сводка КБЖУ в 20:00 или советы недели в воскресенье.</div>
          )}
          {failed && (
            <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
              Не удалось переключить напоминание. Попробуй ещё раз.
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
            {full && `Больше ${REMINDERS_MAX} напоминаний добавить нельзя. `}
            Напоминания приходят в чат бота, пока запущен сервер.
          </p>
        </>
      )}

      {editing && (
        <ReminderSheet
          reminder={editing === 'new' ? null : editing}
          onSaved={(rem) => {
            upsert(rem)
            setEditing(null)
          }}
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

function RemindersError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : null
  if (status === 401 || status === 403)
    return (
      <div className="card hint">
        {inTelegram
          ? 'Не получилось войти. Закрой дневник и открой его заново из бота.'
          : 'Напоминания хранятся на сервере бота. Открой дневник из бота в Telegram, чтобы их настроить.'}
      </div>
    )
  return (
    <div className="card">
      <div className="hint">{status === 404 ? 'Сервер пока не умеет напоминания.' : 'Не удалось загрузить напоминания.'}</div>
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

function Switch({ on, disabled, label, onToggle }: { on: boolean; disabled?: boolean; label: string; onToggle: () => void }) {
  return (
    <button
      className={`switch ${on ? 'on' : ''}`}
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onToggle}
    >
      <span className="switch-track" />
    </button>
  )
}

function saveError(err: unknown): string {
  const status = err instanceof ApiError ? err.status : null
  if (status === 401 || status === 403) return 'Не получилось войти. Закрой дневник и открой его заново из бота.'
  if (status === 409) return `Больше ${REMINDERS_MAX} напоминаний добавить нельзя.`
  if (status != null && status >= 400 && status < 500) return 'Сервер не принял напоминание. Проверь время и текст.'
  return 'Не удалось сохранить. Проверь интернет и попробуй ещё раз.'
}

function ReminderSheet({
  reminder,
  onSaved,
  onGone,
  onClose,
}: {
  reminder: Reminder | null
  onSaved: (r: Reminder) => void
  onGone: (id: number) => void
  onClose: () => void
}) {
  const [time, setTime] = useState(reminder?.time ?? KIND_DEFAULTS.text.time)
  const [kind, setKind] = useState<ReminderKind>(reminder?.kind ?? 'text')
  const [weekday, setWeekday] = useState<number | null>(reminder ? (reminder.weekday ?? null) : KIND_DEFAULTS.text.weekday)
  // A new reminder follows its kind's default time and day until the user picks them.
  const [whenTouched, setWhenTouched] = useState(reminder != null)
  // Kept while switching to "nutrition" so switching back restores it.
  const [text, setText] = useState(reminder?.text ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const timeOk = TIME_RE.test(time)
  const textOk = kind !== 'text' || text.trim().length > 0
  const canSave = timeOk && textOk && !busy

  async function save() {
    if (!canSave) return
    setBusy(true)
    setError(null)
    // `text` is only meaningful for kind "text"; omit it otherwise instead of relying on null handling.
    // `weekday` is always sent: null on PATCH turns a weekly reminder back into a daily one.
    const body: ReminderInput = { time, kind, weekday, ...(kind === 'text' ? { text: text.trim() } : {}) }
    try {
      const saved = reminder ? await updateReminder(reminder.id, body) : await createReminder({ ...body, enabled: true })
      haptic.success()
      onSaved(saved)
    } catch (err) {
      haptic.error()
      if (reminder && err instanceof ApiError && err.status === 404) return onGone(reminder.id)
      setError(saveError(err))
      setBusy(false)
    }
  }

  async function remove() {
    if (!reminder || busy) return
    if (!(await confirm(`Удалить напоминание «${reminderTitle(reminder)}» в ${reminderWhen(reminder)}?`))) return
    setBusy(true)
    setError(null)
    try {
      await deleteReminder(reminder.id)
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
    onGone(reminder.id)
  }

  return (
    <Sheet onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          save()
        }}
      >
        <h1 style={{ fontSize: 22 }}>{reminder ? 'Напоминание' : 'Новое напоминание'}</h1>
        <div className="spacer" />
        <div className="segmented">
          {(
            [
              ['text', 'Текст'],
              // Short labels: three full titles do not fit one line at 360 px.
              ['nutrition', 'КБЖУ'],
              ['advice', 'Советы'],
            ] as const
          ).map(([k, l]) => (
            <button
              type="button"
              key={k}
              className={kind === k ? 'active' : ''}
              onClick={() => {
                haptic.select()
                setKind(k)
                if (!whenTouched) {
                  setTime(KIND_DEFAULTS[k].time)
                  setWeekday(KIND_DEFAULTS[k].weekday)
                }
              }}
            >
              {l}
            </button>
          ))}
        </div>
        <div className="spacer" />
        <div className="list">
          <label className="row">
            <div className="grow title">Время</div>
            <input
              type="time"
              className={`field reminder-time-field ${timeOk ? '' : 'invalid'}`}
              value={time}
              required
              onChange={(e) => {
                setWhenTouched(true)
                setTime(e.target.value.slice(0, 5))
              }}
            />
          </label>
          <div className="row profile-stack">
            <div className="title">День</div>
            <div className="weekday-picker" role="group" aria-label="День">
              {[null, 0, 1, 2, 3, 4, 5, 6].map((d) => (
                <button
                  type="button"
                  key={d ?? 'daily'}
                  className={`${d == null ? 'daily' : ''} ${weekday === d ? 'active' : ''}`}
                  aria-pressed={weekday === d}
                  onClick={() => {
                    haptic.select()
                    setWhenTouched(true)
                    setWeekday(d)
                  }}
                >
                  {d == null ? 'Каждый день' : REMINDER_WEEKDAYS[d]}
                </button>
              ))}
            </div>
          </div>
          <div className="hint">
            {weekday == null
              ? 'Если это время сегодня уже прошло, первое напоминание придёт завтра.'
              : 'Если сегодня этот день, а время уже прошло, первое напоминание придёт через неделю.'}
          </div>
          {kind === 'text' && (
            <label className="row reminder-text-row">
              <div className="title">Текст</div>
              <input
                type="text"
                className="field reminder-text-field"
                value={text}
                maxLength={REMINDER_TEXT_MAX}
                placeholder="Например, «Креатин»"
                enterKeyHint="done"
                onChange={(e) => setText(e.target.value)}
              />
            </label>
          )}
        </div>
        <p className="hint" style={{ margin: '8px 4px 0' }}>
          {kind === 'nutrition'
            ? `Бот пришлёт ${repeatPhrase(weekday)}, сколько ккал и белка осталось до нормы на сегодня.`
            : kind === 'advice'
              ? `ИИ пришлёт ${repeatPhrase(weekday)} рекомендации по питанию, тренировкам и восстановлению. Учитывает блок «О себе».`
              : `Бот пришлёт этот текст ${repeatPhrase(weekday)}.${text.length > REMINDER_TEXT_MAX - 40 ? ` ${text.length} / ${REMINDER_TEXT_MAX}` : ''}`}
          {!timeOk && ' Укажи время.'}
        </p>
        {error && (
          <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
            {error}
          </p>
        )}
        <div className="spacer" />
        <button type="submit" className="btn" disabled={!canSave}>
          {busy ? 'Сохраняю…' : reminder ? 'Сохранить' : 'Добавить'}
        </button>
        {reminder && (
          <button type="button" className="btn danger" style={{ marginTop: 6 }} disabled={busy} onClick={remove}>
            Удалить напоминание
          </button>
        )}
      </form>
    </Sheet>
  )
}
