import { useState } from 'react'

export function SectionCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <h4 style={{ margin: 0 }}>{title}</h4>
      {children}
    </div>
  )
}

export function SettingRow({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, minWidth: 200, flex: 1 }}>
      <span>{label} {hint && <span style={{ color: 'var(--muted)', fontWeight: 400 }} title={hint}>ⓘ</span>}</span>
      {children}
    </label>
  )
}

export const inputStyle: React.CSSProperties = {
  width: '100%',
  marginTop: 4,
  padding: '6px 8px',
  background: 'rgba(0,0,0,.25)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  color: 'var(--fg)',
  fontSize: 12,
}

export function StatusBadge({ state }: { state: 'ready' | 'unknown' | 'error' | 'testing' | 'off' }) {
  const map = {
    ready: { dot: 'var(--accent-2)', text: 'Готов', label: 'Готов' },
    unknown: { dot: 'var(--muted)', text: 'Не проверен', label: 'Не проверен' },
    error: { dot: '#ff6b6b', text: 'Ошибка', label: 'Ошибка' },
    testing: { dot: 'var(--accent-warn)', text: 'Проверка…', label: 'Проверка' },
    off: { dot: 'var(--muted)', text: 'Выкл', label: 'Выкл' },
  } as const
  const s = map[state]
  return (
    <span role="status" aria-label={`Статус: ${s.label}`} style={{ display: 'inline-flex', gap: 6, alignItems: 'center', fontSize: 12 }}>
      <span aria-hidden="true" style={{ color: s.dot }}>●</span> {s.text}
    </span>
  )
}

/** Keyring reference field: never shows or persists a raw key. */
export function SecretReferenceField({
  hasSecret,
  onReplace,
  onDelete,
}: {
  hasSecret: boolean | undefined
  onReplace: (secret: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  if (!editing) {
    return (
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap', fontSize: 12 }}>
        <span style={{ color: 'var(--muted)' }}>
          {hasSecret === true ? 'Ключ сохранён в системном хранилище' : hasSecret === false ? 'Ключ не сохранён' : 'Состояние ключа неизвестно'}
        </span>
        <button
          onClick={() => { setValue(''); setEditing(true) }}
          style={{ padding: '6px 10px', background: 'var(--panel-2)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}
        >
          {hasSecret ? 'Заменить ключ' : 'Добавить ключ'}
        </button>
        {hasSecret && (
          <button
            onClick={onDelete}
            style={{ padding: '6px 10px', background: 'rgba(255,80,80,.12)', border: '1px solid rgba(255,80,80,.3)', borderRadius: 8, fontSize: 12 }}
          >
            Удалить ключ
          </button>
        )}
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      <input
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Вставьте ключ (не сохраняется в конфиг)"
        aria-label="Новый API ключ"
        style={{ ...inputStyle, marginTop: 0, flex: 1, minWidth: 220 }}
      />
      <button
        onClick={() => { if (value.trim()) { onReplace(value.trim()); setValue(''); setEditing(false) } }}
        disabled={!value.trim()}
        style={{ padding: '6px 10px', background: 'var(--accent)', border: 0, borderRadius: 8, fontWeight: 700, fontSize: 12 }}
      >
        Сохранить в keyring
      </button>
      <button
        onClick={() => { setValue(''); setEditing(false) }}
        style={{ padding: '6px 10px', background: 'var(--panel-2)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}
      >
        Отмена
      </button>
    </div>
  )
}
