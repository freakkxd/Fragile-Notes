type Tab = { path: string; dirty: boolean }
type Props = { tabs: Tab[]; active: string | null; onSwitch: (p: string) => void; onClose: (p: string) => void }
export default function TabBar({ tabs, active, onSwitch, onClose }: Props) {
  if (!tabs.length) return null
  return (
    <div className="tabbar">
      {tabs.map((t) => (
        <div key={t.path} className={`tab ${active === t.path ? 'active' : ''}`} onClick={() => onSwitch(t.path)}>
          <span className="tab-title">{t.path.split('/').pop()}{t.dirty ? ' •' : ''}</span>
          <button className="tab-close" onClick={(e) => { e.stopPropagation(); onClose(t.path) }}>×</button>
        </div>
      ))}
    </div>
  )
}
