// Live updates: changes made from the bot chat reach the open Mini App without a reopen.
// POST /api/live/token (initData) -> short-lived token -> EventSource GET /api/live?token=...
// The stream is open only while the page is visible; the logic lives in liveCore.ts.
import { api, ApiError } from './api'
import { createLive, emitRemoteRefresh, type LiveController } from './liveCore'
import { getState } from './store'
import { onBackground, onForeground } from './telegram'

let controller: LiveController | null = null

/**
 * Starts live updates once, in server mode only. `refresh` is the app's full store refresh (sync with
 * the server, then prepare today's workout): it keeps a started workout's edits, see applyServer.
 */
export function startLive(refresh: () => Promise<void>): void {
  if (controller || typeof EventSource === 'undefined' || typeof document === 'undefined') return
  const visible = () => document.visibilityState !== 'hidden'
  const live = createLive({
    getToken: () => api<{ token: string }>('/live/token', { method: 'POST' }).then((r) => r.token),
    open: (token, h) => {
      const es = new EventSource(`./api/live?token=${encodeURIComponent(token)}`)
      es.onopen = () => h.onOpen()
      es.addEventListener('change', (e) => h.onChange((e as MessageEvent<string>).data))
      // The browser would retry with the same (soon expired) token; we reconnect with a fresh one.
      es.onerror = () => {
        es.close()
        h.onError()
      }
      return es
    },
    canRun: () => visible() && getState().mode === 'server',
    errorStatus: (e) => (e instanceof ApiError ? e.status : undefined),
    dispatch: (route) => {
      if (route.sync) void refresh()
      emitRemoteRefresh(route.remote)
    },
    resync: () => {
      void refresh()
      emitRemoteRefresh('all')
    },
  })
  controller = live

  onBackground(() => live.stop())
  onForeground(() => live.start())
  live.start()
}
