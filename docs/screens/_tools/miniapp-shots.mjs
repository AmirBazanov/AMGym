// Mini App screenshots for README: docs/screens/miniapp-<screen>-<light|dark>.png.
// Needs a local API-only server with demo data: built miniapp/dist, then `python -m gymbot.main` with
// RUN_BOT=false, DEV_USER_ID=1, DATABASE_URL pointing to a temporary SQLite file (never data/gym.db) and empty
// LLM keys. Demo data goes in through the same API as the dev user (the 07.10 shots used program start
// 2026-09-14 and were taken on 2026-10-08). Usage: BASE=http://127.0.0.1:8000 node miniapp-shots.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const BASE = process.env.BASE ?? 'http://127.0.0.1:8000'
const OUT = process.env.OUT ?? path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SCALE = Number(process.env.SCALE ?? 2)

/** Scrolls so the element with this exact text sits near the top of the viewport. */
async function scrollTo(page, text) {
  const el = page.getByText(text, { exact: true }).first()
  await el.waitFor()
  await el.evaluate((n) => window.scrollTo({ top: n.getBoundingClientRect().top + window.scrollY - 12 }))
}

const SCREENS = [
  ['today', 'today', null],
  ['program', 'program', null],
  ['history', 'history', async (p) => p.getByText('Ср, 7 октября').click()],
  ['progress', 'progress', null],
  ['weight', 'progress', async (p) => scrollTo(p, 'Вес тела')],
  ['nutrition', 'nutrition', null],
  ['settings', 'nutrition', async (p) => {
    await p.getByRole('button', { name: 'Настройки' }).click()
    await scrollTo(p, 'Норма в день')
  }],
]

const browser = await chromium.launch()
for (const scheme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: SCALE,
    colorScheme: scheme,
    timezoneId: 'Europe/Moscow',
    locale: 'ru-RU',
  })
  await ctx.route(/telegram\.org/, (r) => r.abort()) // outside Telegram the SDK script is not needed
  const page = await ctx.newPage()
  for (const [name, tab, act] of SCREENS) {
    await page.goto(`${BASE}/?tab=${tab}`)
    await page.waitForLoadState('networkidle')
    if (act) await act(page)
    await page.waitForTimeout(1500) // chart animations
    const file = path.join(OUT, `miniapp-${name}-${scheme}.png`)
    await page.screenshot({ path: file })
    console.log(file)
  }
  await ctx.close()
}
await browser.close()
