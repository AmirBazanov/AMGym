import { useEffect, useState } from 'react'

function parse(v: string): number | null {
  const n = parseFloat(v.replace(',', '.'))
  return Number.isFinite(n) && n >= 0 ? n : null
}

// Keeps the raw text locally so partial input like "12," survives re-renders.
export function NumField({
  value,
  onChange,
  placeholder,
  decimal,
}: {
  value: number | null
  onChange: (v: number | null) => void
  placeholder?: string
  decimal?: boolean
}) {
  const [text, setText] = useState(value == null ? '' : String(value).replace('.', ','))
  useEffect(() => {
    if (parse(text) !== value) setText(value == null ? '' : String(value).replace('.', ','))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  return (
    <input
      className="field"
      inputMode={decimal ? 'decimal' : 'numeric'}
      placeholder={placeholder ?? '—'}
      value={text}
      onFocus={(e) => e.target.select()}
      onChange={(e) => {
        const t = e.target.value.replace(/[^\d.,]/g, '')
        setText(t)
        onChange(parse(t))
      }}
    />
  )
}
