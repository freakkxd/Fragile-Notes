from __future__ import annotations

from pathlib import Path

from gi.repository import Gtk

try:
    from gi.repository import WebKit
    HAS_WEBKIT = True
except Exception:
    HAS_WEBKIT = False
    WebKit = None  # type: ignore

try:
    import markdown as _md
    HAS_MD = True
except Exception:
    HAS_MD = False
    _md = None  # type: ignore

try:
    from markdown_it import MarkdownIt
    HAS_MD_IT = True
except Exception:
    HAS_MD_IT = False
    MarkdownIt = None  # type: ignore

CSS = """
:root { --bg: #0a0e16; --fg: #d2dae8; --muted: #8c98ac; --accent: #8ab4ff; --code-bg: #171a20; --border: rgba(255,255,255,0.07); }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 48px; background: var(--bg); color: var(--fg); font-family: Inter, system-ui, sans-serif; line-height: 1.7; max-width: 760px; margin: 0 auto; }
h1 { color: #f0f4fc; font-size: 28px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }
h2 { color: #afbacc; font-size: 20px; text-transform: uppercase; letter-spacing: 0.5px; }
h3 { color: #e4eaf6; font-size: 17px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: var(--code-bg); padding: 2px 6px; border-radius: 4px; font-family: JetBrains Mono, monospace; font-size: 13px; }
pre { background: #04050a; padding: 16px; border-radius: 8px; overflow: auto; border: 1px solid var(--border); }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid var(--accent); margin: 16px 0; padding: 8px 16px; background: rgba(138,180,255,0.06); }
table { border-collapse: collapse; width: 100%; margin: 16px 0; }
th, td { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }
th { background: rgba(255,255,255,0.04); }
hr { border: none; border-top: 1px solid var(--border); margin: 24px 0; }
img { max-width: 100%; border-radius: 8px; }
"""

def md_to_html(text: str) -> str:
    if HAS_MD_IT:
        try:
            md = MarkdownIt("commonmark", {"html": True}).enable("table")
            return md.render(text)
        except Exception:
            pass
    if HAS_MD:
        try:
            import markdown as m
            return m.markdown(text, extensions=["tables", "fenced_code", "codehilite", "toc"])
        except Exception:
            pass
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{esc}</pre>"

def wrap_html(body: str, custom_css: str | None = None) -> str:
    extra = f"<style>{custom_css}</style>" if custom_css else ""
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style>{extra}</head><body class='markdown-body'>{body}</body></html>"

class HtmlPreview(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self._custom_css: str | None = None
        self._load_custom()
        if HAS_WEBKIT:
            self.webview = WebKit.WebView()
            self.webview.set_vexpand(True)
            self.webview.set_hexpand(True)
            scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
            scroller.set_child(self.webview)
            self.append(scroller)
            self._has_wv = True
        else:
            self._label = Gtk.Label(label="WebKitGTK не найден — установите webkitgtk-6.0 для HTML preview", wrap=True, css_classes=["dim-label"], vexpand=True)
            self.append(self._label)
            self._has_wv = False
        self._html: str = ""

    def _load_custom(self) -> None:
        try:
            from ..config import load_settings
            from .theme_manager import custom_css_path as _cp
            s = load_settings()
            p = _cp(s)
            if p and p.is_file():
                self._custom_css = p.read_text(encoding="utf-8", errors="ignore")[:20000]
        except Exception:
            self._custom_css = None

    def set_markdown(self, text: str, highlight: str | None = None) -> None:
        body = md_to_html(text or "")
        html = wrap_html(body, self._custom_css)
        self._html = html
        if self._has_wv:
            try:
                self.webview.load_html(html, None)
            except Exception:
                pass

    def set_html(self, html: str) -> None:
        if self._has_wv:
            try:
                self.webview.load_html(html, None)
            except Exception:
                pass

    def reload_css(self) -> None:
        self._load_custom()
        if self._html and self._has_wv:
            try:
                body_start = self._html.find("<body")
                if body_start != -1:
                    self.webview.load_html(self._html, None)
            except Exception:
                pass
