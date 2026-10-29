import { useMemo } from 'react'
import { parseMarkdown, type MdBlock, type MdInline } from '../life'

// Markdown of the notes, rendered from life.parseMarkdown's tree as React elements: text is always text
// (React escapes it), never HTML, and only http(s) links (life.safeHref) become <a>.

function Inline({ nodes }: { nodes: MdInline[] }) {
  return (
    <>
      {nodes.map((n, i) => {
        switch (n.t) {
          case 'text':
            return n.text
          case 'code':
            return <code key={i}>{n.text}</code>
          case 'bold':
            return (
              <strong key={i}>
                <Inline nodes={n.children} />
              </strong>
            )
          case 'italic':
            return (
              <em key={i}>
                <Inline nodes={n.children} />
              </em>
            )
          case 'link':
            return (
              <a key={i} href={n.href} target="_blank" rel="noopener noreferrer">
                <Inline nodes={n.children} />
              </a>
            )
        }
      })}
    </>
  )
}

function Lines({ lines }: { lines: MdInline[][] }) {
  return (
    <>
      {lines.map((l, i) => (
        <span key={i}>
          {i > 0 && <br />}
          <Inline nodes={l} />
        </span>
      ))}
    </>
  )
}

function Block({ b }: { b: MdBlock }) {
  switch (b.t) {
    case 'h': {
      const H = (`h${b.level + 2}` as 'h3' | 'h4' | 'h5')
      return (
        <H className="md-h">
          <Inline nodes={b.inline} />
        </H>
      )
    }
    case 'p':
      return (
        <p>
          <Lines lines={b.lines} />
        </p>
      )
    case 'ul':
    case 'ol': {
      const L = b.t
      return (
        <L>
          {b.items.map((it, i) => (
            <li key={i} className={it.checked == null ? undefined : 'md-task'}>
              {it.checked != null && <span className="md-box">{it.checked ? '☑' : '☐'} </span>}
              <Inline nodes={it.inline} />
            </li>
          ))}
        </L>
      )
    }
    case 'quote':
      return (
        <blockquote>
          <Lines lines={b.lines} />
        </blockquote>
      )
    case 'code':
      return (
        <pre>
          <code>{b.text}</code>
        </pre>
      )
    case 'hr':
      return <hr />
  }
}

export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => parseMarkdown(text), [text])
  if (!blocks.length) return <p className="hint">Пусто.</p>
  return (
    <div className="md">
      {blocks.map((b, i) => (
        <Block key={i} b={b} />
      ))}
    </div>
  )
}
