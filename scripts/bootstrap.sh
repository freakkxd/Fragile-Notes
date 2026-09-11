#!/usr/bin/env bash
set -e
# bootstrap.sh — проверка зависимостей для из-коробки установки Fragile Notes
# Использование: ./scripts/bootstrap.sh [--fix]

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok() { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; }

echo "== Fragile Notes bootstrap (out-of-box check) =="

# 1. Python
if python3 -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
  ok "Python $(python3 --version 2>&1)"
else
  fail "Python >=3.11 требуется (найден $(python3 --version 2>&1 || echo 'не найден'))"
fi

# 2. GTK / Adwaita (PyGObject)
if python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; print('ok')" 2>/dev/null | grep -q ok; then
  ok "GTK4 + libadwaita (PyGObject)"
else
  fail "GTK4/libadwaita не найден"
  echo "  Arch: sudo pacman -S gtk4 libadwaita gobject-introspection python-gobject"
  echo "  Ubuntu: sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-adwaita-1"
  echo "  Fedora: sudo dnf install gtk4 libadwaita python3-gobject"
fi

# 3. Pip deps
if python3 -c "import yaml, cryptography" 2>/dev/null; then
  ok "Python deps (PyYAML, cryptography)"
else
  warn "PyYAML/cryptography не установлены → pip install -e ."
fi

# 4. Node (опционально, для AO Engine)
if command -v node >/dev/null 2>&1; then
  ok "Node $(node --version) (AO Engine опционально)"
else
  warn "Node не найден — AO Engine будет offline (редактор/задачи всё равно работают)"
  echo "  Установи: sudo pacman -S nodejs  /  sudo apt install nodejs"
fi

# 5. Vault — теперь в Documents/FragileNotesVault, не на рабочем столе
if [ -d "$HOME/Documents/FragileNotesVault" ]; then
  VAULT="$HOME/Documents/FragileNotesVault"
elif [ -d "$HOME/FragileNotesVault" ]; then
  VAULT="$HOME/FragileNotesVault"
else
  # fallback: Documents если есть, иначе ~/
  if [ -d "$HOME/Documents" ]; then VAULT="$HOME/Documents/FragileNotesVault"; else VAULT="$HOME/FragileNotesVault"; fi
fi
# Миграция со старого ~/desktop
if [ -d "$HOME/desktop" ] && [ ! -d "$VAULT" ]; then
  warn "Найден старый волт $HOME/desktop → мигрирую в $VAULT"
  if [[ "$1" == "--fix" ]]; then
    mkdir -p "$(dirname "$VAULT")"
    cp -r "$HOME/desktop" "$VAULT" 2>/dev/null || true
    ok "Мигрирован $VAULT"
  fi
fi
if [ -d "$VAULT" ]; then
  ok "Vault $VAULT существует"
else
  warn "Vault $VAULT не существует — создам скелет"
  if [[ "$1" == "--fix" ]]; then
    mkdir -p "$VAULT/01 Home" "$VAULT/02 Daily" "$VAULT/03 Projects" "$VAULT/04 FreakyWiki" "$VAULT/05 Sort" "$VAULT/06 Media" "$VAULT/_System"
    echo "# Home" > "$VAULT/01 Home/Home.md"
    ok "Создан $VAULT"
  else
    echo "  Запусти: ./scripts/bootstrap.sh --fix  или просто запусти fragile-notes — создаст автоматически"
  fi
fi

# 6. AO Engine (опционально)
if [ -f "$VAULT/_System/ArchiveOrganism/ao-engine/dist/cli/cli.js" ]; then
  ok "AO Engine найден"
else
  warn "AO Engine не найден ($VAULT/_System/ArchiveOrganism/ao-engine) — будет offline, остальное работает"
fi

# 7. LLM (опционально)
if [ -f "$HOME/LLM/models/Qwen3-14B-Q4_K_M.gguf" ] || [ -f "$HOME/LLM/models/gemma4-26b-qat-Q4_K_M.gguf" ]; then
  ok "LLM модели найдены"
else
  warn "LLM модели не найдены (~/LLM/models/) — чат покажет offline"
fi

# 8. Pip install check
if [ -f "pyproject.toml" ]; then
  if pip show fragile-notes >/dev/null 2>&1; then
    ok "fragile-notes установлен (pip show)"
  else
    warn "не установлен: pip install -e ."
  fi
fi

echo ""
echo "Итог: если все ✓ или ⚠ — fragile-notes запустится. ✗ требует установки."
echo "Запуск: pip install -e . && fragile-notes  (или ./run.sh)"
