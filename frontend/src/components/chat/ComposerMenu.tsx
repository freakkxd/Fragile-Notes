import { useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

/**
 * Accessible single-select popover used by the AI chat composer.
 *
 * The trigger is a `role="combobox"` element (a real focus owner), the popup
 * is a `role="listbox"` with `role="option"` children. Follows the
 * "collapsible listbox" pattern:
 *  - Enter / Space / ArrowUp / ArrowDown open the popup
 *  - ArrowUp / ArrowDown / Home / End move the active option
 *  - Enter / Space commit the active option
 *  - Escape and outside pointer-down close it
 *  - focus always returns to the trigger on close
 *
 * No dependencies: built from the existing inline styles + a small set of
 * `.cm-*` classes in styles.css.
 */
export interface ComposerMenuOption {
  id: string
  label: string
  hint?: string
  disabled?: boolean
}

interface ComposerMenuProps {
  ariaLabel: string
  options: ComposerMenuOption[]
  value?: string | null
  onSelect: (id: string) => void
  disabled?: boolean
  className?: string
  /** Rendered inside the trigger, before the chevron. */
  children: ReactNode
  /** Shown when the option list is empty. */
  emptyText?: string
}

export default function ComposerMenu({
  ariaLabel,
  options,
  value = null,
  onSelect,
  disabled = false,
  className = '',
  children,
  emptyText,
}: ComposerMenuProps) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLDivElement>(null)
  const rawId = useId()
  const listId = `cm-${rawId.replace(/:/g, '')}-list`
  const optionId = (i: number) => `${listId}-opt-${i}`

  const enabled = options
    .map((o, i) => (o.disabled ? -1 : i))
    .filter((i) => i >= 0)

  const indexOf = (id: string | null) => options.findIndex((o) => o.id === id && !o.disabled)

  const openMenu = (id: string | null) => {
    if (disabled || options.length === 0) return
    const found = indexOf(id)
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
    const next = pos === -1
      ? (step > 0 ? 0 : enabled.length - 1)
      : (pos + step + enabled.length) % enabled.length
    setActive(enabled[next])
  }

  const choose = (i: number) => {
    const opt = options[i]
    if (!opt || opt.disabled) return
    onSelect(opt.id)
    close(true)
  }

  // Close on outside pointer-down. Escape is handled by the keydown handler so
  // focus returns to the trigger; outside close intentionally leaves focus.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
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
        openMenu(value)
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
    <div className={`cm ${className}`.trim()} ref={rootRef} onKeyDown={onKeyDown}>
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
        className={`cm-trigger${open ? ' is-open' : ''}${disabled ? ' is-disabled' : ''}`}
        onClick={() => (open ? close(true) : openMenu(value))}
      >
        {children}
        <span className="cm-chev" aria-hidden="true">▾</span>
      </div>
      {open && (
        <div className="cm-pop" role="presentation">
          {options.length === 0 && emptyText ? (
            <div className="cm-empty">{emptyText}</div>
          ) : (
            <ul className="cm-list" role="listbox">
              {options.map((o, i) => (
                <li
                  key={o.id}
                  id={optionId(i)}
                  role="option"
                  aria-selected={o.id === value}
                  aria-disabled={o.disabled || undefined}
                  className={`cm-opt${i === active ? ' is-active' : ''}${o.id === value ? ' is-selected' : ''}${o.disabled ? ' is-disabled' : ''}`}
                  onMouseEnter={() => { if (!o.disabled) setActive(i) }}
                  onClick={() => choose(i)}
                >
                  <span className="cm-opt-label">{o.label}</span>
                  {o.hint && <span className="cm-opt-hint">{o.hint}</span>}
                  {o.id === value && <span className="cm-opt-check" aria-hidden="true">✓</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
