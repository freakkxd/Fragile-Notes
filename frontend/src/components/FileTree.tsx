import { useState, useMemo } from 'react'

type Props = { files: string[]; current: string | null; onOpen: (p: string) => void; onNew?: () => void }

const ICON: Record<string, string> = {
  md: '📄', txt: '📝', png: '🖼️', jpg: '🖼️', svg: '🖼️', pdf: '📕', css: '🎨', js: '📜', ts: '📜', json: '🧩', py: '🐍', rs: '🦀', cpp: '⚙️',
}

function extIcon(path: string) {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return ICON[ext] || '📄'
}

function groupFiles(files: string[]) {
  const map: Record<string, string[]> = {}
  for (const f of files) {
    const parts = f.split('/')
    const dir = parts.length > 1 ? parts.slice(0, -1).join('/') : '/'
    if (!map[dir]) map[dir] = []
    map[dir].push(f)
  }
  return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
}

export default function FileTree({ files, current, onOpen, onNew }: Props) {
  const grouped = useMemo(() => groupFiles(files), [files])
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  if (!files.length)
    return (
      <div className="empty">
        <div>vault пуст — создай первую заметку</div>
        <div className="cta" onClick={onNew}>+ Новая заметка</div>
        <div style={{ marginTop: 8, fontSize: 11, opacity: 0.7 }}>Поддержка `[[wikilinks]]` • `#tags` • `- [ ]`</div>
      </div>
    )

  return (
    <div className="file-tree">
      {grouped.map(([dir, items]) => {
        const isCollapsed = collapsed[dir] || false
        return (
          <div key={dir} className={`dir ${isCollapsed ? 'collapsed' : ''}`}>
            <div className="dir-head" onClick={() => setCollapsed((c) => ({ ...c, [dir]: !isCollapsed }))}>
              <span className="chev">▾</span>
              <span>{dir === '/' ? 'Корень' : dir}</span>
              <span style={{ marginLeft: 'auto', opacity: 0.6 }}>{items.length}</span>
            </div>
            <ul>
              {items.map((f) => (
                <li key={f} className={current === f ? 'active' : ''} onClick={() => onOpen(f)} title={f}>
                  <span className="ico">{extIcon(f)}</span>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.split('/').pop()}</span>
                  {current === f && <span style={{ marginLeft: 'auto', fontSize: 10 }}>●</span>}
                </li>
              ))}
            </ul>
          </div>
        )
      })}
    </div>
  )
}
