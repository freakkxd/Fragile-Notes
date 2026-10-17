import { useEffect, useId, useRef, useState } from 'react'
import type {
  ChangeEvent,
  CSSProperties,
  InputHTMLAttributes,
  KeyboardEvent,
  MouseEvent,
  ReactNode,
  TextareaHTMLAttributes,
} from 'react'

/**
 * Scoped Settings design system.
 *
 * These primitives are the single source of truth for the LLM Settings UI:
 * buttons, fields, inputs, the accessible select, cards, status/help/error
 * text. They are intentionally small — no UI framework, no dependencies —
 * and consume only the `--s-*` design tokens from styles.css so every tab
 * stays visually consistent.
 */

export const inputStyle: CSSProperties = {
  width: '100%',
  marginTop: 4,
  padding: '6px 8px',
  background: 'var(--s-surface-2)',
  border: '1px solid var(--s-border)',
  borderRadius: 'var(--s-radius)',
  color: 'var(--s-fg-primary)',
  fontSize: 12,
}

// ── Layout ───────────────────────────────────────────────────────────────

export function SectionCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-space-2)' }}>
      <h4 style={{ margin: 0 }}>{title}</h4>
      {children}
    </div>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
  style,
}: {
  label: string
  hint?: string
  error?: string
  children: ReactNode
  style?: CSSProperties
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, minWidth: 200, flex: 1, ...style }}>
      <span>
        {label}{' '}
        {hint && (
          <span style={{ color: 'var(--s-fg-secondary)', fontWeight: 400 }} title={hint}>
            ⓘ
          </span>
        )}
      </span>
      {children}
      {error && (
        <span role="alert" style={{ color: 'var(--s-danger)', fontSize: 11 }}>
          {error}
        </span>
      )}
    </label>
  )
}

/** Backwards-compatible alias of {@link Field}. */
export const SettingRow = Field

// ── Text ─────────────────────────────────────────────────────────────────

export function HelpText({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return <span style={{ fontSize: 11, color: 'var(--s-fg-secondary)', ...style }}>{children}</span>
}

type NoticeTone = 'info' | 'warn' | 'danger' | 'success'

export function Notice({
  tone = 'info',
  role,
  children,
  style,
}: {
  tone?: NoticeTone
  role?: 'alert' | 'status'
  children: ReactNode
  style?: CSSProperties
}) {
  const palette: Record<NoticeTone, CSSProperties> = {
    info: { background: 'rgba(120,180,255,.07)', borderColor: 'var(--s-border)' },
    warn: { background: 'rgba(255,180,80,.08)', borderColor: 'var(--s-border)' },
    danger: { background: 'rgba(255,80,80,.08)', borderColor: 'rgba(255,80,80,.4)' },
    success: { background: 'rgba(139,213,202,.08)', borderColor: 'var(--s-border)' },
  }
  return (
    <div
      role={role}
      style={{
        padding: '8px 10px',
        border: '1px solid var(--s-border)',
        borderRadius: 'var(--s-radius)',
        fontSize: 12,
        ...palette[tone],
        ...style,
      }}
    >
      {children}
    </div>
  )
}

export function StatusBadge({ state }: { state: 'ready' | 'unknown' | 'error' | 'testing' | 'off' }) {
  const map = {
    ready: { dot: 'var(--accent-2)', text: 'Готов', label: 'Готов' },
    unknown: { dot: 'var(--s-fg-secondary)', text: 'Не проверен', label: 'Не проверен' },
    error: { dot: 'var(--s-danger)', text: 'Ошибка', label: 'Ошибка' },
    testing: { dot: 'var(--s-accent-warn)', text: 'Проверка…', label: 'Проверка' },
    off: { dot: 'var(--s-fg-secondary)', text: 'Выкл', label: 'Выкл' },
  } as const
  const s = map[state]
  return (
    <span role="status" aria-label={`Статус: ${s.label}`} style={{ display: 'inline-flex', gap: 6, alignItems: 'center', fontSize: 12 }}>
      <span aria-hidden="true" style={{ color: s.dot }}>●</span> {s.text}
    </span>
  )
}

// ── Buttons ──────────────────────────────────────────────────────────────

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
type ButtonSize = 'md' | 'sm'

export function Button({
  children,
  variant = 'secondary',
  size = 'md',
  disabled = false,
  onClick,
  type = 'button',
  ariaLabel,
  title,
  style,
}: {
  children: ReactNode
  variant?: ButtonVariant
  size?: ButtonSize
  disabled?: boolean
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void
  type?: 'button' | 'submit' | 'reset'
  ariaLabel?: string
  title?: string
  style?: CSSProperties
}) {
  const height = size === 'sm' ? 28 : 'var(--s-control-h)'
  const base: CSSProperties = {
    height,
    padding: size === 'sm' ? '0 8px' : '0 12px',
    borderRadius: size === 'sm' ? 'var(--s-radius-sm)' : 'var(--s-radius)',
    fontSize: size === 'sm' ? 11 : 12,
    cursor: disabled ? 'not-allowed' : 'pointer',
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    lineHeight: 1,
    border: '1px solid var(--s-border)',
    transition: 'filter .15s ease, background .15s ease',
  }
  const variants: Record<ButtonVariant, CSSProperties> = {
    primary: { background: 'var(--s-accent)', color: 'var(--s-accent-ink)', borderColor: 'transparent', fontWeight: 700 },
    secondary: { background: 'var(--s-surface-2)', color: 'var(--s-fg-primary)' },
    ghost: { background: 'transparent', color: 'var(--s-fg-secondary)', borderColor: 'transparent' },
    danger: { background: 'rgba(255,80,80,.12)', color: 'var(--s-danger)', borderColor: 'rgba(255,80,80,.3)' },
  }
  const disabledStyle: CSSProperties = disabled ? { opacity: 0.5 } : {}
  return (
    <button
      type={type}
      aria-label={ariaLabel}
      title={title}
      disabled={disabled}
      onClick={disabled ? undefined : onClick}
      style={{ ...base, ...variants[variant], ...disabledStyle, ...style }}
    >
      {children}
    </button>
  )
}

// ── Inputs ───────────────────────────────────────────────────────────────

export function Input({ small = false, style, ...rest }: InputHTMLAttributes<HTMLInputElement> & { small?: boolean }) {
  return (
    <input
      {...rest}
      style={{
        ...inputStyle,
        ...(small ? { marginTop: 0, padding: '4px 6px', borderRadius: 'var(--s-radius-sm)', fontSize: 11 } : null),
        ...style,
      }}
    />
  )
}

export function TextArea({ small = false, style, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement> & { small?: boolean }) {
  return (
    <textarea
      {...rest}
      style={{
        ...inputStyle,
        fontFamily: 'var(--font)',
        ...(small ? { marginTop: 0, padding: 6, borderRadius: 'var(--s-radius-sm)', fontSize: 11 } : null),
        ...style,
      }}
    />
  )
}

export function Checkbox({
  label,
  checked,
  onChange,
  disabled = false,
  title,
}: {
  label: string
  checked: boolean
  onChange: (e: ChangeEvent<HTMLInputElement>) => void
  disabled?: boolean
  title?: string
}) {
  return (
    <label title={title} style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12, color: disabled ? 'var(--s-fg-disabled)' : 'var(--s-fg-primary)', cursor: disabled ? 'not-allowed' : 'pointer' }}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={onChange} style={{ accentColor: 'var(--s-accent)' }} />
      {label}
    </label>
  )
}

// ── Accessible select (real combobox + listbox) ───────────────────────────
//
// WebKitGTK renders the native `<select>` popup with the GTK theme, which CSS
// cannot style reliably, so expanded options were unreadable on the dark UI.
// This control replaces it: the trigger is a real `role="combobox"` focus
// owner and the popup is a `role="listbox"` of `role="option"` nodes, fully
// styled with the `--s-*` tokens. Keyboard follows the collapsible-listbox
// pattern (Enter/Space/Arrows open; Arrows/Home/End move; Enter/Space commit;
// Escape closes and refocuses the trigger; Tab closes).

export interface MenuOption {
  id: string
  label: string
  hint?: string
  disabled?: boolean
}

export function MenuSelect({
  ariaLabel,
  options,
  value = null,
  onSelect,
  disabled = false,
  small = false,
  placeholder = '—',
  emptyText,
  style,
}: {
  ariaLabel: string
  options: MenuOption[]
  value?: string | null
  onSelect: (id: string) => void
  disabled?: boolean
  small?: boolean
  placeholder?: string
  emptyText?: string
  style?: CSSProperties
}) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLDivElement>(null)
  const rawId = useId()
  const listId = `s-ms-${rawId.replace(/:/g, '')}-list`
  const optionId = (i: number) => `${listId}-opt-${i}`

  const enabled = options.map((o, i) => (o.disabled ? -1 : i)).filter((i) => i >= 0)
  const indexOf = (id: string | null) => options.findIndex((o) => o.id === id && !o.disabled)
  const selected = options.find((o) => o.id === value)

  const openMenu = () => {
    if (disabled || options.length === 0) return
    const found = indexOf(value)
    setActive(found >= 0 ? found : enabled[0] ?? -1)
    setOpen(true)
  }
  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) triggerRef.current?.focus()
  }
  const move = (step: number) => {
    if (enabled.length === 0) return
    const pos = enabled.indexOf(active)
    const next = pos === -1 ? (step > 0 ? 0 : enabled.length - 1) : (pos + step + enabled.length) % enabled.length
    setActive(enabled[next])
  }
  const choose = (i: number) => {
    const opt = options[i]
    if (!opt || opt.disabled) return
    onSelect(opt.id)
    close(true)
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: globalThis.MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const onKeyDown = (e: KeyboardEvent) => {
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        openMenu()
      }
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      close(true)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      move(1)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      move(-1)
    } else if (e.key === 'Home') {
      e.preventDefault()
      if (enabled.length) setActive(enabled[0])
    } else if (e.key === 'End') {
      e.preventDefault()
      if (enabled.length) setActive(enabled[enabled.length - 1])
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      choose(active)
    } else if (e.key === 'Tab') {
      setOpen(false)
    }
  }

  return (
    <div className="s-ms" ref={rootRef} onKeyDown={onKeyDown} style={style}>
      <div
        ref={triggerRef}
        role="combobox"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && active >= 0 ? optionId(active) : undefined}
        aria-disabled={disabled || undefined}
        tabIndex={disabled ? -1 : 0}
        className={`s-ms-trigger${small ? ' is-sm' : ''}${open ? ' is-open' : ''}${disabled ? ' is-disabled' : ''}`}
        onClick={() => (open ? close(true) : openMenu())}
      >
        <span className={`s-ms-value${selected ? '' : ' is-placeholder'}`}>{selected ? selected.label : placeholder}</span>
        <span className="s-ms-chev" aria-hidden="true">▾</span>
      </div>
      {open && (
        <div className="s-ms-pop" role="presentation">
          {options.length === 0 ? (
            <div className="s-ms-empty">{emptyText ?? 'Нет вариантов'}</div>
          ) : (
            <ul className="s-ms-list" role="listbox" aria-label={ariaLabel}>
              {options.map((o, i) => (
                <li
                  key={o.id}
                  id={optionId(i)}
                  role="option"
                  aria-selected={o.id === value}
                  aria-disabled={o.disabled || undefined}
                  className={`s-ms-opt${i === active ? ' is-active' : ''}${o.id === value ? ' is-selected' : ''}${o.disabled ? ' is-disabled' : ''}`}
                  onMouseEnter={() => { if (!o.disabled) setActive(i) }}
                  onClick={() => choose(i)}
                >
                  <span className="s-ms-opt-label">{o.label}</span>
                  {o.hint && <span className="s-ms-opt-hint">{o.hint}</span>}
                  {o.id === value && <span className="s-ms-opt-check" aria-hidden="true">✓</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

// ── Keyring reference field: never shows or persists a raw key ────────────

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
        <HelpText>
          {hasSecret === true ? 'Ключ сохранён в системном хранилище' : hasSecret === false ? 'Ключ не сохранён' : 'Состояние ключа неизвестно'}
        </HelpText>
        <Button size="sm" onClick={() => { setValue(''); setEditing(true) }}>
          {hasSecret ? 'Заменить ключ' : 'Добавить ключ'}
        </Button>
        {hasSecret && (
          <Button size="sm" variant="danger" onClick={onDelete}>
            Удалить ключ
          </Button>
        )}
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      <Input
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Вставьте ключ (не сохраняется в конфиг)"
        aria-label="Новый API ключ"
        style={{ marginTop: 0, flex: 1, minWidth: 220 }}
      />
      <Button
        variant="primary"
        onClick={() => { if (value.trim()) { onReplace(value.trim()); setValue(''); setEditing(false) } }}
        disabled={!value.trim()}
      >
        Сохранить в keyring
      </Button>
      <Button onClick={() => { setValue(''); setEditing(false) }}>Отмена</Button>
    </div>
  )
}
