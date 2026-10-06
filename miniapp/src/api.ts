// All requests carry Telegram initData; the backend validates its HMAC with the bot token
// before trusting the user id. Never trust initDataUnsafe on the server.
const initData: string =
  (window as unknown as { Telegram?: { WebApp?: { initData?: string } } }).Telegram?.WebApp?.initData ?? ''

/** Opened from Telegram (as opposed to a plain browser preview). */
export const inTelegram = initData.length > 0

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`./api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData, ...init.headers },
  })
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${await res.text()}`)
  return (res.status === 204 ? undefined : await res.json()) as T
}
