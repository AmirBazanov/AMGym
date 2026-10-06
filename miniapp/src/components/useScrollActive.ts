import { useEffect, useRef } from 'react'

/** Keeps the active chip of a horizontal chip row in view. */
export function useScrollActive<T extends HTMLElement>(dep: unknown) {
  const ref = useRef<T>(null)
  useEffect(() => {
    const el = ref.current?.querySelector<HTMLElement>('.active')
    const row = ref.current
    if (el && row) row.scrollTo({ left: el.offsetLeft - row.clientWidth / 2 + el.clientWidth / 2, behavior: 'smooth' })
  }, [dep])
  return ref
}
