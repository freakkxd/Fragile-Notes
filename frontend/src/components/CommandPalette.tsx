import { useEffect, useState } from 'react'

type Cmd = { id: string; label: string; action: () => void }
type Props = { open: boolean; onClose: () => void; commands: Cmd[] }
export default function CommandPalette({ open, onClose, commands }: Props) {
  const [q, setQ] = useState('')
  useEffect(() => {
    if (!open) setQ('')
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    if (open) window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  const filtered = commands.filter((c) => c.label.toLowerCase().includes(q.toLowerCase())).slice(0, 20)
  return (
    <div className="palette-overlay" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input autoFocus placeholder="Команда или заметка…" value={q} onChange={(e) => setQ(e.target.value)} />
        <ul>{filtered.map((c) => <li key={c.id} onClick={() => { c.action(); onClose() }}>{c.label}</li>)}</ul>
      </div>
    </div>
  )
}
