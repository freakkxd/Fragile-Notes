# bootstrap.ps1 — Windows аналог bootstrap.sh
Write-Host "== Fragile Notes bootstrap (Windows) ==" -ForegroundColor Cyan

function ok($m){ Write-Host "✓ $m" -ForegroundColor Green }
function warn($m){ Write-Host "⚠ $m" -ForegroundColor Yellow }
function fail($m){ Write-Host "✗ $m" -ForegroundColor Red }

# Python
try { $v = python --version 2>&1; $ok = python -c "import sys; exit(0 if sys.version_info>=(3,11) else 1)" 2>$null; if ($LASTEXITCODE -eq 0){ ok $v } else { fail "Python >=3.11 требуется ($v)" } } catch { fail "Python не найден" }

# GTK
try { python -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; print('ok')" 2>$null | Out-Null; if ($LASTEXITCODE -eq 0){ ok "GTK4 + libadwaita" } else { throw } } catch {
  fail "GTK4/libadwaita не найден — установи MSYS2: pacman -S mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-gobject"
}

# Pip deps
try { python -c "import yaml, cryptography" 2>$null; ok "PyYAML, cryptography" } catch { warn "pip install -e . требуется" }

# Node
if (Get-Command node -ErrorAction SilentlyContinue) { ok "Node $(node --version) (опционально)" } else { warn "Node не найден — AO Engine offline" }

# Vault
$Vault = "$env:USERPROFILE\desktop"
if (Test-Path $Vault) { ok "Vault $Vault" } else { warn "Vault $Vault нет — создастся при первом запуске (или .\scripts\bootstrap.ps1 -Fix)"; if ($args -contains "-Fix"){ New-Item -ItemType Directory -Force -Path "$Vault\01 Home","$Vault\02 Daily","$Vault\_System" | Out-Null; "# Home" | Out-File "$Vault\01 Home\Home.md" -Encoding utf8; ok "Создан $Vault" } }

# AO
if (Test-Path "$Vault\_System\ArchiveOrganism\ao-engine\dist\cli\cli.js") { ok "AO Engine" } else { warn "AO Engine не найден — offline" }
if (Test-Path "$env:USERPROFILE\LLM\models\Qwen3-14B-Q4_K_M.gguf") { ok "LLM" } else { warn "LLM не найден — чат offline" }

Write-Host "`nЗапуск: pip install -e .; fragile-notes" -ForegroundColor Cyan
