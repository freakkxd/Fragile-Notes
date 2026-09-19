type Props = { value: string; onChange: (v: string) => void; onSave?: () => void }

function insertAtCursor(textarea: HTMLTextAreaElement, before: string, after = '', placeholder = '') {
  const start = textarea.selectionStart
  const end = textarea.selectionEnd
  const sel = textarea.value.slice(start, end) || placeholder
  const next = textarea.value.slice(0, start) + before + sel + after + textarea.value.slice(end)
  return { next, cursor: start + before.length + sel.length + after.length }
}

export default function Editor({ value, onChange, onSave }: Props) {
  const refAction = (fn: (ta: HTMLTextAreaElement) => void) => {
    const ta = document.querySelector('.md-editor') as HTMLTextAreaElement | null
    if (ta) fn(ta)
  }

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault()
      onSave?.()
    }
    if (e.key === 'Tab') {
      e.preventDefault()
      const ta = e.currentTarget
      const { next } = insertAtCursor(ta, '  ')
      onChange(next)
    }
  }

  return (
    <div className="editor-wrap">
      <div className="editor-toolbar">
        <button title="Bold (Ctrl+B)" onClick={() => refAction((ta) => { const { next } = insertAtCursor(ta, '**', '**', 'жирный'); onChange(next); ta.focus() })}>𝐁</button>
        <button title="Italic" onClick={() => refAction((ta) => { const { next } = insertAtCursor(ta, '*', '*', 'курсив'); onChange(next); ta.focus() })}><i>I</i></button>
        <button title="H1" onClick={() => refAction((ta) => { const { next } = insertAtCursor(ta, '# ', '', 'Заголовок'); onChange(next); ta.focus() })}>H1</button>
        <button title="Link [[ ]]" onClick={() => refAction((ta) => { const { next } = insertAtCursor(ta, '[[', ']]', 'Заметка'); onChange(next); ta.focus() })}>🔗</button>
        <button title="List" onClick={() => refAction((ta) => { const { next } = insertAtCursor(ta, '- ', '', 'пункт'); onChange(next); ta.focus() })}>•</button>
        <button title="Task" onClick={() => refAction((ta) => { const { next } = insertAtCursor(ta, '- [ ] ', '', 'задача'); onChange(next); ta.focus() })}>✓</button>
        <button title="Code" onClick={() => refAction((ta) => { const { next } = insertAtCursor(ta, '`', '`', 'код'); onChange(next); ta.focus() })}>{'</>'}</button>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }}>{value.length} симв. • {value.split('\n').length} строк • Markdown • Tab=2 пробела</span>
      </div>
      <textarea
        className="md-editor"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKey}
        spellCheck={false}
        placeholder="Пиши в Markdown — [[ссылки]], #теги, - [ ] задачи, ```код```, | таблицы |, > [!note] callout"
      />
    </div>
  )
}
