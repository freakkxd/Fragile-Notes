"""Главное окно Fragile Notes: header bar, сайдбар, статус-бар, стек вьюх."""

from __future__ import annotations

import datetime
import threading
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ..config import load_settings, save_settings
from ..core import runner as model
from ..core import workspaces as workspaces_core
from ..core.plugins import PluginManager
from ..core.runner_controller import RunnerController
from ..services.engine import EngineBridge
from ..services.enrich import EnrichStatus, fetch_enrich_status
from ..services.git_sync import GitSyncService
from ..services.llm import LlmService
from ..services.vault_service import VaultService
from ..services.web_clipper import WebClipperService
from . import scale
from . import workspace as ws
from .quick_capture import QuickCapture
from .quick_switcher import QuickSwitcher

# a11y: Gtk.Widget.update_property требует списки, но ТЗ использует одиночные аргументы
# Патч делает оба варианта рабочими, не ломая py_compile/Runtime.
try:
    if not getattr(Gtk.Widget.update_property, "_fragile_patched", False):  # type: ignore[attr-defined]
        _orig_upd = Gtk.Widget.update_property  # type: ignore[attr-defined]

        def _compat_update(self, prop, val, *a, **kw):  # type: ignore[no-untyped-def]
            try:
                if isinstance(prop, Gtk.AccessibleProperty):
                    return _orig_upd(self, [prop], [val])  # type: ignore[arg-type]
                return _orig_upd(self, prop, val, *a, **kw)
            except Exception:
                try:
                    return _orig_upd(self, [prop], [val])  # type: ignore
                except Exception:
                    return None

        _compat_update._fragile_patched = True  # type: ignore[attr-defined]
        Gtk.Widget.update_property = _compat_update  # type: ignore[attr-defined,method-assign]
except Exception:
    pass
from . import theme_manager
from .sidebar import Sidebar
from .status_bar import StatusBar
from .style import apply_css
from .workspace_mixin import WorkspaceMixin

POLL_MS = 2000
ENRICH_POLL_MS = 10000
BG_SCAN_MS = 15000

VIEW_TITLES = ws.VIEW_TITLES

VIEWS = (
    "files",
    "daily",
    "tasks",
    "graph",
    "canvas",
    "kanban",
    "database",
    "whiteboard",
    "mindmap",
    "ai_chat",
    "runner",
    "calendar",
    "srs",
    "habits",
    "pomodoro",
    "review",
    "media",
    "voice",
    "video",
    "templates",
    "slides",
    "mermaid_live",
    "latex_live",
    "analytics",
    "plugin_store",
    "theme_editor",
    "home",
    "tags",
    "settings",
)

# Иконки на левом ribbon (аналогия .workspace-ribbon → clickable-icon).
RIB_ICONS = ws.VIEW_ICONS

# Paned сайдбар: лимиты как в Obsidian
_SIDEBAR_MIN = ws.SIDEBAR_WIDTH_MIN
_SIDEBAR_MAX = ws.SIDEBAR_WIDTH_MAX
_SIDEBAR_DEFAULT = ws.SIDEBAR_WIDTH_DEFAULT
_SIDEBAR_COLLAPSED = ws.SIDEBAR_COLLAPSED_WIDTH

# Mobile breakpoint: узкие окна <700px — скрывать сайдбар, показывать bottom bar
MOBILE_BREAKPOINT_PX = 700
MOBILE_VIEWS = ("files", "tasks", "daily", "ai_chat", "calendar", "settings")


class FragileWindow(WorkspaceMixin, Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(
            application=app,
            title="Fragile Notes",
            default_width=1280,
            default_height=820,
        )
        # HeaderBar: на Linux Adw.ApplicationWindow запрещает gtk_window_set_titlebar (фатальный g_error)
        # Поэтому ставим заголовок только на Windows через Gtk.HeaderBar. На Linux — нативный декор.
        import sys as _sys

        if _sys.platform == "win32":
            try:
                header = Gtk.HeaderBar()
                header.set_show_title_buttons(True)
                header.set_title_widget(Gtk.Label(label="Fragile Notes", css_classes=["title"]))
                self.set_titlebar(header)
                self.set_decorated(True)
            except Exception:
                pass
        self.settings = load_settings()
        # Workspaces: гарантируем что vault_root в списке vaults (миграция для старых settings.json)
        try:
            before = list(self.settings.get("vaults") or [])
            workspaces_core.ensure_vaults(self.settings)
            if (self.settings.get("vaults") or []) != before:
                save_settings(self.settings)
        except Exception:
            pass
        # Тема: применяем сразу (Adw.StyleManager + custom.css)
        try:
            theme_manager.apply_theme(theme_manager.get_theme(self.settings))
        except Exception:
            pass
        try:
            from .style import apply_custom_css as _apply_ccss

            _apply_ccss(self.settings)
        except Exception:
            try:
                theme_manager.apply_custom_css(self.settings)
            except Exception:
                pass
        self.vault_service = VaultService(self.settings)
        self.git_sync = GitSyncService(self.settings)
        # Плагин-система: хуки из vault/_System/Plugins/*.py
        self.plugin_manager = PluginManager(self.settings)
        try:
            self.plugin_manager.load_plugins()
        except Exception:
            pass
        # Никаких жёстких минимумов: окно свободно изменяется от 100% экрана
        # (максимизация) до свободного минимума, который диктует содержимое.
        # Вьюхи адаптивны (Adw.Clamp + скроллы) и не навязывают окну свои размеры.
        # Привязка интерьера к окну: при изменении размеров окна внутренний
        # интерфейс масштабируется пропорционально (fit от первого размера).
        self._fit_ref: tuple[int, int] | None = None
        self._fit_scale = 1.0
        self._fit_target = 1.0
        self._fit_timer: int | None = None
        self._preload_tree()
        self._init_scale()
        self.engine = EngineBridge(self.settings)
        self.llm = LlmService(self.settings)
        self.web_clipper = WebClipperService(self.settings)
        self.controller = RunnerController(
            self.settings,
            self.engine,
            self.llm,
            history=self.settings.get("task_history"),
        )
        self.enrich = EnrichStatus()
        self.log = ""
        self._inbox_undecided = 0
        self._bg_scan_at = 0
        self._views: dict[str, Gtk.Widget] = {}
        self._task_counts = {"today": 0, "overdue": 0}
        self._tasks_dirty = True
        self._llm_sig = None
        self._build_ui()
        self._restore_state()
        self.connect("close-request", self._on_close_request)
        # FileMonitor для vault_root — инвалидация кэша + релоад дерева
        self._vault_monitor: Gio.FileMonitor | None = None
        self._vault_monitor_timer: int | None = None
        self._setup_vault_monitor()
        # Монитор custom.css — авто-перезагрузка кастом стилей
        self._custom_css_monitor: Gio.FileMonitor | None = None
        self._custom_css_timer: int | None = None
        self._setup_custom_css_monitor()
        # Git auto-sync (debounce 30с) — стартуем после монитора
        try:
            self.git_sync.start()
        except Exception:
            pass
        GLib.timeout_add(POLL_MS, self._poll)
        GLib.timeout_add(ENRICH_POLL_MS, self._poll_enrich)
        GLib.timeout_add(30000, self._persist_history)
        self.refresh_llm_async()
        self.refresh_enrich_async()

    # ── Масштабирование ───────────────────────────────────────
    def _preload_tree(self) -> None:
        """Прогрев кэша структуры vault при старте: первое открытие «Заметок» — мгновенно."""
        settings = dict(self.settings)
        svc = getattr(self, "vault_service", None)

        def work() -> None:
            try:
                if svc is not None:
                    svc.ensure_file_tree(force=True, settings=settings)
                else:
                    from ..services import vault

                    vault.ensure_file_tree(settings, force=True)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=work, daemon=True).start()

    def _init_scale(self) -> None:
        scale.init(
            _fs(self.settings.get("ui_scale"), 1.0),
            _fs(self.settings.get("editor_zoom"), 1.0),
            bool(self.settings.get("follow_system_scale", True)),
        )
        scale.subscribe(self._on_scale_changed)
        self._apply_scale()

    def _apply_scale(self) -> None:
        apply_css(
            scale.effective_ui() * getattr(self, "_fit_scale", 1.0),
            scale.editor_zoom(),
        )
        # кастом CSS должен оставаться поверх базового — переприменяем
        try:
            from .style import apply_custom_css as _apply_ccss2

            _apply_ccss2(self.settings)
        except Exception:
            try:
                theme_manager.apply_custom_css(self.settings)
            except Exception:
                pass

    def _on_scale_changed(self) -> None:
        self.settings["ui_scale"] = scale.ui_scale()
        self.settings["editor_zoom"] = scale.editor_zoom()
        self.settings["follow_system_scale"] = scale.follow_system()
        save_settings(self.settings)
        self._apply_scale()
        sv = self._views.get("settings")
        if sv is not None:
            sv.refresh_scale()

    # ── Привязка интерфейса к размерам окна ────────────────
    _FIT_MIN = 0.5
    _FIT_MAX = 2.0

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        if width <= 0 or height <= 0 or getattr(self, "_stack", None) is None:
            return
        ref = getattr(self, "_fit_ref", None)
        if ref is None:
            self._fit_ref = (width, height)
            return
        fit = min(width / ref[0], height / ref[1])
        fit = max(self._FIT_MIN, min(self._FIT_MAX, fit))
        # throttle _apply_scale: fit должен измениться >0.03
        if abs(fit - self._fit_scale) < 0.03:
            return
        # обновляем цель всегда — таймер проверит последнее значение
        self._fit_target = fit
        if getattr(self, "_fit_timer", None) is None:
            self._fit_timer = GLib.timeout_add(120, self._apply_window_fit)

    def _apply_window_fit(self) -> bool:
        self._fit_timer = None
        target = getattr(self, "_fit_target", self._fit_scale)
        # повторная проверка последнего значения (debounce 120 мс)
        if abs(target - self._fit_scale) < 0.03:
            return False
        self._fit_scale = target
        self._apply_scale()
        return False

    # ── UI ───────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["fragile-shell"])

        # Верх: Paned как в Obsidian — левая колонка тянется мышью за handle
        self._sidebar_width = self._get_sidebar_width()
        self.top = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, css_classes=["workspace-paned"]
        )
        try:
            self.top.set_wide_handle(True)
        except Exception:
            pass
        self.side_column = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, css_classes=["side-column"]
        )
        # Ribbon в скролле — при 30 иконок не уходит за экран (как в Obsidian)
        self.rail_scroller = Gtk.ScrolledWindow(vexpand=True, css_classes=["rail-scroller"])
        self.rail_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.rail_scroller.set_propagate_natural_height(False)
        self.rail = self._build_ribbon()
        self.rail_scroller.set_child(self.rail)
        self.side_column.append(self.rail_scroller)
        self._sidebar_pinned = False
        try:
            hover = Gtk.EventControllerMotion()
            def _hover_enter(*_):
                if getattr(self, "_sidebar_pinned", False):
                    return None
                if not self.sidebar_revealer.get_reveal_child():
                    self.sidebar_revealer.set_reveal_child(True)
                    try:
                        pos = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, int(self._sidebar_width)))
                        self.top.set_position(pos)
                    except Exception:
                        pass
                return None
            def _hover_leave(*_):
                if getattr(self, "_sidebar_pinned", False):
                    return None
                if self.sidebar_revealer.get_reveal_child():
                    self.sidebar_revealer.set_reveal_child(False)
                    try:
                        self.top.set_position(_SIDEBAR_COLLAPSED)
                    except Exception:
                        pass
                return None
            hover.connect("enter", _hover_enter)
            hover.connect("leave", _hover_leave)
            self.rail_scroller.add_controller(hover)
        except Exception:
            pass

        # Sidebar с workspaces (несколько vault в одном окне)
        try:
            vaults = workspaces_core.get_vaults(self.settings)
        except Exception:
            vaults = []
        self.sidebar = Sidebar(
            self._on_nav,
            self.settings["vault_root"],
            vaults=vaults,
            on_workspace_switch=self._on_workspace_switch,
            on_workspace_add=self._show_workspace_switcher,
        )
        # если Sidebar не принял новые аргументы (старая сигнатура) — fallback
        if not hasattr(self.sidebar, "_ws_list"):
            try:
                # пробуем донастроить через публичные методы
                self.sidebar.set_workspace_callbacks(
                    on_switch=self._on_workspace_switch, on_add=self._show_workspace_switcher
                )
                self.sidebar.set_vaults(vaults, self.settings.get("vault_root"))
            except Exception:
                pass
        self.sidebar_revealer = Gtk.Revealer()
        self.sidebar_revealer.set_child(self.sidebar)
        # SLIDE_RIGHT даёт сжатие ниже min-width (панель реально схлопывается),
        # 180ms — плавность Obsidian без «прыжка» и без роста окна (см. _on_toggle_sidebar)
        self.sidebar_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.sidebar_revealer.set_transition_duration(180)
        self.sidebar_revealer.set_reveal_child(False)
        self.side_column.append(self.sidebar_revealer)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, hexpand=True)
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        body.append(self.stack)
        self.right_revealer = Gtk.Revealer()
        self.right_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.right_revealer.set_transition_duration(180)
        self.right_revealer.set_reveal_child(False)
        self.right_panel = self._build_right_panel()
        self.right_revealer.set_child(self.right_panel)
        body.append(self.right_revealer)

        self.top.set_start_child(self.side_column)
        self.top.set_end_child(body)
        self.top.set_shrink_start_child(False)
        self.top.set_shrink_end_child(False)
        self.top.set_resize_start_child(False)
        self.top.set_resize_end_child(True)
        self.top.set_position(_SIDEBAR_COLLAPSED)
        self.top.connect("notify::position", self._on_paned_position)
        main.append(self.top)

        self.mobile_bottom_bar = self._build_mobile_bar()
        self.mobile_bottom_bar.set_visible(False)
        main.append(self.mobile_bottom_bar)

        self.status_bar = StatusBar()
        main.append(self.status_bar)

        self.toast_overlay.set_child(main)
        self.set_content(self.toast_overlay)

        self.quick = QuickSwitcher(self.settings, self._open_note, on_command=self._run_command)
        if getattr(self, "search_btn", None) is not None:
            self.quick.attach(self.search_btn)
        self.quick_capture = QuickCapture(self, self.settings)
        # Tray daemon + фоновый Quick Capture (AyatanaAppIndicator/StatusIcon, hold для --gapplication-service)
        self._tray = None
        try:
            from ..services.tray import setup_tray as _setup_tray  # noqa: E402

            self._tray = _setup_tray(app, self, self.settings)  # noqa: F821
        except Exception:
            self._tray = None
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_window_key)
        self.add_controller(key_ctrl)
        self._update_sidebar_chrome()
        self._setup_breakpoint()
        self._setup_swipe_gestures()

    def _build_ribbon(self) -> Gtk.Widget:
        """Левый ribbon как у Obsidian: состав и порядок иконок из workspace-конфига."""
        items = ws.normalize(self.settings)["ribbon"]
        rail = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["workspace-ribbon"]
        )
        self._ribbon_btns: dict[str, Gtk.ToggleButton] = {}
        self._cmd_btns: dict[str, Gtk.Button] = {}
        for it in items:
            self._append_ribbon_item(rail, it)
        stretch = Gtk.Box(vexpand=True)
        rail.append(stretch)
        self._append_llm_btn(rail)
        return rail

    def _append_ribbon_item(self, rail: Gtk.Box, it: dict) -> None:
        t = it.get("t")
        if t == "sep":
            rail.append(Gtk.Box(css_classes=["ribbon-sep"]))
            return
        if t == "view":
            key = it["id"]
            b = Gtk.ToggleButton(
                label=RIB_ICONS[key],
                tooltip_text=VIEW_TITLES[key],
                css_classes=["ribbon-btn"],
                can_focus=True,
            )
            b.set_size_request(36, 36)
            b.set_focusable(True)
            b.update_property(Gtk.AccessibleProperty.LABEL, VIEW_TITLES[key])
            b.update_property(
                Gtk.AccessibleProperty.DESCRIPTION, f"Открыть раздел {VIEW_TITLES[key]}"
            )
            b.connect("clicked", self._on_ribbon_click, key)
            self._ribbon_btns[key] = b
            rail.append(b)
            return
        if t == "cmd":
            cid = it["id"]
            b = self._cmd_button(cid)
            if cid == "sidebar":
                self.sidebar_toggle = b
                b.connect("clicked", self._on_toggle_sidebar)
            elif cid == "refresh":
                self.refresh_btn = b
                b.connect("clicked", lambda *_: self.refresh_all())
            elif cid == "search":
                self.search_btn = b
                b.connect("clicked", lambda *_: self.quick.open())
                if getattr(self, "quick", None) is not None:
                    self.quick.attach(self.search_btn)
            elif cid == "new":
                b.connect("clicked", lambda *_: self._new_note_dialog())
            elif cid == "save":
                b.connect("clicked", lambda *_: self._save_current())
            else:
                return
            rail.append(b)
            return
        if t == "open":
            path = self._ribbon_open_path(it)
            if path is None:
                return
            tip = it.get("tip") or str(path)
            b = Gtk.Button(
                label=it.get("icon") or "📄",
                tooltip_text=tip,
                css_classes=["ribbon-btn"],
                can_focus=True,
            )
            b.set_size_request(36, 36)
            b.set_focusable(True)
            b.update_property(Gtk.AccessibleProperty.LABEL, tip)
            b.update_property(Gtk.AccessibleProperty.DESCRIPTION, tip)
            b.connect("clicked", lambda *_: self._open_ribbon_path(path))
            rail.append(b)

    def _cmd_button(self, cid: str) -> Gtk.Button:
        b = Gtk.Button(
            icon_name=ws.CMD_ICONS[cid],
            tooltip_text=ws.CMD_TIPS[cid],
            css_classes=["ribbon-btn"],
            can_focus=True,
        )
        b.set_size_request(36, 36)
        b.set_focusable(True)
        b.update_property(Gtk.AccessibleProperty.LABEL, ws.CMD_TIPS[cid])
        b.update_property(Gtk.AccessibleProperty.DESCRIPTION, ws.CMD_TIPS[cid])
        self._cmd_btns[cid] = b
        return b

    def _append_llm_btn(self, rail: Gtk.Box) -> None:
        self.llm_btn = Gtk.MenuButton(
            icon_name="network-server-symbolic", tooltip_text="LLM", css_classes=["ribbon-btn"]
        )
        self.llm_btn.set_can_focus(True)
        self.llm_btn.set_focusable(True)
        self.llm_btn.update_property(Gtk.AccessibleProperty.LABEL, "LLM")
        self.llm_btn.update_property(Gtk.AccessibleProperty.DESCRIPTION, "Статус LLM")
        self.llm_popover = Gtk.Popover()
        self.llm_pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.llm_pop_box.set_margin_top(10)
        self.llm_pop_box.set_margin_bottom(10)
        self.llm_pop_box.set_margin_start(12)
        self.llm_pop_box.set_margin_end(12)
        self.llm_popover.set_child(self.llm_pop_box)
        self.llm_btn.set_popover(self.llm_popover)
        rail.append(self.llm_btn)

    def _ribbon_open_path(self, it: dict) -> Path | None:
        raw = str(it.get("path") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = Path(self.settings["vault_root"]) / p
        return p

    def _open_ribbon_path(self, path: Path) -> None:
        if not path.is_file():
            self._notify_toast(f"файл не найден: {path.name}")
            return
        self._open_note(str(path))

    def _on_ribbon_click(self, btn: Gtk.ToggleButton, key: str) -> None:
        if not btn.get_active():
            btn.set_active(True)
        self._on_nav(key)

    # ── Ленивые вкладки ──────────────────────────────────────
    def _make_view(self, name: str) -> Gtk.Widget:
        """Вкладка строится при первом открытии; модуль импортируется там же."""
        if name == "home":
            from .home_view import HomeView

            return HomeView(self.settings, self._on_nav)
        if name == "runner":
            from .runner_view import RunnerView

            return RunnerView(self.controller, self._on_runner_action)
        if name == "tasks":
            from .tasks_view import TasksView

            return TasksView(self.settings, self._on_task_action)
        if name == "habits":
            from .habit_view import HabitView

            return HabitView(self.settings, self._open_note)
        if name == "pomodoro":
            from .pomodoro_view import PomodoroView

            return PomodoroView(self.settings, self._on_pomodoro_action)
        if name == "daily":
            from .daily_view import DailyView

            return DailyView(self.settings, self._open_note)
        if name == "calendar":
            from .calendar_view import CalendarView

            return CalendarView(self.settings, self._open_note)
        if name == "review":
            from .review_view import ReviewView

            return ReviewView(self.settings, self._open_note)
        if name == "srs":
            from .srs_view import SrsView

            return SrsView(self.settings, self._open_note)
        if name == "media":
            from .media_view import MediaView

            return MediaView(self.settings)
        if name == "voice":
            from .voice_view import VoiceView

            return VoiceView(self.settings)
        if name == "video":
            from .video_view import VideoView

            return VideoView(self.settings)
        if name == "files":
            from .files_view import FilesView

            return FilesView(self.settings)
        if name == "templates":
            from .templates_view import TemplatesView

            return TemplatesView(self.settings)
        if name == "tags":
            from .files_view import FilesView

            return FilesView(self.settings)
        if name == "graph":
            from .graph_view import GraphView

            return GraphView(self.settings, self._open_note)
        if name == "canvas":
            from .canvas_view import CanvasView

            return CanvasView(self.settings)
        if name == "whiteboard":
            from .whiteboard_view import WhiteboardView

            return WhiteboardView(self.settings, self._open_note)
        if name == "kanban":
            from .kanban_view import KanbanView

            return KanbanView(self.settings, self._open_note)
        if name == "database":
            from .database_view import DatabaseView

            return DatabaseView(self.settings, self._open_note)
        if name == "slides":
            from .slides_view import SlidesView

            return SlidesView(self.settings)
        if name == "mindmap":
            from .mindmap_view import MindMapView

            return MindMapView(self.settings, self._on_mindmap_navigate)
        if name == "mermaid_live":
            from .mermaid_live import MermaidLiveView

            return MermaidLiveView(self.settings)
        if name == "latex_live":
            from .latex_live import LatexLiveView

            return LatexLiveView(self.settings)
        if name == "ai_chat":
            from .ai_chat_view import AiChatView

            return AiChatView(self.llm, self.settings)
        if name == "analytics":
            from .analytics_view import AnalyticsView

            return AnalyticsView(self.settings, self._open_note)
        if name == "plugin_store":
            from .plugin_store_view import PluginStoreView

            return PluginStoreView(self.settings, self.plugin_manager)
        if name == "theme_editor":
            from .theme_editor_view import ThemeEditorView

            return ThemeEditorView(self.settings)
        if name == "settings":
            from .settings_view import SettingsView

            return SettingsView(
                self.settings,
                self._on_settings_saved,
                on_ribbon_changed=self._rebuild_ribbon,
                on_window_resize=self._resize_window,
            )
        raise KeyError(name)

    def _ensure_view(self, name: str) -> Gtk.Widget:
        view = self._views.get(name)
        if view is None:
            view = self._make_view(name)
            self._views[name] = view
            self.stack.add_named(view, name)
        return view

    def _visible_name(self) -> str:
        return self.stack.get_visible_child_name() or ""

    def _sync_graph_current(self) -> None:
        gv = self._views.get("graph")
        fv = self._views.get("files")
        if gv is not None and fv is not None and hasattr(gv, "set_current_file"):
            cur = getattr(fv, "_current", None)
            try:
                gv.set_current_file(cur)
            except Exception:
                pass

    def _sync_mindmap_current(self) -> None:
        mv = self._views.get("mindmap")
        fv = self._views.get("files")
        if mv is not None and fv is not None and hasattr(mv, "set_current_file"):
            cur = getattr(fv, "_current", None)
            try:
                mv.set_current_file(cur)
            except Exception:
                pass

    def _on_mindmap_navigate(
        self, line: int, title: str | None = None, path: Path | None = None
    ) -> None:
        """Клик по узлу Mind Map → скролл к заголовку в редакторе."""
        # открываем нужную заметку если клик из другого файла (при ручном открытии)
        target = (
            path
            if path is not None
            else getattr(self._views.get("files"), "_current", None)
            if self._views.get("files") is not None
            else None
        )
        if target is not None and Path(target).is_file():
            # если текущая заметка != target, откроем target
            cur = (
                getattr(self._views.get("files"), "_current", None)
                if self._views.get("files") is not None
                else None
            )
            try:
                if cur is None or Path(cur).resolve() != Path(target).resolve():
                    self._open_note(str(target))
            except Exception:
                pass
        fv = self._views.get("files")
        if fv is not None and hasattr(fv, "buffer") and hasattr(fv, "editor"):
            try:
                buf = fv.buffer
                it = buf.get_iter_at_line(int(line))
                buf.place_cursor(it)
                fv.editor.scroll_to_iter(it, 0.0, False, 0, 0)
                fv.editor.grab_focus()
                self._show_view("files")
            except Exception:
                pass

    def _show_view(self, key: str) -> None:
        self._ensure_view(key)
        self.stack.set_visible_child_name(key)
        self._sync_chrome(key)
        try:
            if key == "files":
                if hasattr(self, "sidebar_revealer"):
                    self.sidebar_revealer.set_reveal_child(True)
                    try:
                        self._sidebar_pinned = True
                    except Exception:
                        pass
                    try:
                        pos = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, int(self._sidebar_width)))
                        self.top.set_position(pos)
                    except Exception:
                        pass
                if hasattr(self, "right_revealer"):
                    self.right_revealer.set_reveal_child(False)
            elif key in ("home", "settings"):
                if hasattr(self, "right_revealer"):
                    self.right_revealer.set_reveal_child(False)
            else:
                if hasattr(self, "right_revealer"):
                    self.right_revealer.set_reveal_child(True)
        except Exception:
            pass
        if key == "graph":
            self._sync_graph_current()
        if key == "mindmap":
            self._sync_mindmap_current()
        self.refresh_all()

    def _sync_chrome(self, key: str) -> None:
        """Подсветить раздел в ribbon и выбрать row в сайдбаре."""
        for k, b in self._ribbon_btns.items():
            b.set_active(k == key)
        self.sidebar.select(key)
        for k, b in getattr(self, "_mobile_btns", {}).items():
            try:
                b.set_active(k == key)
            except Exception:
                pass
        for k, b in getattr(self, "_right_btns", {}).items():
            try:
                if hasattr(b, "set_css_classes"):
                    base = ["right-panel-btn"]
                    if k == key:
                        base.append("right-panel-btn-active")
                    b.set_css_classes(base)
            except Exception:
                pass

    def _llm_row(self, icon: str, text: str) -> Gtk.Label:
        lbl = Gtk.Label(
            label=f"{icon}  {text}", halign=Gtk.Align.START, xalign=0, css_classes=["sb-llm-row"]
        )
        return lbl

    def _refresh_llm_popover(self) -> None:
        while (w := self.llm_pop_box.get_first_child()) is not None:
            self.llm_pop_box.remove(w)
        day = self.llm.get_status("day")
        arch = self.llm.get_status("archive")
        self.llm_pop_box.append(self._llm_row("💎", "Qwen3‑14B (day)"))
        self.llm_pop_box.append(
            self._llm_row(
                "   ",
                "статус: "
                + ("ВКЛ" if day.online else "ВЫКЛ" if day.online is False else "не проверен"),
            )
        )
        if day.ctx_size:
            self.llm_pop_box.append(
                self._llm_row("   ", f"ctx: {day.ctx_size} · порт {self.llm.port('day')}")
            )
        self.llm_pop_box.append(self._llm_row("🌙", "Gemma‑4‑26B (archive)"))
        self.llm_pop_box.append(
            self._llm_row(
                "   ",
                "статус: "
                + ("ВКЛ" if arch.online else "ВЫКЛ" if arch.online is False else "не проверен"),
            )
        )
        if arch.ctx_size:
            self.llm_pop_box.append(
                self._llm_row("   ", f"ctx: {arch.ctx_size} · порт {self.llm.port('archive')}")
            )
        self.llm_pop_box.append(self._llm_row("🛠", f"скрипт: {self.llm.script.name}"))

    # ── Навигация ────────────────────────────────────────────
    def _on_nav(self, key: str) -> None:
        # Теги — это виртуальная вьюха поверх Заметок: открываем files и фокус на # фильтр
        if key == "tags":
            if "files" not in self._views:
                self._ensure_view("files")
            self._show_view("files")
            fv = self._views.get("files")
            if fv is not None and hasattr(fv, "filter_entry"):
                try:
                    fv.filter_entry.set_text("#")
                    if hasattr(fv, "focus_filter"):
                        GLib.idle_add(lambda: fv.focus_filter() or False)
                except Exception:
                    pass
            # сохраняем ui_state как tags
            ui_state = {**self.settings.get("ui_state", {}), "view": key}
            if ui_state != self.settings.get("ui_state"):
                self.settings["ui_state"] = ui_state
                save_settings(self.settings)
            self._sync_chrome("tags")
            return
        if key not in VIEWS:
            return
        changed = self._visible_name() != key or key not in self._views
        self._show_view(key)
        if changed:
            ui_state = {**self.settings.get("ui_state", {}), "view": key}
            if ui_state != self.settings.get("ui_state"):
                self.settings["ui_state"] = ui_state
                save_settings(self.settings)

    def _run_command(self, cmd_id: str) -> None:
        actions = {
            "new": self._new_note_dialog,
            "save": self._save_current,
            "search": self.quick.open,
            "quick_open": self.quick.open,
            "global_search": self._focus_global_search,
            "duplicate_line": self._duplicate_line,
            "toggle_preview": self._toggle_editor_preview,
            "sidebar": self._on_toggle_sidebar,
            "quick_capture": lambda: getattr(self, "quick_capture", None)
            and self.quick_capture.open(),
            "web_clip": self._web_clip_dialog,
            "snippets": self._open_snippets_palette,
            "files": lambda: self._on_nav("files"),
            "settings": lambda: self._on_nav("settings"),
        }
        actions.get(cmd_id, lambda: None)()

    def _rebuild_ribbon(self) -> None:
        """Пересборка ribbon после изменения workspace-конфига (без потери состояния, не ломает Paned)."""
        cur = self._visible_name()
        po = self.quick.popover
        if po.get_parent() is not None:
            po.unparent()
        # Paned хранит side_column как start_child — трогаем только rail внутри scroller
        pos = self.top.get_position() if hasattr(self, "top") else _SIDEBAR_COLLAPSED
        if self.rail is not None and hasattr(self, "rail_scroller"):
            self.rail_scroller.set_child(None)
        elif self.rail is not None:
            try:
                self.side_column.remove(self.rail)
            except Exception:
                pass
        self.rail = self._build_ribbon()
        if hasattr(self, "rail_scroller"):
            self.rail_scroller.set_child(self.rail)
        else:
            self.side_column.prepend(self.rail)
        # Восстановить позицию Paned после пересборки (GTK может сбросить)
        if hasattr(self, "top"):
            GLib.idle_add(
                lambda: self.top.set_position(pos) if self.top.get_position() != pos else False
            )
        self._update_sidebar_chrome()
        self._sync_chrome(cur)

    # ── Адаптивность (mobile) ───────────────────────────────────
    def _build_mobile_bar(self) -> Gtk.Widget:
        """Bottom bar для узких окон (<700px): горизонтальная навигация."""
        bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=2,
            css_classes=["mobile-bottom-bar"],
            hexpand=True,
            homogeneous=True,
        )
        bar.set_halign(Gtk.Align.FILL)
        self._mobile_btns: dict[str, Gtk.ToggleButton] = {}
        for key in MOBILE_VIEWS:
            icon = RIB_ICONS.get(key, "?")
            tip = VIEW_TITLES.get(key, key)
            btn = Gtk.ToggleButton(
                label=icon,
                tooltip_text=tip,
                css_classes=["mobile-nav-btn"],
                can_focus=True,
            )
            btn.set_focusable(True)
            try:
                btn.update_property(Gtk.AccessibleProperty.LABEL, tip)
                btn.update_property(Gtk.AccessibleProperty.DESCRIPTION, f"Открыть раздел {tip}")
            except Exception:
                pass
            btn.connect("clicked", self._on_mobile_nav, key)
            self._mobile_btns[key] = btn
            bar.append(btn)
        # extra: кнопка меню для сайдбара на мобиле
        menu_btn = Gtk.Button(
            icon_name="sidebar-show-symbolic",
            tooltip_text="Сайдбар",
            css_classes=["mobile-nav-btn", "mobile-menu-btn"],
        )
        menu_btn.connect("clicked", self._on_toggle_sidebar)
        bar.append(menu_btn)
        self._mobile_menu_btn = menu_btn
        return bar

    def _build_right_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["right-panel"])
        box.set_size_request(220, -1)
        header = Gtk.Label(label="Фичи", css_classes=["right-panel-header"], halign=Gtk.Align.START)
        header.set_margin_top(8)
        header.set_margin_start(10)
        box.append(header)
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box.append(sep)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        list_box.set_margin_start(6)
        list_box.set_margin_end(6)
        RIGHT_FEATURES = ["graph", "canvas", "kanban", "database", "whiteboard", "mindmap", "ai_chat", "runner", "calendar", "srs", "analytics", "plugin_store", "settings"]
        self._right_btns: dict[str, Gtk.Button] = {}
        for key in RIGHT_FEATURES:
            icon = RIB_ICONS.get(key, "?")
            tip = VIEW_TITLES.get(key, key)
            btn = Gtk.Button(label=f"{icon}  {tip}", halign=Gtk.Align.FILL, css_classes=["right-panel-btn"])
            btn.set_has_frame(False)
            btn.connect("clicked", lambda _b, k=key: self._on_nav(k))
            self._right_btns[key] = btn
            list_box.append(btn)
        scroller.set_child(list_box)
        box.append(scroller)
        close_btn = Gtk.Button(label="Скрыть ▶", css_classes=["right-panel-close"])
        close_btn.connect("clicked", lambda *_: self.right_revealer.set_reveal_child(False))
        box.append(close_btn)
        return box

    def _on_mobile_nav(self, btn: Gtk.ToggleButton, key: str) -> None:
        if not btn.get_active():
            btn.set_active(True)
        self._on_nav(key)

    def _setup_breakpoint(self) -> None:
        """Adw.Breakpoint для <700px: скрывать сайдбар, показывать bottom bar."""
        self._is_narrow = False
        try:
            try:
                cond = Adw.BreakpointCondition.parse(f"max-width: {MOBILE_BREAKPOINT_PX}px")
            except Exception:
                cond = Adw.BreakpointCondition.new_length(
                    Adw.BreakpointConditionLengthType.MAX_WIDTH,
                    float(MOBILE_BREAKPOINT_PX),
                    Adw.LengthUnit.PX,
                )
            bp = Adw.Breakpoint.new(cond)
            # Декларативные сеттеры: Adw сам включит/выключит на узком/широком
            try:
                bp.add_setter(self.mobile_bottom_bar, "visible", True)
            except Exception:
                pass
            try:
                bp.add_setter(self.sidebar_revealer, "reveal-child", False)
            except Exception:
                pass
            # Сигналы для дополнительной логики (если поддерживаются)
            try:
                bp.connect("apply", lambda *_: self._on_breakpoint_apply())
            except Exception:
                pass
            try:
                bp.connect("unapply", lambda *_: self._on_breakpoint_unapply())
            except Exception:
                pass
            try:
                self.add_breakpoint(bp)
            except Exception:
                pass
            self._mobile_breakpoint = bp
        except Exception:
            pass
        # Fallback: отслеживание ширины окна для окружений без Adw.Breakpoint
        try:
            self.connect("notify::default-width", lambda *_: self._check_narrow_fallback())
        except Exception:
            pass

    def _check_narrow_fallback(self) -> None:
        try:
            w = self.get_width()
            if w <= 0:
                return
            narrow = w < MOBILE_BREAKPOINT_PX
            if narrow == getattr(self, "_is_narrow", False):
                return
            if narrow:
                self._on_breakpoint_apply()
            else:
                self._on_breakpoint_unapply()
        except Exception:
            pass

    def _on_breakpoint_apply(self) -> None:
        """Узкое окно: показать bottom bar, скрыть сайдбар."""
        self._is_narrow = True
        try:
            if hasattr(self, "mobile_bottom_bar"):
                self.mobile_bottom_bar.set_visible(True)
        except Exception:
            pass
        try:
            if (
                getattr(self, "sidebar_revealer", None) is not None
                and self.sidebar_revealer.get_reveal_child()
            ):
                self.sidebar_revealer.set_reveal_child(False)
                self._update_sidebar_chrome()
        except Exception:
            pass

    def _on_breakpoint_unapply(self) -> None:
        """Широкое окно: скрыть bottom bar."""
        self._is_narrow = False
        try:
            if hasattr(self, "mobile_bottom_bar"):
                self.mobile_bottom_bar.set_visible(False)
        except Exception:
            pass

    def _setup_swipe_gestures(self) -> None:
        """3-пальцевые жесты: свайп влево/вправо — навигация, вверх — палитра.

        Использует :mod:`fragilenotes.ui.gestures` (``Gtk.GestureSwipe(n_points=3)``).
        Fallback — делегирование на старый inline-код если модуль недоступен.
        """
        try:
            from .gestures import setup_three_finger_gestures

            gestures = setup_three_finger_gestures(self)
            if gestures:
                return
        except Exception:
            pass
        # fallback: inline 1-пальцевый swipe/drag если gestures недоступен
        try:
            swipe = Gtk.GestureSwipe.new()
            swipe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            swipe.connect("swipe", self._on_swipe)
            self.add_controller(swipe)
            self._swipe_gesture = swipe
        except Exception:
            pass
        try:
            drag = Gtk.GestureDrag.new()
            drag.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
            drag.connect("drag-end", self._on_drag_swipe)
            self.add_controller(drag)
            self._drag_gesture = drag
        except Exception:
            pass

    def _on_swipe(self, gesture, vx: float, vy: float) -> None:
        """Обработка 3-пальцевого свайпа: влево/вправо — навигация, вверх — палитра.

        Горизонтальный свайп также тогглит сайдбар в narrow-режиме.
        """
        try:
            # пробуем делегировать в gestures модуль (единая логика направлений)
            try:
                from .gestures import SWIPE_VELOCITY_THRESHOLD, get_swipe_direction

                direction = get_swipe_direction(
                    float(vx), float(vy), threshold=SWIPE_VELOCITY_THRESHOLD
                )
                if direction == "up":
                    # палитра команд (3 пальца вверх)
                    try:
                        if hasattr(self, "quick") and hasattr(self.quick, "open_commands"):
                            self.quick.open_commands()
                            return
                    except Exception:
                        pass
                    try:
                        from .gestures import _open_palette

                        if _open_palette(self):
                            return
                    except Exception:
                        pass
                    return
                if direction == "down":
                    return
                # горизонтальные — ниже сохраняем логику сайдбара + делегируем
                if direction in ("left", "right"):
                    pass  # обработка ниже
                else:
                    return
            except Exception:
                # fallback на старую проверку
                if abs(vx) < abs(vy):
                    # вертикальный — проверяем up для палитры
                    THRESH = 400.0
                    if abs(vy) >= THRESH and float(vy) < 0:
                        try:
                            if hasattr(self, "quick") and hasattr(self.quick, "open_commands"):
                                self.quick.open_commands()
                                return
                        except Exception:
                            pass
                    return
                THRESH = 400.0
                if abs(vx) < THRESH:
                    return

            THRESH = 400.0
            # используем vx напрямую для совместимости с narrow-логникой
            if float(vx) > 0:
                # свайп вправо (3 пальца вправо) — назад
                if getattr(self, "_is_narrow", False):
                    if not self.sidebar_revealer.get_reveal_child():
                        self.sidebar_revealer.set_reveal_child(True)
                        self._update_sidebar_chrome()
                        if hasattr(self, "top"):
                            pos = getattr(self, "_sidebar_width", _SIDEBAR_DEFAULT)
                            pos = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, int(pos)))
                            try:
                                self.top.set_position(pos)
                            except Exception:
                                pass
                        return
                self._navigate_swipe(-1)
            else:
                # свайп влево (3 пальца влево) — вперёд
                if getattr(self, "_is_narrow", False) and self.sidebar_revealer.get_reveal_child():
                    self.sidebar_revealer.set_reveal_child(False)
                    self._update_sidebar_chrome()
                    if hasattr(self, "top"):
                        try:
                            self.top.set_position(_SIDEBAR_COLLAPSED)
                        except Exception:
                            pass
                    return
                self._navigate_swipe(1)
        except Exception:
            pass

    def _on_drag_swipe(self, gesture, dx: float, dy: float) -> None:
        """Fallback для устройств где GestureSwipe не срабатывает: drag >80px (3 пальца).

        Поддержка вертикального drag вверх → палитра.
        """
        try:
            # делегируем в gestures если возможно
            try:
                from .gestures import handle_three_finger_drag

                if handle_three_finger_drag(self, float(dx), float(dy)):
                    return
            except Exception:
                pass
            # локальный fallback: горизонтальный drag
            if abs(float(dx)) < 80 or abs(float(dx)) < abs(float(dy)):
                # вертикальный drag вверх → палитра
                if abs(float(dy)) >= 80 and float(dy) < 0:
                    try:
                        if hasattr(self, "quick") and hasattr(self.quick, "open_commands"):
                            self.quick.open_commands()
                            return
                    except Exception:
                        pass
                return
            if float(dx) > 0:
                self._on_swipe(gesture, 600.0, 0.0)
            else:
                self._on_swipe(gesture, -600.0, 0.0)
        except Exception:
            pass

    def _navigate_swipe(self, delta: int) -> None:
        try:
            cur = self._visible_name()
            if cur not in VIEWS:
                return
            idx = VIEWS.index(cur)
            nxt = (idx + delta) % len(VIEWS)
            self._on_nav(VIEWS[nxt])
        except Exception:
            pass

    def _on_window_key(self, _ctrl, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        # Ctrl+Enter — предпросмотр (обсидиан-стиль: открыть/переключить preview)
        if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            self._toggle_editor_preview()
            return True
        if ctrl:
            if keyval in (Gdk.KEY_b, Gdk.KEY_B):
                self._on_toggle_sidebar()
                return True
            # Ctrl+O — быстрый переход к файлам (аналог Obsidian Ctrl+O)
            if keyval in (Gdk.KEY_o, Gdk.KEY_O) and not shift:
                self.quick.open()
                return True
            if keyval in (Gdk.KEY_p, Gdk.KEY_k) and not shift:
                self.quick.open()
                return True
            if keyval in (Gdk.KEY_p, Gdk.KEY_P) and shift:
                self.quick.open_commands()
                return True
            # Ctrl+Shift+F — глобальный поиск по заметкам
            if keyval in (Gdk.KEY_f, Gdk.KEY_F) and shift:
                self._focus_global_search()
                return True
            # Ctrl+D — дублировать строку в редакторе (CodeMirror/Obsidian)
            if keyval in (Gdk.KEY_d, Gdk.KEY_D) and not shift:
                if self._duplicate_line():
                    return True
            # Ctrl+E — предпросмотр, Ctrl+Shift+E — алиас
            if keyval in (Gdk.KEY_e, Gdk.KEY_E):
                self._toggle_editor_preview()
                return True
            if keyval in (Gdk.KEY_s, Gdk.KEY_S) and not shift:
                self._save_current()
                return True
            if keyval in (Gdk.KEY_n, Gdk.KEY_N) and not shift:
                self._new_note_dialog()
                return True
            # Ctrl+Shift+Q — Quick Capture (vault/Inbox/)
            if shift and keyval in (Gdk.KEY_q, Gdk.KEY_Q):
                try:
                    self.quick_capture.open()
                except Exception:
                    pass
                return True
            # Ctrl+Shift+W — Web Clipper (vault/Clippings/)
            if shift and keyval in (Gdk.KEY_w, Gdk.KEY_W):
                try:
                    self._web_clip_dialog()
                except Exception:
                    pass
                return True
            # Ctrl+Shift+O — Workspaces: быстрый свитч vault
            if shift and keyval in (Gdk.KEY_o, Gdk.KEY_O):
                try:
                    self._show_workspace_switcher()
                except Exception:
                    pass
                return True
        return False

    # ── Workspaces — несколько vault в одном окне ─────────────
    def _on_workspace_switch(self, path_str: str) -> None:
        """Переключить активный vault по пути (вызывается из sidebar/list)."""
        try:
            p = str(Path(str(path_str)).expanduser().resolve(strict=False))
        except Exception:
            p = str(path_str).strip()
        if not p:
            return
        # если уже текущий — ничего
        try:
            cur = str(
                Path(str(self.settings.get("vault_root") or "")).expanduser().resolve(strict=False)
            )
        except Exception:
            cur = str(self.settings.get("vault_root") or "")
        if p == cur:
            return
        # проверяем существование — предупреждение но не блокируем
        exists = Path(p).is_dir()
        new_settings = dict(self.settings)
        try:
            workspaces_core.switch_vault(new_settings, p)
        except Exception:
            new_settings["vault_root"] = p
        # сохраняем и переинициализируем (как _on_settings_saved)
        self._on_settings_saved(new_settings)
        name = Path(p).name or p
        self._notify_toast(f"vault → {name}" + ("" if exists else " (папка не найдена)"))

    def _show_workspace_switcher(self) -> None:
        """Диалог быстрого свитча Ctrl+Shift+O и управления списком vault."""
        try:
            vaults = workspaces_core.get_vaults(self.settings)
        except Exception:
            vaults = []
        current = str(self.settings.get("vault_root") or "")
        try:
            cur_norm = str(Path(current).expanduser().resolve(strict=False))
        except Exception:
            cur_norm = current

        dialog = Adw.Dialog(title="Workspaces — vault")
        dialog.set_content_width(520)
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["dialog-body"]
        )
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(16)
        body.set_margin_end(16)

        header = Gtk.Label(
            label="Workspaces — несколько vault в одном окне\nCtrl+Shift+O — быстрый свитч",
            halign=Gtk.Align.START,
            xalign=0,
            css_classes=["dim-label"],
            wrap=True,
        )
        body.append(header)

        # поиск-фильтр
        filter_entry = Gtk.SearchEntry(placeholder_text="фильтр по имени или пути…", hexpand=True)
        body.append(filter_entry)

        scroller = Gtk.ScrolledWindow(vexpand=True, max_content_height=260)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        lb = Gtk.ListBox(css_classes=["ws-switch-list"])
        lb.set_selection_mode(Gtk.SelectionMode.SINGLE)
        lb.set_activate_on_single_click(False)
        scroller.set_child(lb)
        body.append(scroller)

        status = Gtk.Label(label="", css_classes=["dim-hint"], halign=Gtk.Align.START, wrap=True)
        body.append(status)

        btn_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["btn-row"]
        )
        btn_row.set_halign(Gtk.Align.END)
        close_btn = Gtk.Button(label="Закрыть", css_classes=["mod-neutral"])
        add_btn = Gtk.Button(label="＋ Добавить", css_classes=["suggested-action", "mod-cta"])
        remove_btn = Gtk.Button(label="− Удалить", css_classes=["mod-neutral"])
        switch_btn = Gtk.Button(label="Переключить", css_classes=["suggested-action"])
        btn_row.append(remove_btn)
        btn_row.append(add_btn)
        btn_row.append(switch_btn)
        btn_row.append(close_btn)
        body.append(btn_row)

        def _rebuild(filter_q: str = "") -> None:
            while (child := lb.get_first_child()) is not None:
                lb.remove(child)
            q = filter_q.strip().lower()
            shown = 0
            for v in vaults:
                path = str(v.get("path") or "")
                name = str(v.get("name") or path)
                if q and q not in name.lower() and q not in path.lower():
                    continue
                try:
                    is_cur = str(Path(path).expanduser().resolve(strict=False)) == cur_norm
                except Exception:
                    is_cur = path == current
                row = Gtk.ListBoxRow(
                    activatable=True,
                    selectable=True,
                    css_classes=["ws-switch-row"] + (["ws-current"] if is_cur else []),
                )
                h = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=8,
                    valign=Gtk.Align.CENTER,
                    hexpand=True,
                )
                h.set_margin_top(6)
                h.set_margin_bottom(6)
                h.set_margin_start(8)
                h.set_margin_end(8)
                dot = Gtk.Label(label="●" if is_cur else "○", css_classes=["sb-ws-icon"])
                h.append(dot)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True)
                title = Gtk.Label(
                    label=name + ("  · текущий" if is_cur else ""),
                    halign=Gtk.Align.START,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.MIDDLE,
                    css_classes=["sb-ws-name"],
                )
                path_lbl = Gtk.Label(
                    label=path,
                    halign=Gtk.Align.START,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.MIDDLE,
                    css_classes=["dim-label", "sb-ws-path"],
                )
                vbox.append(title)
                vbox.append(path_lbl)
                h.append(vbox)
                row.set_child(h)
                row._ws_path = path  # type: ignore[attr-defined]
                row._ws_name = name  # type: ignore[attr-defined]
                lb.append(row)
                shown += 1
            if shown == 0:
                row = Gtk.ListBoxRow(activatable=False, selectable=False)
                row.set_child(
                    Gtk.Label(
                        label="ничего не найдено" if q else "список пуст — добавь vault",
                        css_classes=["dim-label"],
                    )
                )
                lb.append(row)
            # состояние кнопок
            sel = lb.get_selected_row()
            has_sel = sel is not None and getattr(sel, "_ws_path", None) is not None
            switch_btn.set_sensitive(bool(has_sel))
            remove_btn.set_sensitive(bool(has_sel))
            if has_sel and getattr(sel, "_ws_path", None) == cur_norm:
                remove_btn.set_sensitive(False)
                switch_btn.set_sensitive(False)

        _rebuild("")
        filter_entry.connect("search-changed", lambda e: _rebuild(e.get_text()))
        lb.connect("row-selected", lambda *_: _rebuild(filter_entry.get_text()))
        lb.connect("row-activated", lambda _lb, row: _do_switch(getattr(row, "_ws_path", None)))

        def _do_switch(path: str | None) -> None:
            if not path:
                return
            try:
                dialog.close()
            except Exception:
                pass
            self._on_workspace_switch(path)

        def _do_add(*_):
            self._show_add_workspace_dialog(parent_dialog=dialog, on_added=lambda: _on_added())

        def _on_added() -> None:
            # перезагрузить список после добавления
            try:
                new_vaults = workspaces_core.get_vaults(self.settings)
            except Exception:
                new_vaults = []
            nonlocal vaults
            vaults = new_vaults
            _rebuild(filter_entry.get_text())
            try:
                if hasattr(self.sidebar, "set_vaults"):
                    self.sidebar.set_vaults(vaults, self.settings.get("vault_root"))
            except Exception:
                pass

        def _do_remove(*_):
            row = lb.get_selected_row()
            if row is None or not getattr(row, "_ws_path", None):
                status.set_label("выбери vault для удаления")
                return
            path = str(row._ws_path)
            try:
                if str(Path(path).expanduser().resolve(strict=False)) == cur_norm:
                    status.set_label("нельзя удалить текущий vault")
                    return
            except Exception:
                if path == current:
                    status.set_label("нельзя удалить текущий vault")
                    return
            try:
                workspaces_core.remove_vault(self.settings, path)
                save_settings(self.settings)
                vaults[:] = workspaces_core.get_vaults(self.settings)  # type: ignore
            except Exception as exc:
                status.set_label(f"ошибка: {exc}")
                return
            _rebuild(filter_entry.get_text())
            try:
                if hasattr(self.sidebar, "set_vaults"):
                    self.sidebar.set_vaults(
                        workspaces_core.get_vaults(self.settings), self.settings.get("vault_root")
                    )
            except Exception:
                pass
            self._notify_toast(f"удалён: {Path(path).name}")
            status.set_label(f"удалён: {path}")

        switch_btn.connect(
            "clicked",
            lambda *_: _do_switch(
                getattr(lb.get_selected_row(), "_ws_path", None) if lb.get_selected_row() else None
            ),
        )
        add_btn.connect("clicked", _do_add)
        remove_btn.connect("clicked", _do_remove)
        close_btn.connect("clicked", lambda *_: dialog.close())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(body)
        dialog.set_child(content)
        dialog.present(self)

    def _show_add_workspace_dialog(self, parent_dialog=None, on_added=None) -> None:
        """Диалог добавления vault: путь + имя."""
        dialog = Adw.Dialog(title="Добавить vault")
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["dialog-body"]
        )
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(16)
        body.set_margin_end(16)

        hint = Gtk.Label(
            label="Укажи путь к папке vault (существующая или новая)\nИмя подставится из имени папки, если оставить пустым.",
            wrap=True,
            halign=Gtk.Align.START,
            xalign=0,
            css_classes=["dim-label"],
        )
        body.append(hint)

        path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, hexpand=True)
        path_entry = Gtk.Entry(placeholder_text="/home/user/notes  или  ~/work-vault", hexpand=True)
        path_entry.set_text(str(self.settings.get("vault_root") or ""))
        path_entry.set_hexpand(True)
        path_row.append(path_entry)
        browse_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Выбрать папку")
        browse_btn.set_can_focus(False)
        path_row.append(browse_btn)
        body.append(
            Gtk.Label(
                label="Путь", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]
            )
        )
        body.append(path_row)

        name_entry = Gtk.Entry(placeholder_text="Имя воркспейса (опционально)", hexpand=True)
        body.append(
            Gtk.Label(label="Имя", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"])
        )
        body.append(name_entry)

        status = Gtk.Label(label="", css_classes=["dim-hint"], halign=Gtk.Align.START, wrap=True)
        body.append(status)

        btn_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["btn-row"]
        )
        btn_row.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        create = Gtk.Button(label="Добавить", css_classes=["suggested-action", "mod-cta"])
        switch_chk = Gtk.CheckButton(label="Переключиться сразу")
        switch_chk.set_active(True)
        btn_row.append(switch_chk)
        btn_row.append(cancel)
        btn_row.append(create)
        body.append(btn_row)

        def _browse(*_):
            # GTK4 FileDialog (предпочтительно) или FileChooserDialog fallback
            try:
                # пробуем FileDialog (GTK 4.10+)
                dlg = Gtk.FileDialog()
                dlg.set_title("Выбери папку vault")
                try:
                    dlg.select_folder(self, None, lambda d2, res: _on_folder_chosen(d2, res))
                except TypeError:
                    # старая сигнатура
                    dlg.select_folder(self, None, _on_folder_chosen)
                return
            except Exception:
                pass
            # fallback: FileChooserDialog
            try:
                chooser = Gtk.FileChooserDialog(
                    title="Выбери папку vault",
                    action=Gtk.FileChooserAction.SELECT_FOLDER,
                    transient_for=self,
                )
                chooser.add_button("Отмена", Gtk.ResponseType.CANCEL)
                chooser.add_button("Выбрать", Gtk.ResponseType.ACCEPT)
                chooser.connect("response", lambda d, r: _on_chooser_response(d, r))
                chooser.present()
            except Exception as exc:
                status.set_label(f"выбор папки недоступен: {exc}")

        def _on_folder_chosen(dlg, res) -> None:
            try:
                f = dlg.select_folder_finish(res)
                if f is not None:
                    p = f.get_path() or f.get_uri()
                    if p:
                        # если uri file://
                        if str(p).startswith("file://"):
                            from urllib.parse import unquote

                            p = unquote(str(p)[7:])
                        path_entry.set_text(str(p))
            except Exception as exc:
                status.set_label(f"ошибка выбора: {exc}")

        def _on_chooser_response(dlg, resp) -> None:
            try:
                if resp == Gtk.ResponseType.ACCEPT:
                    f = dlg.get_file()
                    if f is not None:
                        p = f.get_path()
                        if p:
                            path_entry.set_text(str(p))
                dlg.close()
            except Exception:
                try:
                    dlg.close()
                except Exception:
                    pass

        browse_btn.connect("clicked", _browse)

        def _do_create(*_):
            raw = path_entry.get_text().strip()
            if not raw:
                status.set_label("укажи путь к vault")
                return
            name = name_entry.get_text().strip() or None
            norm = raw
            try:
                norm = str(Path(raw).expanduser().resolve(strict=False))
            except Exception:
                pass
            # валидация: не пустой, не дублируем без нужды
            try:
                workspaces_core.add_vault(self.settings, norm, name)
                save_settings(self.settings)
            except Exception as exc:
                status.set_label(f"ошибка: {exc}")
                return
            # обновить sidebar
            try:
                if hasattr(self.sidebar, "set_vaults"):
                    self.sidebar.set_vaults(
                        workspaces_core.get_vaults(self.settings), self.settings.get("vault_root")
                    )
            except Exception:
                pass
            self._notify_toast(f"добавлен: {Path(norm).name}")
            try:
                dialog.close()
            except Exception:
                pass
            if on_added is not None:
                try:
                    on_added()
                except Exception:
                    pass
            if switch_chk.get_active():
                self._on_workspace_switch(norm)
            elif parent_dialog is not None:
                try:
                    parent_dialog.present(self)
                except Exception:
                    pass

        cancel.connect("clicked", lambda *_: dialog.close())
        create.connect("clicked", _do_create)
        path_entry.connect("activate", _do_create)
        name_entry.connect("activate", _do_create)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(body)
        dialog.set_child(content)
        body.set_size_request(460, -1)
        dialog.present(self)
        path_entry.grab_focus()

    def _open_snippets_palette(self) -> None:
        """Открыть палитру сниппетов (trigger → expansion)."""
        self._show_view("files")
        fv = self._views.get("files")
        if fv is not None and hasattr(fv, "_on_snippets_palette"):
            GLib.idle_add(lambda: fv._on_snippets_palette() or False)  # type: ignore
        else:
            self._notify_toast("сниппеты — открой вкладку Заметки")

    def _focus_global_search(self) -> None:
        """Ctrl+Shift+F — открыть вкладку файлов и фокус на filter_entry."""
        self._show_view("files")
        fv = self._views.get("files")
        if fv is not None and hasattr(fv, "focus_filter"):
            GLib.idle_add(lambda: fv.focus_filter() or False)

    def _duplicate_line(self) -> bool:
        """Ctrl+D — дублировать строку под курсором в активном редакторе."""
        # files_view — основной редактор
        fv = self._views.get("files")
        if fv is not None and hasattr(fv, "duplicate_line"):
            try:
                if fv.duplicate_line():
                    return True
            except Exception:  # noqa: BLE001
                pass
        # daily_view — тоже может иметь редактор
        dv = self._views.get("daily")
        if dv is not None and hasattr(dv, "duplicate_line"):
            try:
                if dv.duplicate_line():
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _save_current(self) -> None:
        cur = self._visible_name()
        v = self._views.get(cur)
        if v is None:
            return
        try:
            if cur == "daily" and not v.save_btn.get_sensitive():
                return
            if hasattr(v, "_on_save"):
                v._on_save(None)
        except Exception:  # noqa: BLE001
            pass
        # plugin hook on_save
        try:
            if hasattr(self, "plugin_manager"):
                path = getattr(v, "_current", None) if v is not None else None
                if path is not None:
                    p = Path(path) if not isinstance(path, Path) else path
                    # попытаться передать контент если есть buffer
                    content = None
                    try:
                        if hasattr(v, "buffer"):
                            s, e = v.buffer.get_bounds()
                            content = v.buffer.get_text(s, e, True)
                    except Exception:
                        content = None
                    if content is not None:
                        self.plugin_manager.trigger("on_save", p, content)
                    else:
                        self.plugin_manager.trigger("on_save", p)
        except Exception:
            pass

    def _toggle_editor_preview(self) -> None:
        fv = self._views.get("files")
        if fv is None or fv._current is None:
            return
        name = fv.stack.get_visible_child_name()
        fv.stack.set_visible_child_name("Просмотр" if name == "Редактор" else "Редактор")
        if fv.stack.get_visible_child_name() == "Просмотр":
            fv._render_preview()

    def _new_note_dialog(self) -> None:
        """Создание заметки как в Obsidian: диалог имени, файл в текущей папке + выбор шаблона."""
        fv = self._views.get("files")
        root = Path(self.settings["vault_root"])
        folder = root
        if fv is not None:
            cur = Path(fv._current) if fv._current else None
            if cur is not None and cur.parent.is_dir():
                folder = cur.parent

        # список шаблонов для выпадающего меню
        try:
            from ..core.templates import list_templates as _list_tpl

            templates = _list_tpl(self.settings)
            tpl_names = ["— без шаблона —"] + [p.stem for p in templates]
        except Exception:
            templates = []
            tpl_names = ["— без шаблона —"]

        dialog = Adw.Dialog(title="Новая заметка")
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            css_classes=["dialog-body"],
            margin_top=8,
            margin_bottom=8,
            margin_start=24,
            margin_end=24,
        )
        hint = Gtk.Label(
            label=folder.relative_to(root).as_posix() or root.as_posix(),
            css_classes=["dim-label"],
            halign=Gtk.Align.START,
        )
        hint.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        entry = Gtk.Entry(placeholder_text="Название заметки", hexpand=True)
        body.append(hint)
        body.append(entry)
        # выбор шаблона
        if len(tpl_names) > 1:
            tpl_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            tpl_row.append(
                Gtk.Label(
                    label="Шаблон", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]
                )
            )
            tpl_drop = Gtk.DropDown.new_from_strings(tpl_names)
            tpl_drop.set_selected(0)
            tpl_row.append(tpl_drop)
            hint2 = Gtk.Label(
                label="vault/_System/Templates/ · {{date}} {{time}} {{title}} {{uuid}}",
                halign=Gtk.Align.START,
                xalign=0,
                css_classes=["dim-hint"],
            )
            hint2.set_wrap(True)
            tpl_row.append(hint2)
            body.append(tpl_row)
        else:
            tpl_drop = None  # type: ignore[assignment]
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["btn-row"])
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        create = Gtk.Button(label="Создать", css_classes=["suggested-action", "mod-cta"])
        cancel.connect("clicked", lambda *_: dialog.close())

        def _do_create(*_):
            sel = ""
            if tpl_drop is not None:
                idx = tpl_drop.get_selected()
                if idx > 0:
                    sel = tpl_names[int(idx)]
            self._create_note(
                dialog,
                entry.get_text().strip(),
                folder,
                template_name=sel if sel and sel != "— без шаблона —" else None,
            )

        create.connect("clicked", _do_create)
        entry.connect("activate", _do_create)
        row.append(cancel)
        row.append(create)
        row.set_halign(Gtk.Align.END)
        body.append(row)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(body)
        dialog.set_child(content)
        body.set_size_request(360, -1)
        dialog.present(self)
        entry.grab_focus()

    def _create_note(
        self, dialog, name: str, folder: Path, template_name: str | None = None
    ) -> None:
        if not name:
            return
        target = folder / (name if name.lower().endswith(".md") else name + ".md")
        if target.exists():
            self._notify_toast(f"заметка уже есть: {target.name}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        # применение шаблона если выбран
        content = ""
        if template_name:
            try:
                from ..core.templates import render_for_new_note

                title = target.stem
                content = render_for_new_note(self.settings, template_name, title)
            except Exception:
                content = ""
        target.write_text(content, encoding="utf-8")
        # plugin hook on_new
        try:
            if hasattr(self, "plugin_manager"):
                self.plugin_manager.trigger("on_new", target)
        except Exception:
            pass
        dialog.close()
        self._open_note(target)
        self._notify_toast(
            f"создана: {target.name}" + (f" из шаблона {template_name}" if template_name else "")
        )

    # ── Web Clipper ──────────────────────────────────────────────
    def _web_clip_dialog(self) -> None:
        """Диалог Web Clipper: URL → vault/Clippings/ как markdown."""
        try:
            clip_dir = self.web_clipper.clippings_dir()
        except Exception:
            clip_dir = Path(self.settings["vault_root"]) / "Clippings"
        dialog = Adw.Dialog(title="Web Clipper")
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            css_classes=["dialog-body"],
            margin_top=8,
            margin_bottom=8,
            margin_start=24,
            margin_end=24,
        )
        hint = Gtk.Label(
            label=f"Сохранит в {clip_dir}/*.md",
            css_classes=["dim-label"],
            halign=Gtk.Align.START,
            wrap=True,
        )
        hint.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        url_entry = Gtk.Entry(placeholder_text="https://example.com/article", hexpand=True)
        url_entry.set_input_purpose(Gtk.InputPurpose.URL)
        title_entry = Gtk.Entry(
            placeholder_text="Заголовок (опционально — возьмётся из <title>)", hexpand=True
        )
        body.append(hint)
        body.append(Gtk.Label(label="URL", halign=Gtk.Align.START, css_classes=["settings-label"]))
        body.append(url_entry)
        body.append(
            Gtk.Label(label="Заголовок", halign=Gtk.Align.START, css_classes=["settings-label"])
        )
        body.append(title_entry)
        status = Gtk.Label(label="", css_classes=["dim-hint"], halign=Gtk.Align.START, wrap=True)
        body.append(status)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["btn-row"])
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        clip_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action", "mod-cta"])
        cancel.connect("clicked", lambda *_: dialog.close())
        row.append(cancel)
        row.append(clip_btn)
        row.set_halign(Gtk.Align.END)
        body.append(row)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(body)
        dialog.set_child(content)
        body.set_size_request(520, -1)

        def _do_clip(*_):
            url = url_entry.get_text().strip()
            if not url:
                status.set_label("введи URL")
                return
            title = title_entry.get_text().strip() or None
            clip_btn.set_sensitive(False)
            cancel.set_sensitive(False)
            status.set_label("загружаю…")
            settings_copy = dict(self.settings)

            def work() -> None:
                try:
                    path = self.web_clipper.clip(url, settings=settings_copy, title=title)
                    GLib.idle_add(lambda: _on_done(path))
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)[:300]
                    GLib.idle_add(lambda: _on_error(msg))

            def _on_done(path: Path) -> bool:
                try:
                    dialog.close()
                except Exception:
                    pass
                try:
                    self.vault_service.invalidate_cache()
                except Exception:
                    pass
                try:
                    self._open_note(str(path))
                except Exception:
                    pass
                self._notify_toast(f"сохранено: {path.name} → Clippings/")
                # обновить дерево если файлы открыты
                try:
                    fv = self._views.get("files")
                    if fv is not None and hasattr(fv, "reload"):
                        fv.reload(force=True)
                except Exception:
                    pass
                return False

            def _on_error(msg: str) -> bool:
                clip_btn.set_sensitive(True)
                cancel.set_sensitive(True)
                status.set_label(f"ошибка: {msg}")
                return False

            threading.Thread(target=work, daemon=True).start()

        clip_btn.connect("clicked", _do_clip)
        url_entry.connect("activate", _do_clip)
        title_entry.connect("activate", _do_clip)
        dialog.present(self)
        url_entry.grab_focus()

    def _open_note(self, path: str, highlight: str | None = None) -> None:
        p = Path(path)
        if not p.is_file():
            return
        self._show_view("files")
        fv = self._views.get("files")
        if fv is not None:
            fv.open_path(p, highlight)
        # keep graph/mindmap current in sync
        for key in ("graph", "mindmap"):
            gv = self._views.get(key)
            if gv is not None and hasattr(gv, "set_current_file"):
                try:
                    gv.set_current_file(p)
                except Exception:
                    pass
        # plugin hook on_open
        try:
            if hasattr(self, "plugin_manager"):
                self.plugin_manager.trigger("on_open", p)
        except Exception:
            pass

    def _restore_state(self) -> None:
        ui_state = self.settings.get("ui_state") or {}
        view = ui_state.get("view", "files")
        if ui_state.get("maximized"):
            self.maximize()
        else:
            w = ui_state.get("width")
            h = ui_state.get("height")
            if w and h:
                self.set_default_size(int(w), int(h))
            else:
                self._default_size_percent()
        view = view if view in VIEWS else "files"
        self._show_view(view)
        # Восстановить ширину сайдбара в Paned (отложенно — после realize)
        if hasattr(self, "top") and hasattr(self, "_sidebar_width"):
            w = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, int(self._sidebar_width)))
            is_open = (
                self.sidebar_revealer.get_reveal_child()
                if hasattr(self, "sidebar_revealer")
                else False
            )
            target = w if is_open else _SIDEBAR_COLLAPSED
            GLib.idle_add(
                lambda: self.top.set_position(target)
                if self.top.get_position() != target
                else False
            )

    def _on_close_request(self, _win) -> bool:
        # Tray daemon: скрыть окно вместо выхода, держать Gio.Application --gapplication-service
        try:
            tray = getattr(self, "_tray", None)
            if tray is not None and hasattr(tray, "is_active") and tray.is_active():
                # flush dirty но не останавливаем сервисы — остаёмся в фоне
                try:
                    self._flush_dirty()
                except Exception:
                    pass
                if (
                    hasattr(self, "top")
                    and getattr(self, "sidebar_revealer", None)
                    and self.sidebar_revealer.get_reveal_child()
                ):
                    cur = self.top.get_position()
                    if _SIDEBAR_MIN <= cur <= _SIDEBAR_MAX:
                        self._save_sidebar_width(cur)
                self.settings["ui_state"] = {
                    **self.settings.get("ui_state", {}),
                    "maximized": self.is_maximized(),
                    "width": self.get_width() if not self.is_maximized() else 1280,
                    "height": self.get_height() if not self.is_maximized() else 820,
                }
                try:
                    save_settings(self.settings)
                except Exception:
                    pass
                try:
                    self.set_visible(False)
                except Exception:
                    pass
                return True
        except Exception:
            pass
        self._flush_dirty()
        try:
            self._cancel_vault_monitor()
        except Exception:
            pass
        try:
            self._cancel_custom_css_monitor()
        except Exception:
            pass
        try:
            if hasattr(self, "git_sync"):
                self.git_sync.stop()
        except Exception:
            pass
        try:
            tray2 = getattr(self, "_tray", None)
            if tray2 is not None and hasattr(tray2, "destroy"):
                tray2.destroy()
        except Exception:
            pass
        # Сохранить актуальную ширину сайдбара если он открыт и в лимитах
        if (
            hasattr(self, "top")
            and getattr(self, "sidebar_revealer", None)
            and self.sidebar_revealer.get_reveal_child()
        ):
            cur = self.top.get_position()
            if _SIDEBAR_MIN <= cur <= _SIDEBAR_MAX:
                self._save_sidebar_width(cur)
        self.settings["ui_state"] = {
            **self.settings.get("ui_state", {}),
            "maximized": self.is_maximized(),
            "width": self.get_width() if not self.is_maximized() else 1280,
            "height": self.get_height() if not self.is_maximized() else 820,
        }
        save_settings(self.settings)
        return False

    def _flush_dirty(self) -> None:
        """Сохранить несохранённые правки (files/daily) перед close/пересборкой вьюх."""
        for name in ("files", "daily"):
            v = self._views.get(name)
            if v is None:
                continue
            try:
                if name == "files" and getattr(v, "_dirty", False):
                    v._on_save(None)
                elif (
                    name == "daily"
                    and getattr(v, "save_btn", None) is not None
                    and v.save_btn.get_sensitive()
                ):
                    v._on_save(None)
            except Exception:  # noqa: BLE001
                pass

    def _on_task_action(self, action: str) -> None:
        labels = {
            "done": "✓ Задача выполнена",
            "skip": "⏭ Рутина пропущена",
            "pay": "✓ Счёт оплачен",
        }
        self._notify_toast(labels.get(action, action))
        self._tasks_dirty = True
        self.refresh_all()

    def _on_pomodoro_action(self, kind: str, task) -> None:
        if kind == "work_done":
            title = getattr(task, "title", "") if task is not None else ""
            msg = f"🍅 Pomodoro: {title}" if title else "🍅 Pomodoro завершён — перерыв 5 мин"
            self._notify_toast(msg, timeout=6)
        elif kind == "break_done":
            self._notify_toast("☕ Перерыв окончен — пора работать", timeout=6)
        self._tasks_dirty = True
        # обновить pomodoro стату в видимой вкладке
        try:
            pv = self._views.get("pomodoro")
            if pv is not None and hasattr(pv, "_refresh_stats_full"):
                pv._refresh_stats_full()
        except Exception:
            pass

    # ── Действия ─────────────────────────────────────────────
    def _on_runner_action(self, task_id: str, action: str) -> None:
        if self.controller.status.state == model.RUNNING:
            self.toast_overlay.add_toast(Adw.Toast.new("Операция уже выполняется"))
            return
        ctrl = self.controller
        d = next((x for x in ctrl.defs if x.id == task_id), None)
        if d is None:
            return
        ctrl.status.current_command = d.command_ids[0] if d.command_ids else None
        ctrl.status.state = model.RUNNING
        ctrl.poll_status()
        self.refresh_all()
        threading.Thread(target=self._execute, args=(task_id, action), daemon=True).start()

    def _execute(self, task_id: str, action: str) -> None:
        try:
            ok, detail = self.controller.run_operation(task_id, action)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
            self.controller.finish(False, str(exc))
        self.log = detail or ("OK" if ok else "Ошибка")
        GLib.idle_add(self._op_done, ok, detail)

    def _op_done(self, ok: bool, detail: str) -> None:
        toast = Adw.Toast.new(("✓ " if ok else "✗ ") + (detail or "готово"))
        toast.set_timeout(6)
        self.toast_overlay.add_toast(toast)
        self.refresh_llm_async()
        self.refresh_all()

    def _on_settings_saved(self, settings: dict) -> None:
        self.settings = settings
        if hasattr(self, "vault_service"):
            self.vault_service.update_settings(settings)
        if hasattr(self, "git_sync"):
            try:
                self.git_sync.update_settings(settings)
            except Exception:
                pass
        save_settings(settings)
        # Тема + кастом CSS
        try:
            theme_manager.apply_theme(theme_manager.get_theme(settings))
        except Exception:
            pass
        try:
            from .style import reload_custom_css as _reload_ccss

            _reload_ccss(settings)
        except Exception:
            try:
                theme_manager.reload_custom_css(settings)
            except Exception:
                pass
        scale.init(
            _fs(settings.get("ui_scale"), 1.0),
            _fs(settings.get("editor_zoom"), 1.0),
            bool(settings.get("follow_system_scale", True)),
        )
        self._apply_scale()
        self.engine = EngineBridge(settings)
        self.llm = LlmService(settings)
        self.controller = RunnerController(
            settings,
            self.engine,
            self.llm,
            history=settings.get("task_history"),
        )
        self.quick.settings = settings
        if hasattr(self, "quick_capture"):
            self.quick_capture.update_settings(settings)
        if hasattr(self, "web_clipper"):
            try:
                self.web_clipper.update_settings(settings)
            except Exception:
                pass
        # Tray daemon — обновить vault_root для Quick Capture
        try:
            tray = getattr(self, "_tray", None)
            if tray is not None and hasattr(tray, "update_settings"):
                tray.update_settings(settings)
            elif tray is None:
                # tray мог не создаться ранее (headless) — пробуем пересоздать
                from ..services.tray import setup_tray as _setup_tray2

                self._tray = _setup_tray2(self.get_application(), self, settings)
        except Exception:
            pass
        self.sidebar.set_vault(settings["vault_root"])
        # Workspaces sidebar: обновить список vaults
        try:
            vaults = workspaces_core.get_vaults(settings)
            if hasattr(self.sidebar, "set_vaults"):
                self.sidebar.set_vaults(vaults, settings.get("vault_root"))
        except Exception:
            pass
        # перезагрузить плагины при смене vault_root
        if hasattr(self, "plugin_manager"):
            try:
                self.plugin_manager.update_settings(settings)
                self.plugin_manager.load_plugins(settings)
            except Exception:
                pass
        self._llm_sig = None
        self._bg_scan_at = 0
        self._tasks_dirty = True
        self._setup_vault_monitor()
        self._setup_custom_css_monitor()
        try:
            if hasattr(self, "git_sync"):
                self.git_sync.start()
        except Exception:
            pass
        self._rebuild_ribbon()
        self._rebuild_views()
        self.refresh_llm_async()
        self.refresh_all()

    def _rebuild_views(self) -> None:
        self._flush_dirty()
        for name, child in list(self._views.items()):
            self.stack.remove(child)
        self._views.clear()
        cur = self.settings.get("ui_state", {}).get("view", "home")
        self._show_view(cur if cur in VIEWS else "home")

    # ── Фоновая работа ───────────────────────────────────────
    def refresh_llm_async(self) -> None:
        def work() -> None:
            day = self.llm.ping("day")
            arch = self.llm.ping("archive")
            GLib.idle_add(self._llm_ready)

        threading.Thread(target=work, daemon=True).start()

    def _llm_ready(self) -> None:
        self.refresh_all()

    def refresh_enrich_async(self) -> None:
        url = self.settings.get("engine_status_url", "")
        if not url:
            return

        def work() -> None:
            st = fetch_enrich_status(url)
            GLib.idle_add(self._enrich_ready, st)

        threading.Thread(target=work, daemon=True).start()

    def _enrich_ready(self, st: EnrichStatus) -> None:
        self.enrich = st
        self.refresh_all()

    def _notify_toast(self, msg: str, timeout: int = 4) -> None:
        t = Adw.Toast.new(msg)
        t.set_timeout(timeout)
        self.toast_overlay.add_toast(t)

    def refresh_all(self) -> None:
        """Дешёвый тик: статус-бар + только видимая вкладка."""
        busy = self.controller.status.state == model.RUNNING
        cur = self._visible_name()

        if cur == "home" and "home" in self._views:
            self._views["home"].refresh(
                self.controller,
                self.llm,
                self.enrich,
                due_today=self._task_counts["today"],
                overdue=self._task_counts["overdue"],
            )
        elif cur == "runner" and "runner" in self._views:
            self._views["runner"].refresh(busy, self.log, self.enrich, self._inbox_undecided)
        elif cur == "tasks" and "tasks" in self._views:
            if self._tasks_dirty:
                tv = self._views["tasks"]
                tv.reload()
                self._task_counts = {"today": tv.today_count, "overdue": tv.overdue_count}
                self._tasks_dirty = False
        elif cur == "daily" and "daily" in self._views:
            self._views["daily"].reload_if_needed()
        elif cur == "calendar" and "calendar" in self._views:
            try:
                self._views["calendar"].reload()
            except Exception:
                pass
        elif cur == "review" and "review" in self._views:
            try:
                self._views["review"].reload()
            except Exception:
                pass
        elif cur == "kanban" and "kanban" in self._views:
            # kanban — лёгкий reload при bg-скане (vault monitor дебаунс)
            try:
                self._views["kanban"].reload()
            except Exception:
                pass
        elif cur == "database" and "database" in self._views:
            try:
                self._views["database"].reload()
            except Exception:
                pass
        elif cur == "pomodoro" and "pomodoro" in self._views:
            try:
                self._views["pomodoro"].reload()
            except Exception:
                pass
        elif cur == "habits" and "habits" in self._views:
            try:
                self._views["habits"].reload()
            except Exception:
                pass
        elif cur == "srs" and "srs" in self._views:
            try:
                self._views["srs"].reload()
            except Exception:
                pass

        self.status_bar.refresh(
            self.controller,
            self.llm,
            self.enrich,
            due_today=self._task_counts["today"],
            overdue=self._task_counts["overdue"],
            log=self.log,
            git_sync=getattr(self, "git_sync", None),
        )
        # AI чат — обновить ссылку на llm после пересоздания и статус
        av = self._views.get("ai_chat")
        if av is not None and hasattr(av, "refresh_llm"):
            if getattr(av, "llm", None) is not self.llm:
                av.refresh_llm(self.llm)
            elif cur == "ai_chat":
                try:
                    av._refresh_status()
                except Exception:
                    pass
        self._maybe_refresh_llm_popover()

    def _maybe_refresh_llm_popover(self) -> None:
        day = self.llm.get_status("day")
        arch = self.llm.get_status("archive")
        sig = (
            day.online,
            day.ctx_size,
            arch.online,
            arch.ctx_size,
            str(getattr(self.llm.script, "name", "")),
        )
        if sig == getattr(self, "_llm_sig", None):
            return
        self._llm_sig = sig
        self._refresh_llm_popover()

    def _poll(self) -> bool:
        if self.controller.status.state == model.RUNNING:
            self.controller.poll_status()
        self._maybe_bg_scan()
        self.refresh_all()
        return True

    # ── Фоновые сканы (vault-обходы вне GUI-потока) ──────────
    def _maybe_bg_scan(self) -> None:
        now = GLib.get_monotonic_time() // 1000
        if now - self._bg_scan_at < BG_SCAN_MS:
            return
        self._bg_scan_at = now
        settings = dict(self.settings)
        threading.Thread(target=self._bg_scan_work, args=(settings,), daemon=True).start()

    def _bg_scan_work(self, settings: dict) -> None:
        from ..core import tasks as tm
        from ..paths import resolve_paths

        svc = getattr(self, "vault_service", None)
        undecided = 0
        try:
            if svc is not None:
                _, _, undecided = svc.scan_inbox(limit=1, settings=settings)
            else:
                from ..services import vault

                _, _, undecided = vault.scan_inbox(settings, limit=1)
        except Exception:  # noqa: BLE001
            undecided = 0
        counts = {"today": 0, "overdue": 0}
        try:
            paths = resolve_paths(settings)
            all_tasks = tm.load_tasks(paths.tm_tasks)
            today = datetime.date.today()
            counts = {
                "today": len([t for t in all_tasks if t.is_active and t.is_due_on(today)]),
                "overdue": len([t for t in all_tasks if t.is_active and t.is_overdue(today)]),
            }
        except Exception:  # noqa: BLE001
            pass
        GLib.idle_add(self._bg_scan_done, undecided, counts)

    def _bg_scan_done(self, undecided: int, counts: dict) -> bool:
        changed = undecided != self._inbox_undecided or counts != self._task_counts
        self._inbox_undecided = undecided
        self._task_counts = counts
        if changed:
            self._tasks_dirty = True
            if self._visible_name() != "tasks":
                self.refresh_all()
        return False

    def _poll_enrich(self) -> bool:
        self.refresh_enrich_async()
        return True

    # ── FileMonitor vault_root ───────────────────────────
    def _setup_vault_monitor(self) -> None:
        self._cancel_vault_monitor()
        root = self.settings.get("vault_root")
        if not root:
            return
        try:
            p = Path(str(root))
            if not p.is_dir():
                return
            gfile = Gio.File.new_for_path(str(p))
            # достаточно корня (нерекурсивно) — кэши инвалидируются при любом изменении сверху
            mon = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", self._on_vault_changed)
            self._vault_monitor = mon
        except Exception:
            self._vault_monitor = None

    def _cancel_vault_monitor(self) -> None:
        if getattr(self, "_vault_monitor", None) is not None:
            try:
                self._vault_monitor.cancel()
            except Exception:
                pass
            self._vault_monitor = None
        if getattr(self, "_vault_monitor_timer", None) is not None:
            try:
                GLib.source_remove(self._vault_monitor_timer)
            except Exception:
                pass
            self._vault_monitor_timer = None

    def _on_vault_changed(
        self,
        _mon: Gio.FileMonitor,
        _file: Gio.File,
        _other: Gio.File | None,
        event: Gio.FileMonitorEvent,
    ) -> None:
        # реагируем только на реальные изменения структуры/содержимого
        if event not in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        ):
            return
        # debounce 400мс — не спамим инвалидацией при массовых событиях
        if self._vault_monitor_timer is not None:
            try:
                GLib.source_remove(self._vault_monitor_timer)
            except Exception:
                pass
            self._vault_monitor_timer = None
        self._vault_monitor_timer = GLib.timeout_add(400, self._do_vault_changed)

    def _do_vault_changed(self) -> bool:
        self._vault_monitor_timer = None
        # Git auto-sync debounce 30с
        try:
            if hasattr(self, "git_sync"):
                self.git_sync.schedule_sync()
        except Exception:
            pass
        try:
            self.vault_service.invalidate_vault_cache()
        except Exception:
            try:
                from ..services import vault as _vault

                _vault.invalidate_vault_cache()
            except Exception:
                pass
        # обновить дерево если вьюха файлов видима
        try:
            fv = self._views.get("files")
            if fv is not None and hasattr(fv, "reload"):
                # видимо если стек показывает files (или tags — тоже files)
                vis = self._visible_name() if hasattr(self, "_visible_name") else ""
                if vis in ("files", "tags"):
                    fv.reload(force=True)
                elif vis == "":
                    # окно ещё не показало вьюху — всё равно перезагрузить для кэша
                    pass
                else:
                    # невидимая — отложим reload, но кэш уже инвалидирован
                    # также пробуем обновить если files существует но скрыт (чтобы данные были свежими при следующем показе)
                    # не вызываем чтобы не трогать hidden widget — достаточно инвалидации
                    pass
            # также пробуем обновить graph/tags если есть
        except Exception:
            pass
        # database: если видима — перезагрузить
        try:
            vis = self._visible_name() if hasattr(self, "_visible_name") else ""
            if vis == "database":
                dv = self._views.get("database")
                if dv is not None and hasattr(dv, "reload"):
                    dv.reload()
        except Exception:
            pass
        return False

    # ── Монитор custom.css ───────────────────────────────────
    def _setup_custom_css_monitor(self) -> None:
        self._cancel_custom_css_monitor()
        try:
            cpath = theme_manager.custom_css_path(self.settings)
        except Exception:
            return
        # Мониторим как файл (если есть) так и папку _System (если файла нет — ждём создания)
        try:
            target = cpath if cpath.is_file() else cpath.parent
            if not target.exists():
                return
            gfile = Gio.File.new_for_path(str(target))
            flags = Gio.FileMonitorFlags.NONE
            mon = gfile.monitor(flags, None) if target.is_dir() else gfile.monitor_file(flags, None)
            mon.connect("changed", self._on_custom_css_changed)
            self._custom_css_monitor = mon
        except Exception:
            self._custom_css_monitor = None

    def _cancel_custom_css_monitor(self) -> None:
        if getattr(self, "_custom_css_monitor", None) is not None:
            try:
                self._custom_css_monitor.cancel()
            except Exception:
                pass
            self._custom_css_monitor = None
        if getattr(self, "_custom_css_timer", None) is not None:
            try:
                GLib.source_remove(self._custom_css_timer)
            except Exception:
                pass
            self._custom_css_timer = None

    def _on_custom_css_changed(self, _mon, _file, _other, event) -> None:
        # Фильтруем по имени если мониторим директорию
        try:
            fname = _file.get_basename() if _file is not None else ""
            if (
                fname
                and fname != "custom.css"
                and getattr(self, "_custom_css_monitor", None) is not None
            ):
                # если мониторим папку — реагируем только на custom.css
                is_dir = theme_manager.custom_css_path(self.settings).parent.is_dir()
                if is_dir and fname != "custom.css":
                    return
        except Exception:
            pass
        if event not in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        ):
            return
        if self._custom_css_timer is not None:
            try:
                GLib.source_remove(self._custom_css_timer)
            except Exception:
                pass
        self._custom_css_timer = GLib.timeout_add(300, self._do_custom_css_changed)

    def _do_custom_css_changed(self) -> bool:
        self._custom_css_timer = None
        try:
            from .style import reload_custom_css as _reload

            _reload(self.settings)
        except Exception:
            try:
                theme_manager.reload_custom_css(self.settings)
            except Exception:
                pass
        # если файл был создан/удалён — перенастроить монитор (файл↔директория)
        try:
            cpath = theme_manager.custom_css_path(self.settings)
            is_file = cpath.is_file()
            cur_mon = getattr(self, "_custom_css_monitor", None)
            # простая эвристика: если файл появился а мониторили папку — пересетапим
            if cur_mon is not None:
                self._setup_custom_css_monitor()
        except Exception:
            pass
        return False

    def _persist_history(self) -> bool:
        self.settings["task_history"] = {
            k: [r.__dict__ for r in v] for k, v in self.controller.task_history.items()
        }
        save_settings(self.settings)
        return True


def _fs(value, default: float) -> float:
    """Безопасный float из настроек (источник может быть строкой из старого UI)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
