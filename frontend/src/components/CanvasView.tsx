export default function CanvasView() {
  return (
    <div className="canvas-view">
      <h3 style={{ margin:'4px 0 6px' }}>Canvas — бесконечная доска</h3>
      <p style={{ color:'var(--muted)', fontSize:13 }}>Drag & drop заметок, стрелки, группы, mindmap — как в Obsidian Canvas.</p>
      <div className="graph-placeholder">🎨 Canvas — перемещай карточки, соединяй стрелками, группируй (Excalidraw-lite)</div>
      <div style={{ marginTop:10, display:'flex', gap:8 }}>
        <button disabled title="Заметки на доске — не реализовано" style={{ padding:'7px 10px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:600, opacity:.5, cursor:'not-allowed' }}>+ Заметка</button>
        <button disabled title="Стрелки — не реализовано" style={{ padding:'7px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, opacity:.5, cursor:'not-allowed' }}>+ Стрелка</button>
        <button disabled title="Группы — не реализовано" style={{ padding:'7px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, opacity:.5, cursor:'not-allowed' }}>+ Группа</button>
      </div>
    </div>
  )
}
