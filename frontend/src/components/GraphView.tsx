type Props = { files: string[] }
export default function GraphView({ files }: Props) {
  return (
    <div className="graph-view">
      <h3 style={{ margin:'4px 0 6px' }}>Граф связей</h3>
      <p style={{ color:'var(--muted)', fontSize:13 }}>Заметок: <b style={{ color:'var(--fg)' }}>{files.length}</b> — связи из <code>[[wikilinks]]</code> и <code>#tags</code> (C++ `link_index`).</p>
      <div className="graph-placeholder">🕸 интерактивный граф — узлы масштабируются, drag, hover preview (скоро d3-force)</div>
      <div className="card" style={{ marginTop:12 }}>
        <h4>Легенда</h4>
        <div style={{ fontSize:12, color:'var(--muted)', display:'flex', gap:12, flexWrap:'wrap' }}>
          <span><span style={{ color:'var(--accent)' }}>●</span> Заметка</span>
          <span><span style={{ color:'var(--accent-2)' }}>●</span> Тег</span>
          <span>─ связь</span>
          <span>толще = чаще упоминается</span>
        </div>
      </div>
    </div>
  )
}
