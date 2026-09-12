type Props = { files: string[] }
export default function GraphView({ files }: Props) {
  return (
    <div className="graph-view">
      <h3>Граф связей</h3>
      <p>Заметок: {files.length} — связи парсятся из [[wikilinks]] (C++ vault).</p>
      <div className="graph-placeholder">🕸 canvas graph — d3 stub</div>
    </div>
  )
}
