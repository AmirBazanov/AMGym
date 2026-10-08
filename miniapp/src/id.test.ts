import { afterEach, describe, expect, it, vi } from 'vitest'
import { newId } from './id'

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('newId', () => {
  it('uses crypto.randomUUID when present', () => {
    expect(newId()).toMatch(UUID4)
  })

  it('falls back to getRandomValues without randomUUID (old WebViews)', () => {
    const real = globalThis.crypto
    const getRandomValues = vi.fn(<T extends ArrayBufferView>(a: T) => real.getRandomValues(a as never) as T)
    vi.stubGlobal('crypto', { getRandomValues })
    const a = newId()
    const b = newId()
    expect(a).toMatch(UUID4)
    expect(a).not.toBe(b)
    expect(getRandomValues).toHaveBeenCalledTimes(2)
  })
})
