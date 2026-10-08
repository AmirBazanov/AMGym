// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sheet } from './Sheet'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let container: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  // Not '': a cleared value must not pass for a restored one.
  document.body.style.overflow = 'auto'
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

let closeAll: () => void = () => {}

function Stack({ child }: { child: boolean }) {
  const [open, setOpen] = useState(true)
  closeAll = () => setOpen(false)
  if (!open) return null
  return (
    <Sheet onClose={() => setOpen(false)}>
      outer
      {child && <Sheet onClose={() => {}}>inner</Sheet>}
    </Sheet>
  )
}

describe('Sheet scroll lock', () => {
  it('restores the original overflow after parent and child unmount together', () => {
    // The child opens after the parent (the editor, then its scope sheet), then one update closes both.
    act(() => root.render(<Stack child={false} />))
    act(() => root.render(<Stack child />))
    expect(document.body.style.overflow).toBe('hidden')
    act(() => closeAll())
    expect(document.body.style.overflow).toBe('auto')
  })

  it('keeps the lock while the parent stays open after the child closes', () => {
    act(() => root.render(<Stack child />))
    act(() => root.render(<Stack child={false} />))
    expect(document.body.style.overflow).toBe('hidden')
    act(() => closeAll())
    expect(document.body.style.overflow).toBe('auto')
  })

  it('Escape closes only the topmost sheet', () => {
    const outer = vi.fn()
    const inner = vi.fn()
    act(() =>
      root.render(
        <Sheet onClose={outer}>
          <Sheet onClose={inner}>x</Sheet>
        </Sheet>,
      ),
    )
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    })
    expect(inner).toHaveBeenCalledTimes(1)
    expect(outer).not.toHaveBeenCalled()
  })
})

describe('Sheet and the Telegram back button', () => {
  it('shows it while a sheet is open; a click closes the topmost sheet; hides it after the last one', async () => {
    const handlers = new Set<() => void>()
    const BackButton = {
      show: vi.fn(),
      hide: vi.fn(),
      onClick: vi.fn((cb: () => void) => handlers.add(cb)),
      offClick: vi.fn((cb: () => void) => handlers.delete(cb)),
    }
    ;(window as unknown as { Telegram?: unknown }).Telegram = { WebApp: { BackButton } }
    vi.resetModules()
    try {
      const { Sheet: TgSheet } = await import('./Sheet')
      const outer = vi.fn()
      function Two({ inner }: { inner: boolean }) {
        return <TgSheet onClose={outer}>{inner && <TgSheet onClose={() => root.render(<Two inner={false} />)}>x</TgSheet>}</TgSheet>
      }
      act(() => root.render(<Two inner={false} />))
      act(() => root.render(<Two inner />))
      expect(BackButton.show).toHaveBeenCalledTimes(1)
      act(() => handlers.forEach((h) => h()))
      expect(outer).not.toHaveBeenCalled()
      expect(BackButton.hide).not.toHaveBeenCalled()
      act(() => root.render(<></>))
      expect(BackButton.hide).toHaveBeenCalledTimes(1)
      expect(handlers.size).toBe(0)
    } finally {
      delete (window as unknown as { Telegram?: unknown }).Telegram
      vi.resetModules()
    }
  })
})
