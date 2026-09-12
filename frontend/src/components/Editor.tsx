type Props = { value: string; onChange: (v: string) => void }

export default function Editor({ value, onChange }: Props) {
  return (
    <textarea
      className="md-editor"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      spellCheck={false}
      placeholder="Пиши заметку в Markdown — [[ссылки]], #теги, - [ ] задачи"
    />
  )
}
