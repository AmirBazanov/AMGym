// Renders every dialog of chat.html (a static Telegram chat mock) to docs/screens/chat-<name>.png.
// Usage: node chat-shots.mjs
import { chromium } from 'playwright'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const OUT = process.env.OUT ?? path.resolve(here, '..')
const url = pathToFileURL(path.join(here, 'chat.html')).href

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 420, height: 400 }, deviceScaleFactor: 2, colorScheme: 'light' })
await page.goto(url)
const names = await page.evaluate(() => window.DIALOG_NAMES)
for (const name of names) {
  await page.goto(`${url}?d=${name}`)
  const file = path.join(OUT, `chat-${name}.png`)
  await page.locator('#chat').screenshot({ path: file })
  console.log(file)
}
await browser.close()
