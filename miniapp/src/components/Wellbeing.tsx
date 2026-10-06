import { useEffect, useState } from 'react'
import { ApiError, deleteWellbeing, getWellbeing, type WellbeingEntry } from '../api'
import { confirm, haptic } from '../telegram'
import { useRemote } from '../useRemote'
import { formatNotedTime, formatWellbeingDate, groupByDate, wellbeingDetails } from '../wellbeing'
import { Sheet } from './Sheet'

export const WELLBEING_DAYS = 14

export interface WellbeingData {
  /** Undefined while loading and on any error (401 outside Telegram included): the UI then shows nothing. */
  entries: WellbeingEntry[] | undefined
  failed: boolean
  remove: (e: WellbeingEntry) => Promise<void>
  clearFailed: () => void
}

/** Server-only data: loaded when the screen opens and again when the app comes back to the foreground. */
export function useWellbeing(): WellbeingData {
  const r = useRemote<WellbeingEntry[]>(`wellbeing:${WELLBEING_DAYS}`, () => getWellbeing(WELLBEING_DAYS))
  const [hidden, setHidden] = useState<Set<number>>(new Set())
  const [failed, setFailed] = useState(false)

  async function remove(e: WellbeingEntry) {
    if (!(await confirm(`Удалить запись о самочувствии от ${formatNotedTime(e.notedAt)}?`))) return
    setFailed(false)
    setHidden((h) => new Set(h).add(e.id))
    try {
      await deleteWellbeing(e.id)
      haptic.success()
    } catch (err) {
      // 404: already gone; anything else: show it again.
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

  return { entries: r.data?.filter((e) => !hidden.has(e.id)), failed, remove, clearFailed: () => setFailed(false) }
}

/** Keeps the day open while it has entries; closes the sheet once the last one is deleted. */
export function useWellbeingSheet(entries: WellbeingEntry[] | undefined) {
  const [date, setDate] = useState<string | null>(null)
  const dayEntries = (date && entries && groupByDate(entries).find((d) => d.date === date)?.entries) || []
  useEffect(() => {
    if (date && entries && !entries.some((e) => e.date === date)) setDate(null)
  }, [date, entries])
  return { date, dayEntries, open: setDate, close: () => setDate(null) }
}

function WellbeingSheet({
  date,
  entries,
  failed,
  onRemove,
  onClose,
}: {
  date: string
  entries: WellbeingEntry[]
  failed: boolean
  onRemove: (e: WellbeingEntry) => void
  onClose: () => void
}) {
  return (
    <Sheet onClose={onClose}>
      <div className="eyebrow">Самочувствие</div>
      <h1 style={{ fontSize: 22 }}>{formatWellbeingDate(date)}</h1>
      {failed && (
        <p className="hint" style={{ margin: '8px 4px 0', color: 'var(--danger)' }}>
          Не удалось удалить. Проверь интернет и попробуй ещё раз.
        </p>
      )}
      {entries.map((e) => {
        const rows = wellbeingDetails(e)
        return (
          <div className="wb-entry" key={e.id}>
            <div className="wb-entry-time num">{formatNotedTime(e.notedAt)}</div>
            <div className="list">
              {rows.length ? (
                rows.map((r) => (
                  <div className="row wb-detail" key={r.label}>
                    <div className="hint">{r.label}</div>
                    <div className="wb-detail-value num">{r.value}</div>
                  </div>
                ))
              ) : (
                <div className="row hint">Без подробностей</div>
              )}
            </div>
            <button type="button" className="btn danger" onClick={() => onRemove(e)}>
              Удалить запись
            </button>
          </div>
        )
      })}
    </Sheet>
  )
}

/** The open day's sheet; renders nothing when no day is open or the day has no entries left. */
export function WellbeingDaySheet({ wb, sheet }: { wb: WellbeingData; sheet: ReturnType<typeof useWellbeingSheet> }) {
  if (!sheet.date || !sheet.dayEntries.length) return null
  return (
    <WellbeingSheet
      date={sheet.date}
      entries={sheet.dayEntries}
      failed={wb.failed}
      onRemove={wb.remove}
      onClose={() => {
        wb.clearFailed()
        sheet.close()
      }}
    />
  )
}
