// Pure parts of the live updates channel (GET /api/live, server-sent events): topic parsing and routing,
// the reconnect backoff, the refresh bus for server-only screens and the connection state machine.
// No window/fetch/EventSource here, so tests run in node; live.ts wires it to the browser.

export const TOPICS = ['state', 'nutrition', 'reminders', 'facts', 'wellbeing', 'plan', 'workouts', 'weight'] as const
export type Topic = (typeof TOPICS)[number]

const KNOWN = new Set<string>(TOPICS)

/** Topics of a `change` event payload `{"topics": [...]}`; unknown names and malformed data are dropped. */
export function parseChange(data: string): Topic[] {
  let parsed: unknown
  try {
    parsed = JSON.parse(data)
  } catch {
    return []
  }
  const topics = (parsed as { topics?: unknown } | null)?.topics
  if (!Array.isArray(topics)) return []
  const out: Topic[] = []
  for (const t of topics) if (typeof t === 'string' && KNOWN.has(t) && !out.includes(t as Topic)) out.push(t as Topic)
  return out
}

export interface Route {
  /** The offline store must reload /api/state (settings, history, the workout in progress). */
  sync: boolean
  /** Topics passed to the refresh bus; each useRemote reloads only if it watches one of them. */
  remote: Topic[]
}

/** state/workouts live in the store; the rest are server-only screens loaded with useRemote. */
export function routeTopics(topics: readonly Topic[]): Route {
  return {
    sync: topics.includes('state') || topics.includes('workouts'),
    // A screen may also depend on state or workouts (today's plan, nutrition targets): pass all.
    remote: [...topics],
  }
}

export const BACKOFF_MIN_MS = 1000
export const BACKOFF_MAX_MS = 30_000

/** Delay before reconnect attempt `attempt` (0-based): 1 s doubling up to 30 s, with +-20% jitter. */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** Math.max(0, attempt))
  const jittered = base * (0.8 + 0.4 * random())
  return Math.round(Math.min(BACKOFF_MAX_MS, Math.max(BACKOFF_MIN_MS * 0.8, jittered)))
}

/** POST /api/live/token answered by an older server without live updates: off for the session. */
export function liveUnsupported(status: number | undefined): boolean {
  return status === 404 || status === 405
}

// ---------- refresh bus for useRemote ----------

/** 'all' after a reconnect: events may have been missed while the stream was down. */
export type RefreshSignal = readonly Topic[] | 'all'

const busListeners = new Set<(signal: RefreshSignal) => void>()

export function onRemoteRefresh(cb: (signal: RefreshSignal) => void): () => void {
  busListeners.add(cb)
  return () => {
    busListeners.delete(cb)
  }
}

export function emitRemoteRefresh(signal: RefreshSignal): void {
  if (signal !== 'all' && signal.length === 0) return
  busListeners.forEach((l) => l(signal))
}

/** Whether a screen watching `watched` must reload on `signal`. No topics: only foreground reloads. */
export function signalMatches(watched: readonly Topic[] | undefined, signal: RefreshSignal): boolean {
  if (!watched?.length) return false
  return signal === 'all' || signal.some((t) => watched.includes(t))
}

// ---------- connection state machine ----------

export interface LiveHandlers {
  onOpen(): void
  onChange(data: string): void
  onError(): void
}

export interface LiveStream {
  close(): void
}

export interface LiveDeps {
  /** POST /api/live/token; rejects with an error carrying the HTTP status when there is one. */
  getToken(): Promise<string>
  /** Opens GET /api/live?token=...; the stream must not reconnect by itself after onError. */
  open(token: string, handlers: LiveHandlers): LiveStream
  /** Page visible and the app in server mode. */
  canRun(): boolean
  errorStatus(e: unknown): number | undefined
  /** Changes reported by the server, merged over COALESCE_MS. */
  dispatch(route: Route): void
  /** Full refresh after a reconnect (events may have been missed). */
  resync(): void
  random?: () => number
  now?: () => number
}

/** Changes arriving together (a bot reply touching several tables) cause one refresh. */
export const COALESCE_MS = 150
/** A stream that lived this long was healthy: the next reconnect starts from the shortest delay. */
export const HEALTHY_MS = 30_000

export interface LiveController {
  /** Connect if allowed and not connected yet; call on start and when the page becomes visible. */
  start(): void
  /** Close the stream and cancel retries; call when the page is hidden. */
  stop(): void
  readonly disabled: boolean
  readonly connected: boolean
}

export function createLive(deps: LiveDeps): LiveController {
  const now = deps.now ?? Date.now
  let disabled = false
  let gen = 0 // bumps on every connect/stop; callbacks of an older generation are ignored
  let stream: LiveStream | null = null
  let connecting = false
  let open = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let attempt = 0
  let openedAt = 0
  let missed = false // the stream dropped: refresh everything once the next one opens
  let pending = new Set<Topic>()
  let flushTimer: ReturnType<typeof setTimeout> | null = null

  const flush = () => {
    flushTimer = null
    const topics = [...pending]
    pending = new Set()
    if (topics.length) deps.dispatch(routeTopics(topics))
  }

  const clearTimers = () => {
    if (retryTimer) clearTimeout(retryTimer)
    if (flushTimer) clearTimeout(flushTimer)
    retryTimer = flushTimer = null
    pending = new Set()
  }

  const closeStream = () => {
    stream?.close()
    stream = null
    open = false
  }

  const retry = () => {
    missed = true
    const delay = backoffDelay(attempt, deps.random)
    attempt += 1
    retryTimer = setTimeout(() => {
      retryTimer = null
      if (deps.canRun()) connect()
    }, delay)
  }

  const connect = () => {
    if (disabled || connecting || stream || retryTimer) return
    const my = ++gen
    connecting = true
    deps.getToken().then(
      (token) => {
        if (my !== gen) return
        connecting = false
        stream = deps.open(token, {
          onOpen: () => {
            if (my !== gen) return
            open = true
            openedAt = now()
            if (missed) {
              missed = false
              deps.resync()
            }
          },
          onChange: (data) => {
            if (my !== gen) return
            const topics = parseChange(data)
            if (!topics.length) return
            topics.forEach((t) => pending.add(t))
            if (!flushTimer) flushTimer = setTimeout(flush, COALESCE_MS)
          },
          onError: () => {
            if (my !== gen) return
            // A stream that never opened (401, network) or dropped right away keeps growing the delay.
            if (open && now() - openedAt >= HEALTHY_MS) attempt = 0
            closeStream()
            gen++
            retry()
          },
        })
      },
      (e: unknown) => {
        if (my !== gen) return
        connecting = false
        if (liveUnsupported(deps.errorStatus(e))) {
          disabled = true
          return
        }
        retry()
      },
    )
  }

  return {
    start() {
      if (disabled || !deps.canRun()) return
      if (retryTimer) {
        // Back from the background while waiting out a long delay: try now.
        clearTimeout(retryTimer)
        retryTimer = null
      }
      connect()
    },
    stop() {
      gen++
      connecting = false
      closeStream()
      clearTimers()
      // The foreground refresh on return covers what happens while hidden.
      attempt = 0
      missed = false
    },
    get disabled() {
      return disabled
    },
    get connected() {
      return open
    },
  }
}
