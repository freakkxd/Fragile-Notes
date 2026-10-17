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
  background: 'var(--panel-2)',
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

export function Button({
  children,
  variant = 'secondary',
  disabled = false,
  onClick,
  type = 'button',
  ariaLabel,
  style,
}: {
  children: React.ReactNode
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  disabled?: boolean
  onClick?: (e: React.MouseEvent) => void
  type?: 'button' | 'submit' | 'reset'
  ariaLabel?: string
  style?: React.CSSProperties
}) {
  const base: React.CSSProperties = {
    height: 'var(--s-control-h, 36px)',
    padding: '0 12px',
    borderRadius: 'var(--s-radius, 8px)',
    fontSize: 12,
    fontWeight: variant === 'primary' ? 700 : 400,
    cursor: disabled ? 'not-allowed' : 'pointer',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    border: '1px solid var(--s-border, var(--border))',
    transition: 'all 0.15s ease',
  }
  const variants: Record<string, React.CSSProperties> = {
    primary: { background: 'var(--s-accent, var(--accent))', color: '#fff', borderColor: 'transparent' },
    secondary: { background: 'var(--s-surface-2, var(--panel-2))', color: 'var(--s-fg-primary, var(--fg))' },
    ghost: { background: 'transparent', color: 'var(--s-fg-secondary, var(--muted))', borderColor: 'transparent' },
    danger: { background: 'rgba(255, 80, 80, 0.12)', color: 'var(--s-danger, #ff6b6b)', borderColor: 'rgba(255, 80, 80, 0.3)' },
  }
  const disabledStyle: React.CSSProperties = disabled ? { opacity: 0.5, cursor: 'not-allowed' } : {}
  return (
    <button
      type={type}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={disabled ? undefined : onClick}
      style={{ ...base, ...variants[variant], ...disabledStyle, ...style }}
    >
      {children}
    </button>
  )
}

export function Select({
  value,
  onChange,
  children,
  disabled = false,
  style,
  ariaLabel,
}: {
  value?: string
  onChange?: (e: React.ChangeEvent<HTMLSelectElement>) => void
  children: React.ReactNode
  disabled?: boolean
  style?: React.CSSProperties
  ariaLabel?: string
}) {
  return (
    <select
      value={value}
      onChange={onChange}
      disabled={disabled}
      aria-label={ariaLabel}
      style={{
        ...inputStyle,
        height: 'var(--s-control-h, 36px)',
        appearance: 'none',
        WebkitAppearance: 'none',
        MozAppearance: 'none',
        marginTop: 0,
        ...style,
      }}
    >
      {children}
    </select>
  )
}

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
