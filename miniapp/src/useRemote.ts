import { useEffect, useState } from 'react'
import { onForeground } from './telegram'

// ---------- data loading for server-only screens (nutrition, reminders, wellbeing) ----------

export interface Remote<T> {
  data: T | undefined
  error: unknown
  loading: boolean
  reload: () => void
}

/** Loads `key` with `load`; keeps showing the last data of the same key while reloading. */
export function useRemote<T>(key: string, load: () => Promise<T>): Remote<T> {
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
  useEffect(() => onForeground(() => setNonce((n) => n + 1)), [])

  const cur = res?.key === key ? res : null
  return { data: cur?.data, error: cur?.error, loading: !cur, reload: () => setNonce((n) => n + 1) }
}
