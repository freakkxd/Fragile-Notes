from __future__ import annotations

from pathlib import Path

from gi.repository import Gtk, GLib

try:
    from gi.repository import WebKit
    HAS_WEBKIT = True
except Exception:
    HAS_WEBKIT = False
    WebKit = None  # type: ignore


class ReactView(Gtk.Box):
    def __init__(self, settings: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.settings = settings
        if HAS_WEBKIT:
            self.webview = WebKit.WebView()
            self.webview.set_vexpand(True)
            self.webview.set_hexpand(True)
            dist = Path(__file__).parent / "react_dist" / "index.html"
            if dist.is_file():
                self.webview.load_uri(dist.as_uri())
            else:
                self.webview.load_html("<html><body style='background:#1e1e1e;color:#aaa;padding:20px'>React build not found — run <code>npm run build</code> in frontend/</body></html>", None)
            try:
                mgr = self.webview.get_user_content_manager()
                mgr.register_script_message_handler("fragile")
                mgr.connect("script-message-received::fragile", self._on_js)
            except Exception:
                pass
            self.append(self.webview)
        else:
            lbl = Gtk.Label(label="WebKitGTK не найден — fallback к FilesView\nsudo pacman -S webkitgtk-6.0", wrap=True, vexpand=True)
            self.append(lbl)

    def _on_js(self, mgr, msg):
        try:
            txt = msg.get_js_value().to_string()
            if txt.startswith("open:"):
                from pathlib import Path as _P

                p = _P(txt[5:])
                if p.is_file():
                    parent = self.get_ancestor(Gtk.Window)
                    if parent and hasattr(parent, "_open_note"):
                        parent._open_note(str(p))
        except Exception:
            pass

    def load_file(self, path: Path) -> None:
        if not HAS_WEBKIT or not hasattr(self, "webview"):
            return
        try:
            self.webview.evaluate_javascript(f"window.fragileBridge && window.fragileBridge.open && window.fragileBridge.open('{path}')", -1, None, None, None, None, None)
        except Exception:
            pass
