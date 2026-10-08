import { useEffect, useState } from 'react'
import { onRecords, type RecordNote } from '../liveCore'

const SHOW_MS = 5000
const MAX_LINES = 3

/** Brief note about personal records, pushed by the server after a workout is saved (live topic 'records'). */
export function RecordToast() {
  const [notes, setNotes] = useState<RecordNote[] | null>(null)

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const off = onRecords((n) => {
      setNotes(n.slice(0, MAX_LINES))
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => setNotes(null), SHOW_MS)
    })
    return () => {
      off()
      if (timer) clearTimeout(timer)
    }
  }, [])

  if (!notes) return null
  return (
    <div className="card notice record-toast" role="status">
      {notes.map((n, i) => (
        <div key={i}>🏆 Рекорд: {n.text}</div>
      ))}
    </div>
  )
}
