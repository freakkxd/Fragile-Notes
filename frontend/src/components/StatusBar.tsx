type Props = { status: string; words: number; chars: number }
export default function StatusBar({ status, words, chars }: Props) {
  return (
    <div className="statusbar">
      <span><b>{status}</b> • {words} слов • {chars} симв. • UTF-8 • Markdown</span>
      <span>Fragile Notes v0.4.6 • Tauri+C++ • <span style={{ color:'var(--accent)' }}>●</span> FTS5 • <span style={{ color:'var(--accent-2)' }}>●</span> CRDT</span>
    </div>
  )
}
