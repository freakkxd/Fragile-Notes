import { useEffect, useMemo, useState } from 'react'

type Cmd = { id: string; label: string; action: () => void; hint?: string }
type Props = { open: boolean; onClose: () => void; commands: Cmd[] }

function fuzzyScore(q: string, label: string) {
  q = q.toLowerCase(); label = label.toLowerCase()
  let score = 0, qi = 0
  for (let i = 0; i < label.length && qi < q.length; i++) if (label[i] === q[qi]) { score += 10 - i * 0.1; qi++ }
  if (qi === q.length) return score + (label.includes(q) ? 20 : 0)
  return -1
}

export default function CommandPalette({ open, onClose, commands }: Props) {
  const [q, setQ] = useState('')
  const [idx, setIdx] = useState(0)
  useEffect(() => {
    if (!open) { setQ(''); setIdx(0) }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (!open) return
      if (e.key === 'ArrowDown') { e.preventDefault(); setIdx((i) => i + 1) }
      if (e.key === 'ArrowUp') { e.preventDefault(); setIdx((i) => Math.max(0, i - 1)) }
      if (e.key === 'Enter') { e.preventDefault(); filtered[idx]?.action(); onClose() }
    }
    if (open) window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose, q])

  const filtered = useMemo(() => {
    if (!q) return commands.slice(0, 20)
    return commands.map(c => ({ c, s: fuzzyScore(q, c.label) })).filter(x => x.s >= 0).sort((a,b)=>b.s-a.s).slice(0,20).map(x=>x.c)
  }, [q, commands])

  useEffect(()=> setIdx(0), [q])

  if (!open) return null
  return (
    <div className="palette-overlay" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input autoFocus placeholder="Поиск команды или заметки… (↑↓ Enter)" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="hint">Найдено {filtered.length} • <b>Ctrl+P</b> закрыть • <b>Ctrl+S</b> сохранить</div>
        <ul>
          {filtered.map((c, i) => (
            <li key={c.id} className={i===idx?'active':''} onClick={() => { c.action(); onClose() }}>
              <span>{c.label}</span>
              {c.hint && <span className="k">{c.hint}</span>}
            </li>
          ))}
          {!filtered.length && <li style={{ opacity:.6 }}>Ничего не найдено</li>}
        </ul>
      </div>
    </div>
  )
}
