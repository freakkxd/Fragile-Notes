type Tab = { path: string; dirty: boolean }
type Props = { tabs: Tab[]; active: string | null; onSwitch: (p: string) => void; onClose: (p: string) => void }
export default function TabBar({ tabs, active, onSwitch, onClose }: Props) {
  if (!tabs.length) return <div style={{ padding: '8px 12px', fontSize: 12, color: 'var(--muted)', borderBottom: '1px solid var(--border)', background: 'var(--panel)' }}>Нет открытых заметок — выбери файл слева или <b>Ctrl+P</b></div>
  return (
    <div className="tabbar">
      {tabs.map((t) => (
        <div key={t.path} className={`tab ${active === t.path ? 'active' : ''} ${t.dirty ? 'dirty' : ''}`} onClick={() => onSwitch(t.path)} title={t.path}>
          <span className="pin" />
          <span className="tab-title">{t.path.split('/').pop()}{t.dirty ? ' •' : ''}</span>
          <button className="tab-close" onClick={(e) => { e.stopPropagation(); onClose(t.path) }}>×</button>
        </div>
      ))}
      <div style={{ flex: 1, background: 'var(--panel)' }} />
    </div>
  )
}
