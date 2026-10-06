// All requests carry Telegram initData; the backend validates its HMAC with the bot token
// before trusting the user id. Never trust initDataUnsafe on the server.
const initData = (window as any).Telegram?.WebApp?.initData ?? ''

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData, ...init.headers },
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}
