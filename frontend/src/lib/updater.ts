/**
 * Простой updater через GitHub Releases API — без Tauri signing.
 * Проверяет https://api.github.com/repos/freakkxd/Fragile-Notes/releases/latest
 * и сравнивает tag_name с текущей версией из package.json / tauri.conf.json
 */

const REPO = 'freakkxd/Fragile-Notes'
const CURRENT = '0.4.11' // sync with package.json / Cargo.toml

function compareVersions(a: string, b: string): number {
  const pa = a.replace(/^v/, '').split('.').map(Number)
  const pb = b.replace(/^v/, '').split('.').map(Number)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const da = pa[i] || 0
    const db = pb[i] || 0
    if (da !== db) return da - db
  }
  return 0
}

export async function checkForUpdates(silent = true): Promise<{ hasUpdate: boolean; latest: string; url: string; notes: string } | null> {
  try {
    const res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, {
      headers: { Accept: 'application/vnd.github.v3+json' },
    })
    if (!res.ok) {
      if (!silent) console.warn('[updater] GitHub API failed', res.status)
      return null
    }
    const data = await res.json()
    const latest: string = data.tag_name || data.name || ''
    const url: string = data.html_url || `https://github.com/${REPO}/releases/latest`
    const notes: string = data.body || ''
    if (!latest) return null
    const hasUpdate = compareVersions(latest, CURRENT) > 0
    return { hasUpdate, latest, url, notes }
  } catch (e) {
    if (!silent) console.error('[updater] check failed', e)
    return null
  }
}

export function openReleasePage(url: string) {
  // Tauri shell.open если доступен, иначе window.open
  const w = window as unknown as {
    __TAURI__?: { shell?: { open: (u: string) => void }; invoke?: (cmd: string, args?: unknown) => Promise<unknown> }
  }
  if (w.__TAURI__?.shell?.open) {
    w.__TAURI__.shell.open(url)
  } else if (w.__TAURI__?.invoke) {
    w.__TAURI__.invoke('plugin:shell|open', { path: url }).catch(() => window.open(url, '_blank'))
  } else {
    window.open(url, '_blank')
  }
}
