import { askInPage } from './components/ConfirmHost'

// Thin wrapper over window.Telegram.WebApp. Outside Telegram (browser preview) every call is a no-op.
type HapticStyle = 'light' | 'medium' | 'heavy'

interface TgWebApp {
  ready(): void
  expand(): void
  colorScheme?: 'light' | 'dark'
  themeParams?: Record<string, string>
  setHeaderColor?(color: string): void
  setBackgroundColor?(color: string): void
  onEvent?(event: string, cb: () => void): void
  offEvent?(event: string, cb: () => void): void
  HapticFeedback?: {
    impactOccurred(style: HapticStyle): void
    notificationOccurred(type: 'success' | 'warning' | 'error'): void
    selectionChanged(): void
  }
  showConfirm?(message: string, cb: (ok: boolean) => void): void
}

export const tg: TgWebApp | undefined = (window as unknown as { Telegram?: { WebApp?: TgWebApp } }).Telegram
  ?.WebApp

// Telegram injects --tg-theme-* CSS variables itself; we only mirror the color scheme
// onto <html data-theme> so our fallbacks match when the app runs in a plain browser.
function applyScheme() {
  const inTelegram = Boolean(tg?.themeParams && Object.keys(tg.themeParams).length)
  const root = document.documentElement
  // A host page (browser preview) may already pin a theme; keep it.
  if (!inTelegram && root.dataset.tg !== '0' && root.dataset.theme) return
  const dark = inTelegram ? tg?.colorScheme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches
  root.dataset.theme = dark ? 'dark' : 'light'
  document.documentElement.dataset.tg = inTelegram ? '1' : '0'
}

export function initTelegram() {
  applyScheme()
  tg?.ready()
  tg?.expand()
  tg?.onEvent?.('themeChanged', applyScheme)
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyScheme)
}

/**
 * Calls `cb` when the app comes back to the foreground: the page becomes visible or Telegram reports
 * `activated` (a minimized Mini App reopened; there `visibilitychange` may not fire). Not window `focus`:
 * it also fires after native dialogs, and a reload then could bring back an item being deleted.
 * Signals within FOREGROUND_GAP_MS of each other count once. Returns the unsubscribe function.
 */
export function onForeground(cb: () => void): () => void {
  let last = 0
  const fire = () => {
    if (document.visibilityState === 'hidden') return
    const now = Date.now()
    if (now - last < FOREGROUND_GAP_MS) return
    last = now
    cb()
  }
  document.addEventListener('visibilitychange', fire)
  tg?.onEvent?.('activated', fire)
  return () => {
    document.removeEventListener('visibilitychange', fire)
    tg?.offEvent?.('activated', fire)
  }
}

const FOREGROUND_GAP_MS = 1000

export const haptic = {
  tap: () => tg?.HapticFeedback?.impactOccurred('light'),
  select: () => tg?.HapticFeedback?.selectionChanged(),
  success: () => tg?.HapticFeedback?.notificationOccurred('success'),
  error: () => tg?.HapticFeedback?.notificationOccurred('error'),
}

export function confirm(message: string): Promise<boolean> {
  const show = tg?.showConfirm
  if (!show) return askInPage(message)
  return new Promise((resolve) => {
    try {
      show.call(tg, message, resolve)
    } catch {
      // Older clients throw outside of supported versions; fall back to the in-page dialog.
      askInPage(message).then(resolve)
    }
  })
}
