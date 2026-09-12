type Props = { files: string[]; current: string | null; onOpen: (p: string) => void }

function groupByDir(files: string[]) {
  const tree: Record<string, string[]> = {}
  for (const f of files) {
    const parts = f.split('/')
    const dir = parts.length > 1 ? parts.slice(0, -1).join('/') : '/'
    if (!tree[dir]) tree[dir] = []
    tree[dir].push(f)
  }
  return Object.entries(tree).sort(([a], [b]) => a.localeCompare(b))
}

export default function FileTree({ files, current, onOpen }: Props) {
  if (!files.length) return <div className="empty">vault пуст — создай заметку</div>
  const grouped = groupByDir(files)
  return (
    <div className="file-tree">
      {grouped.map(([dir, items]) => (
        <div key={dir} className="dir">
          <div className="dir-name">{dir}</div>
          <ul>
            {items.map((f) => (
              <li key={f} className={current === f ? 'active' : ''} onClick={() => onOpen(f)} title={f}>
                {f.split('/').pop()}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}
