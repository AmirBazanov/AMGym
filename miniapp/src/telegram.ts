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
