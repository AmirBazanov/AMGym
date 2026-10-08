// Live updates: event parsing, topic routing, the refresh bus, backoff and the connection state machine.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  BACKOFF_MAX_MS,
  COALESCE_MS,
  HEALTHY_MS,
  backoffDelay,
  createLive,
  emitRecords,
  emitRemoteRefresh,
  liveUnsupported,
  onRecords,
  onRemoteRefresh,
  parseChange,
  parseRecords,
  routeTopics,
  signalMatches,
  type LiveDeps,
  type LiveHandlers,
  type RecordNote,
  type RefreshSignal,
  type Route,
} from './liveCore'

describe('parseChange', () => {
  it('reads known topics', () => {
    expect(parseChange('{"topics":["nutrition","state"]}')).toEqual(['nutrition', 'state'])
  })
  it('keeps the weight topic', () => {
    expect(parseChange('{"topics":["weight","state"]}')).toEqual(['weight', 'state'])
  })
  it('drops unknown names, non-strings and duplicates', () => {
    expect(parseChange('{"topics":["plan","bogus",3,null,"plan","facts"]}')).toEqual(['plan', 'facts'])
  })
  it('accepts the records topic', () => {
    expect(parseChange('{"topics":["records"],"records":[]}')).toEqual(['records'])
  })
  it('accepts the program topic', () => {
    expect(parseChange('{"topics":["program","plan","state"]}')).toEqual(['program', 'plan', 'state'])
  })
  it('returns nothing for malformed data', () => {
    for (const d of ['', 'not json', 'null', '[]', '{"topics":"state"}', '{}', '42']) expect(parseChange(d)).toEqual([])
  })
})

describe('routeTopics', () => {
  it('syncs the store for state and workouts', () => {
    expect(routeTopics(['state']).sync).toBe(true)
    expect(routeTopics(['workouts']).sync).toBe(true)
    expect(routeTopics(['nutrition', 'reminders', 'facts', 'wellbeing', 'plan']).sync).toBe(false)
  })
  it('does not sync the store for records alone', () => {
    expect(routeTopics(['records']).sync).toBe(false)
  })
  it('syncs the store for a program edit, so a new programVersion reloads the program', () => {
    expect(routeTopics(['program']).sync).toBe(true)
    expect(routeTopics(['program']).remote).toEqual(['program'])
  })
  it('passes all topics to the refresh bus', () => {
    expect(routeTopics(['workouts', 'plan']).remote).toEqual(['workouts', 'plan'])
  })
})

describe('signalMatches', () => {
  it('matches a watched topic only', () => {
    expect(signalMatches(['nutrition', 'state'], ['state'])).toBe(true)
    expect(signalMatches(['reminders'], ['nutrition', 'facts'])).toBe(false)
  })
  it('matches everything on a full refresh', () => {
    expect(signalMatches(['facts'], 'all')).toBe(true)
  })
  it('never matches a screen without topics', () => {
    expect(signalMatches(undefined, 'all')).toBe(false)
    expect(signalMatches([], ['state'])).toBe(false)
  })
})

describe('refresh bus', () => {
  it('delivers signals to subscribers until they unsubscribe', () => {
    const got: RefreshSignal[] = []
    const off = onRemoteRefresh((s) => got.push(s))
    emitRemoteRefresh(['facts'])
    emitRemoteRefresh([]) // nothing to say: not delivered
    emitRemoteRefresh('all')
    off()
    emitRemoteRefresh(['plan'])
    expect(got).toEqual([['facts'], 'all'])
  })
})

const REC = { exercise: 'жим лёжа', weight: 92.5, reps: 6, kind: 'e1rm', text: 'жим лёжа 92,5×6' }

describe('parseRecords', () => {
  const payload = (records: unknown) => JSON.stringify({ topics: ['records'], records })
  it('reads a valid payload', () => {
    expect(parseRecords(payload([REC]))).toEqual([REC])
  })
  it('keeps a bodyweight record with null weight', () => {
    const bw = { exercise: 'подтягивания', weight: null, reps: 12, kind: 'bw_reps', text: 'подтягивания ×12' }
    expect(parseRecords(payload([bw]))).toEqual([bw])
  })
  it('returns nothing for malformed JSON or a missing array', () => {
    for (const d of ['', 'not json', 'null', '{}', '{"records":"x"}', '{"topics":["records"]}']) {
      expect(parseRecords(d)).toEqual([])
    }
  })
  it('drops items with bad fields', () => {
    const bad = [
      null,
      'x',
      { ...REC, text: '   ' },
      { ...REC, text: 5 },
      { ...REC, exercise: 1 },
      { ...REC, reps: 0 },
      { ...REC, reps: 2.5 },
      { ...REC, reps: '6' },
      { ...REC, weight: -1 },
      { ...REC, weight: '90' },
      { ...REC, weight: undefined },
    ]
    expect(parseRecords(payload([...bad, REC]))).toEqual([REC])
  })
  it('trims the text and cuts it to 120 characters', () => {
    const [a, b] = parseRecords(payload([{ ...REC, text: '  жим  ' }, { ...REC, text: 'я'.repeat(300) }]))
    expect(a.text).toBe('жим')
    expect(b.text).toHaveLength(120)
  })
  it('keeps at most 5 items', () => {
    expect(parseRecords(payload(Array.from({ length: 9 }, () => REC)))).toHaveLength(5)
  })
})

describe('records bus', () => {
  it('delivers notes until unsubscribed and ignores an empty list', () => {
    const got: RecordNote[][] = []
    const off = onRecords((n) => got.push(n))
    emitRecords([REC])
    emitRecords([])
    off()
    emitRecords([REC])
    expect(got).toEqual([[REC]])
  })
})

describe('backoffDelay', () => {
  const mid = () => 0.5 // no jitter
  it('doubles from 1 s and caps at 30 s', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 10].map((a) => backoffDelay(a, mid))).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ])
  })
  it('jitters by +-20% and never exceeds the cap', () => {
    expect(backoffDelay(0, () => 0)).toBe(800)
    expect(backoffDelay(0, () => 1)).toBe(1200)
    expect(backoffDelay(3, () => 0)).toBe(6400)
    expect(backoffDelay(3, () => 1)).toBe(9600)
    expect(backoffDelay(20, () => 1)).toBe(BACKOFF_MAX_MS)
    expect(backoffDelay(20, () => 0)).toBe(24000)
  })
})

describe('liveUnsupported', () => {
  it('only for 404 and 405', () => {
    expect(liveUnsupported(404)).toBe(true)
    expect(liveUnsupported(405)).toBe(true)
    for (const s of [401, 500, 502, undefined]) expect(liveUnsupported(s)).toBe(false)
  })
})

// ---------- state machine with a fake EventSource and fake timers ----------

class HttpError extends Error {
  constructor(public status: number) {
    super(`${status}`)
  }
}

interface FakeStream {
  token: string
  h: LiveHandlers
  closed: boolean
}

function setup(over: Partial<LiveDeps> = {}) {
  let tokenN = 0
  let tokenResult: () => Promise<string> = () => Promise.resolve(`t${++tokenN}`)
  const streams: FakeStream[] = []
  const routes: Route[] = []
  let resyncs = 0
  let canRun = true
  const getToken = vi.fn(() => tokenResult())
  const live = createLive({
    getToken,
    open: (token, h) => {
      const s: FakeStream = { token, h, closed: false }
      streams.push(s)
      return { close: () => void (s.closed = true) }
    },
    canRun: () => canRun,
    errorStatus: (e) => (e instanceof HttpError ? e.status : undefined),
    dispatch: (r) => routes.push(r),
    resync: () => void resyncs++,
    random: () => 0.5,
    ...over,
  })
  return {
    live,
    streams,
    routes,
    getToken,
    last: () => streams[streams.length - 1],
    resyncs: () => resyncs,
    setCanRun: (v: boolean) => void (canRun = v),
    failToken: (e: unknown) => void (tokenResult = () => Promise.reject(e)),
    okToken: () => void (tokenResult = () => Promise.resolve(`t${++tokenN}`)),
  }
}

/** Let resolved token promises run their callbacks. */
const flush = () => vi.advanceTimersByTimeAsync(0)

describe('createLive', () => {
  beforeEach(() => void vi.useFakeTimers())
  afterEach(() => void vi.useRealTimers())

  it('connects with a fresh token and dispatches coalesced changes', async () => {
    const t = setup()
    t.live.start()
    await flush()
    expect(t.streams).toHaveLength(1)
    expect(t.last().token).toBe('t1')
    t.last().h.onOpen()
    expect(t.live.connected).toBe(true)
    expect(t.resyncs()).toBe(0) // the first connection follows the app's own initial refresh

    t.last().h.onChange('{"topics":["nutrition"]}')
    t.last().h.onChange('{"topics":["state","nutrition"]}')
    t.last().h.onChange('garbage')
    expect(t.routes).toEqual([])
    await vi.advanceTimersByTimeAsync(COALESCE_MS)
    expect(t.routes).toEqual([{ sync: true, remote: ['nutrition', 'state'] }])
  })

  it('passes records to deps at once, only for records events', async () => {
    const got: RecordNote[][] = []
    const t = setup({ records: (n) => void got.push(n) })
    t.live.start()
    await flush()
    t.last().h.onOpen()
    t.last().h.onChange('{"topics":["workouts"]}')
    t.last().h.onChange('{"topics":["records"],"records":[]}')
    t.last().h.onChange('{"topics":["records"],"records":"bad"}')
    expect(got).toEqual([])
    t.last().h.onChange(JSON.stringify({ topics: ['workouts', 'records'], records: [REC] }))
    expect(got).toEqual([[REC]]) // before the coalescing timer fires
    expect(t.routes).toEqual([])
    await vi.advanceTimersByTimeAsync(COALESCE_MS)
    expect(t.routes).toEqual([{ sync: true, remote: ['workouts', 'records'] }])
  })

  it('works without a records dep', async () => {
    const t = setup()
    t.live.start()
    await flush()
    t.last().h.onChange(JSON.stringify({ topics: ['records'], records: [REC] }))
    await vi.advanceTimersByTimeAsync(COALESCE_MS)
    expect(t.routes).toEqual([{ sync: false, remote: ['records'] }])
  })

  it('does not connect when it may not run, nor twice', async () => {
    const t = setup()
    t.setCanRun(false)
    t.live.start()
    await flush()
    expect(t.getToken).not.toHaveBeenCalled()
    t.setCanRun(true)
    t.live.start()
    t.live.start()
    await flush()
    t.live.start()
    await flush()
    expect(t.getToken).toHaveBeenCalledTimes(1)
    expect(t.streams).toHaveLength(1)
  })

  it('reconnects with backoff and a new token, then refreshes everything once', async () => {
    const t = setup()
    t.live.start()
    await flush()
    t.last().h.onOpen()

    t.last().h.onError()
    expect(t.streams[0].closed).toBe(true)
    await vi.advanceTimersByTimeAsync(999)
    expect(t.getToken).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(t.streams).toHaveLength(2)
    expect(t.last().token).toBe('t2')

    // Unauthorized stream (token expired) never opens: the delay grows.
    t.last().h.onError()
    await vi.advanceTimersByTimeAsync(1999)
    expect(t.streams).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(t.streams).toHaveLength(3)

    t.last().h.onOpen()
    expect(t.resyncs()).toBe(1)
    t.last().h.onOpen() // a second open of the same stream is not another reconnect
    expect(t.resyncs()).toBe(1)
  })

  it('a stream dropping right after opening keeps growing the delay; a healthy one resets it', async () => {
    const t = setup()
    t.live.start()
    await flush()
    t.last().h.onOpen()
    t.last().h.onError() // attempt 0 -> 1 s
    await vi.advanceTimersByTimeAsync(1000)
    t.last().h.onOpen()
    t.last().h.onError() // opened but dropped at once -> 2 s
    await vi.advanceTimersByTimeAsync(1999)
    expect(t.streams).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(t.streams).toHaveLength(3)

    t.last().h.onOpen()
    await vi.advanceTimersByTimeAsync(HEALTHY_MS)
    t.last().h.onError() // lived long enough -> back to 1 s
    await vi.advanceTimersByTimeAsync(1000)
    expect(t.streams).toHaveLength(4)
  })

  it('retries token errors with backoff', async () => {
    const t = setup()
    t.failToken(new HttpError(500))
    t.live.start()
    await flush()
    expect(t.streams).toHaveLength(0)
    t.failToken(new TypeError('offline'))
    await vi.advanceTimersByTimeAsync(1000)
    expect(t.getToken).toHaveBeenCalledTimes(2)
    t.okToken()
    await vi.advanceTimersByTimeAsync(2000)
    expect(t.streams).toHaveLength(1)
    t.last().h.onOpen()
    expect(t.resyncs()).toBe(1)
  })

  it('turns itself off for the session on 404/405 from an older server', async () => {
    for (const status of [404, 405]) {
      const t = setup()
      t.failToken(new HttpError(status))
      t.live.start()
      await flush()
      expect(t.live.disabled).toBe(true)
      await vi.advanceTimersByTimeAsync(BACKOFF_MAX_MS * 2)
      t.live.start()
      await flush()
      expect(t.getToken).toHaveBeenCalledTimes(1)
      expect(t.streams).toHaveLength(0)
    }
  })

  it('closes on stop, ignores late callbacks and reopens on start', async () => {
    const t = setup()
    t.live.start()
    await flush()
    const first = t.last()
    first.h.onOpen()
    first.h.onChange('{"topics":["facts"]}')
    t.live.stop()
    expect(first.closed).toBe(true)
    expect(t.live.connected).toBe(false)
    await vi.advanceTimersByTimeAsync(COALESCE_MS)
    first.h.onChange('{"topics":["plan"]}')
    first.h.onError()
    await vi.advanceTimersByTimeAsync(BACKOFF_MAX_MS)
    expect(t.routes).toEqual([]) // dropped: the foreground refresh covers the hidden time
    expect(t.streams).toHaveLength(1)

    t.live.start()
    await flush()
    expect(t.streams).toHaveLength(2)
    t.last().h.onOpen()
    expect(t.resyncs()).toBe(0)
  })

  it('stop while the token is on its way: the token is discarded', async () => {
    const t = setup()
    t.live.start()
    t.live.stop()
    await flush()
    expect(t.streams).toHaveLength(0)
  })

  it('start while waiting out a retry connects right away', async () => {
    const t = setup()
    t.live.start()
    await flush()
    t.last().h.onError()
    t.last().h.onError() // ignored: already closed
    t.live.start()
    await flush()
    expect(t.streams).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(BACKOFF_MAX_MS)
    expect(t.streams).toHaveLength(2)
  })

  it('a retry firing while it may not run waits for the next start', async () => {
    const t = setup()
    t.live.start()
    await flush()
    t.last().h.onError()
    t.setCanRun(false)
    await vi.advanceTimersByTimeAsync(1000)
    expect(t.getToken).toHaveBeenCalledTimes(1)
    t.setCanRun(true)
    t.live.start()
    await flush()
    expect(t.streams).toHaveLength(2)
    t.last().h.onOpen()
    expect(t.resyncs()).toBe(1)
  })
})
