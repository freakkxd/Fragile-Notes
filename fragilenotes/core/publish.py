"""Publish — экспорт vault в статический сайт.

Генерация статического сайта из vault:
- markdown -> HTML (wikilinks, теги, таблицы, код)
- индекс (index.html) со списком заметок и живой фильтр
- граф (graph.json + graph.html) — узлы/рёбра из [[wikilink]]
- копирование ассетов (изображения) c сохранением структуры
- CLI ``fragile publish`` и кнопка в приложении.

Дизайн — AO Glass тёмная тема, без внешних зависимостей (кроме PyYAML).
Поддерживает опционально ``markdown`` / ``markdown_it`` для более богатого рендера.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Константы ─────────────────────────────────────────────────────────

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
TAG_RE = re.compile(r"(?<![\w/])#([a-zA-Z\u0400-\u04FF][\w\u0400-\u04FF-]*)")

HEAVY_DIRS = {
    "node_modules", ".git", "dist", "build", "target", ".venv", "venv",
    "__pycache__", "bin", "obj", ".cache", ".trash",
    ".obsidian", "Trash", "images", "ao-engine",
}

ALLOWED_MD_EXTS = {".md"}
ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif",
    ".pdf", ".mp4", ".mp3", ".wav", ".ogg", ".css", ".js",
}

# ── CSS (AO Glass, минималистичный статический) ─────────────────────

SITE_CSS = r"""
:root{
  --bg:#06080d; --panel:#0b0e16; --panel2:#101520;
  --text:#d2dae8; --muted:#afbacc; --faint:#8c98ac;
  --accent:#8ab4ff; --accent2:#bea5ff; --border:rgba(255,255,255,0.08);
  --code-bg:#171a20; --codeblock-bg:#04050a;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; font-family:Inter,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg); color:var(--text); line-height:1.6; word-wrap:break-word;
}
a{color:var(--accent); text-decoration:none}
a:hover{text-decoration:underline}
header{
  position:sticky; top:0; z-index:10;
  background:rgba(6,8,13,0.82); backdrop-filter:blur(10px);
  border-bottom:1px solid var(--border);
  padding:10px 18px; display:flex; gap:12px; align-items:center;
}
header .brand{font-weight:700; color:#f0f4fc; letter-spacing:0.02em}
header .meta{margin-left:auto; color:var(--faint); font-size:0.85em}
.container{max-width:1100px; margin:0 auto; padding:18px 16px 40px}
.grid{display:grid; grid-template-columns:250px 1fr; gap:18px}
@media(max-width:860px){ .grid{grid-template-columns:1fr} }
.sidebar{
  background:var(--panel); border:1px solid var(--border); border-radius:12px;
  padding:12px; position:sticky; top:60px; max-height:calc(100vh - 80px); overflow:auto;
}
.sidebar h3{margin:0 0 8px; color:var(--muted); font-size:0.78em; letter-spacing:0.08em; text-transform:uppercase}
.sidebar a{display:block; padding:4px 6px; border-radius:6px; color:var(--text); font-size:0.92em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.sidebar a:hover{background:rgba(130,168,255,0.08); text-decoration:none}
.sidebar .search{width:100%; padding:7px 10px; border-radius:8px; border:1px solid var(--border); background:var(--panel2); color:var(--text); outline:none}
.sidebar .count{color:var(--faint); font-size:0.82em; margin-top:6px}
.content{
  background:var(--panel); border:1px solid var(--border); border-radius:12px;
  padding:22px 22px 18px; min-width:0;
}
.content h1{color:#f0f4fc; font-size:1.9em; border-bottom:1px solid var(--border); padding-bottom:8px; margin-top:0}
.content h2{color:var(--muted); font-size:0.95em; text-transform:uppercase; letter-spacing:0.06em; margin:1.4em 0 0.4em}
.content h3{color:#e4eaf6; margin:1.1em 0 0.3em}
.content p{margin:0.7em 0}
.content code{padding:2px 5px; border-radius:6px; background:var(--code-bg); border:1px solid var(--border); font-family:'JetBrains Mono',monospace; font-size:0.88em}
.content pre{padding:12px 14px; border-radius:10px; background:var(--codeblock-bg); border:1px solid var(--border); overflow:auto}
.content pre code{padding:0; border:none; background:transparent}
.content blockquote{margin:0.9em 0; padding:8px 12px 8px 14px; border-left:3px solid var(--accent); background:#151a26; border-radius:8px}
.content table{border-collapse:collapse; width:100%; margin:0.8em 0; font-size:0.92em}
.content th,.content td{border:1px solid var(--border); padding:6px 8px; text-align:left}
.content th{background:var(--panel2); color:var(--muted); font-size:0.82em; text-transform:uppercase; letter-spacing:0.04em}
.content hr{border:none; border-top:1px solid var(--border); margin:1.2em 0}
.content .tag{display:inline-block; padding:1px 8px; border-radius:999px; font-size:0.82em; background:rgba(190,165,255,0.14); color:var(--accent2); border:1px solid rgba(190,165,255,0.22)}
.graph-wrap{width:100%; height:560px; border:1px solid var(--border); border-radius:12px; background:var(--panel); overflow:hidden; position:relative}
.graph-wrap canvas,.graph-wrap svg{width:100%; height:100%; display:block}
.index-list{list-style:none; padding:0; margin:0}
.index-list li{padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.04); display:flex; gap:10px; align-items:center}
.index-list li:hover{background:rgba(255,255,255,0.03)}
.index-list .when{color:var(--faint); font-size:0.82em; white-space:nowrap}
.footer{color:var(--faint); font-size:0.82em; text-align:center; margin-top:18px}
"""

INDEX_JS = r"""
function doFilter(){
  const q=(document.getElementById('q')||{value:''}).value.toLowerCase().trim();
  const list=document.querySelectorAll('[data-title]');
  let vis=0;
  list.forEach(li=>{
    const hay=(li.getAttribute('data-title')+' '+li.getAttribute('data-path')+' '+li.textContent).toLowerCase();
    const ok=!q || hay.includes(q);
    li.style.display= ok? '':'none';
    if(ok) vis++;
  });
  const c=document.getElementById('count');
  if(c) c.textContent = vis + ' / ' + list.length;
}
document.addEventListener('DOMContentLoaded',()=>{
  const iq=document.getElementById('q');
  if(iq) iq.addEventListener('input', doFilter);
  doFilter();
});
"""

GRAPH_JS = r"""
async function initGraph(){
  const container=document.getElementById('graph');
  if(!container) return;
  let data;
  try{ const r=await fetch('graph.json'); data=await r.json(); }catch(e){ container.textContent='graph.json не загружен: '+e; return; }
  const nodes=data.nodes||[], links=data.links||[];
  if(!nodes.length){ container.textContent='Vault пуст — нет узлов'; return; }
  // Попытка D3 если доступен, иначе Canvas fallback
  if(window.d3){
    drawD3(container, nodes, links);
    return;
  }
  drawCanvas(container, nodes, links);
}
function drawCanvas(container, nodes, links){
  const w=container.clientWidth||900, h=container.clientHeight||560;
  const canvas=document.createElement('canvas');
  canvas.width=w; canvas.height=h;
  container.appendChild(canvas);
  const ctx=canvas.getContext('2d');
  // простая force 60 итераций (как в GraphView)
  const cx=w/2, cy=h/2;
  nodes.forEach((n,i)=>{ const ang=2*Math.PI*i/Math.max(1,nodes.length); const r=180+Math.random()*80; n.x=cx+r*Math.cos(ang); n.y=cy+r*Math.sin(ang); n.vx=0; n.vy=0; });
  const id2idx={}; nodes.forEach((n,i)=> id2idx[n.id]=i);
  const edges=links.map(l=>[id2idx[l.source], id2idx[l.target]]).filter(a=> a[0]!=null && a[1]!=null);
  for(let iter=0; iter<60; iter++){
    for(let i=0;i<nodes.length;i++) for(let j=i+1;j<nodes.length;j++){ let dx=nodes[i].x-nodes[j].x, dy=nodes[i].y-nodes[j].y; let d2=dx*dx+dy*dy+0.01, f=2200/d2; if(f>18) f=18; let d=Math.sqrt(d2); let fx=f*dx/d, fy=f*dy/d; nodes[i].vx+=fx; nodes[i].vy+=fy; nodes[j].vx-=fx; nodes[j].vy-=fy; }
    edges.forEach(([a,b])=>{ let dx=nodes[b].x-nodes[a].x, dy=nodes[b].y-nodes[a].y; let d=Math.sqrt(dx*dx+dy*dy)+0.01; let f=0.045*(d-110); let fx=f*dx/d, fy=f*dy/d; nodes[a].vx+=fx; nodes[a].vy+=fy; nodes[b].vx-=fx; nodes[b].vy-=fy; });
    nodes.forEach(n=>{ n.vx+=(cx-n.x)*0.012; n.vy+=(cy-n.y)*0.012; n.vx*=0.82; n.vy*=0.82; n.x+=n.vx; n.y+=n.vy; });
  }
  function frame(){
    ctx.clearRect(0,0,w,h);
    ctx.strokeStyle='rgba(100,120,160,0.18)'; ctx.lineWidth=1;
    edges.forEach(([a,b])=>{ ctx.beginPath(); ctx.moveTo(nodes[a].x,nodes[a].y); ctx.lineTo(nodes[b].x,nodes[b].y); ctx.stroke(); });
    nodes.forEach(n=>{
      ctx.beginPath(); ctx.arc(n.x,n.y,7,0,Math.PI*2); ctx.fillStyle='rgba(120,140,190,0.9)'; ctx.fill(); ctx.strokeStyle='rgba(10,18,36,0.9)'; ctx.stroke();
      ctx.fillStyle='rgba(210,218,232,0.95)'; ctx.font='11px Inter, sans-serif'; const lab=(n.title||n.id).slice(0,22); const tw=ctx.measureText(lab).width; ctx.fillText(lab, Math.max(2,Math.min(w-tw-2, n.x-tw/2)), n.y+18);
    });
  }
  frame();
  // клик -> переход
  canvas.addEventListener('click', e=>{
    const rect=canvas.getBoundingClientRect(); const x=e.clientX-rect.left, y=e.clientY-rect.top;
    let best=null, bestD=144;
    nodes.forEach(n=>{ const dx=x-n.x, dy=y-n.y, d2=dx*dx+dy*dy; if(d2<bestD){ bestD=d2; best=n; } });
    if(best && best.href) window.location.href=best.href;
  });
}
function drawD3(container, nodes, links){
  const w=container.clientWidth||900, h=560;
  const svg=d3.select(container).append('svg').attr('viewBox',[0,0,w,h]).attr('width',w).attr('height',h);
  const g=svg.append('g');
  svg.call(d3.zoom().on('zoom', e=> g.attr('transform', e.transform)));
  const sim=d3.forceSimulation(nodes).force('link', d3.forceLink(links).id(d=>d.id).distance(110)).force('charge', d3.forceManyBody().strength(-220)).force('center', d3.forceCenter(w/2,h/2)).force('collide', d3.forceCollide(28));
  const link=g.append('g').attr('stroke','rgba(100,120,160,0.22)').attr('stroke-width',1).selectAll('line').data(links).join('line');
  const node=g.append('g').selectAll('g').data(nodes).join('g').call(d3.drag().on('start', (e,d)=>{ if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }).on('drag', (e,d)=>{ d.fx=e.x; d.fy=e.y; }).on('end', (e,d)=>{ if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));
  node.append('circle').attr('r',7).attr('fill','rgba(120,140,190,0.92)').attr('stroke','rgba(10,18,36,0.9)');
  node.append('text').text(d=>(d.title||d.id).slice(0,18)).attr('x',10).attr('y',4).attr('font-size','11px').attr('fill','rgba(210,218,232,0.95)');
  node.on('click', (e,d)=>{ if(d.href) window.location.href=d.href; });
  sim.on('tick', ()=>{ link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y); node.attr('transform', d=>`translate(${d.x},${d.y})`); });
}
document.addEventListener('DOMContentLoaded', initGraph);
"""

# ── Утилиты ──────────────────────────────────────────────────────────

def _vault_root_from_settings(settings: dict | None) -> Path | None:
    if not settings:
        return None
    vr = settings.get("vault_root")
    if not vr:
        return None
    try:
        return Path(str(vr)).expanduser().resolve()
    except Exception:
        return Path(str(vr))


def _is_within(child: Path, parent: Path) -> bool:
    """True если child внутри parent (включая равенство)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        try:
            # fallback строковый префикс
            return str(child.resolve()).startswith(str(parent.resolve()).rstrip("/") + "/") or str(child.resolve()) == str(parent.resolve())
        except Exception:
            return False


def _collect_markdown_files(vault_root: Path, exclude_dir: Path | None = None) -> list[Path]:
    """Собрать все .md вне HEAVY_DIRS и скрытых папок, отсортировано."""
    out: list[Path] = []
    stack = [vault_root]
    while stack:
        cur = stack.pop()
        # пропускаем exclude_dir целиком
        if exclude_dir is not None and _is_within(cur, exclude_dir):
            continue
        try:
            entries = sorted(os.scandir(cur), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            continue
        pending: list[Path] = []
        for e in entries:
            if e.name.startswith(".") or e.name in HEAVY_DIRS:
                continue
            p = Path(e.path)
            if exclude_dir is not None and _is_within(p, exclude_dir):
                continue
            if e.is_dir(follow_symlinks=False):
                pending.append(p)
            elif e.is_file(follow_symlinks=False) and p.suffix.lower() == ".md":
                out.append(p)
        for d in reversed(pending):
            stack.append(d)
    out.sort(key=lambda p: p.as_posix().lower())
    return out


def _collect_assets(vault_root: Path, exclude_dir: Path | None = None) -> list[Path]:
    """Скопировать ассеты (изображения и т.п.) — рекурсивно вне HEAVY_DIRS."""
    out: list[Path] = []
    stack = [vault_root]
    while stack:
        cur = stack.pop()
        if exclude_dir is not None and _is_within(cur, exclude_dir):
            continue
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name.lower())
        except OSError:
            continue
        pending: list[Path] = []
        for e in entries:
            if e.name.startswith(".") or e.name in HEAVY_DIRS:
                continue
            p = Path(e.path)
            if exclude_dir is not None and _is_within(p, exclude_dir):
                continue
            if e.is_dir(follow_symlinks=False):
                pending.append(p)
            elif e.is_file(follow_symlinks=False) and p.suffix.lower() in ASSET_EXTS:
                out.append(p)
        for d in reversed(pending):
            stack.append(d)
    return out


def _parse_frontmatter_title(text: str, path: Path) -> tuple[dict, str, str]:
    """Возвращает (frontmatter dict, body, title)."""
    # переиспользуем vault.parse_frontmatter если доступен, иначе лёгкий парс
    try:
        from fragilenotes.vault import parse_frontmatter as _pf  # type: ignore

        fm, body = _pf(text)
    except Exception:
        fm, body = {}, text
        m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", text, re.DOTALL)
        if m:
            try:
                import yaml  # type: ignore

                data = yaml.safe_load(m.group(1))
                if isinstance(data, dict):
                    fm = data
                body = text[m.end():]
            except Exception:
                body = text[m.end():] if m else text
    title = ""
    if isinstance(fm, dict):
        t = fm.get("title")
        if t:
            title = str(t).strip()
    if not title:
        # пробуем первый H1
        for line in body.split("\n"):
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                break
    if not title:
        title = path.stem
    return fm, body, title


def _relative_href(from_html: Path, to_html: Path) -> str:
    """Относительный href от from_html к to_html (POSIX, для file://)."""
    try:
        rel = os.path.relpath(str(to_html), start=str(from_html.parent))
        # нормализуем в POSIX
        return Path(rel).as_posix()
    except Exception:
        return to_html.name


# ── Markdown -> HTML ─────────────────────────────────────────────────

def _escape(s: str) -> str:
    return html.escape(s, quote=True)


def _naive_markdown_to_html(text: str, wikilink_map: dict[str, Path], cur_html: Path | None, site_root: Path | None) -> str:
    """Наивный markdown -> HTML с поддержкой wikilink ссылок."""

    # wikilink -> <a>
    def _wlink_sub(m: re.Match[str]) -> str:
        target = (m.group(1) or "").strip()
        alias = (m.group(2) or "").strip() or target
        low = target.lower()
        dest = wikilink_map.get(low)
        if dest is not None and cur_html is not None and site_root is not None:
            # dest — абсолютный Path html в site
            href = _relative_href(cur_html, dest)
            return f'<a href="{_escape(href)}">{_escape(alias)}</a>'
        if dest is not None:
            # fallback: имя файла html
            href = dest.name
            return f'<a href="{_escape(href)}">{_escape(alias)}</a>'
        return _escape(alias)

    # применяем до общего парсинга, чтобы не ломать разметку
    # но сохраняем как html-якоря: временно заменяем на плейсхолдер без экранирования
    # Используем прямой проход внутрь _inline позже
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    in_list = False
    list_tag = "ul"

    def _flush_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{list_tag}>")
            in_list = False

    def _inline(s: str) -> str:
        # сначала экранируем, потом применяем inline
        # но wikilinks обрабатываем отдельно с html-ссылками
        # разбиваем по wikilink
        parts: list[str] = []
        last = 0
        for m in WIKILINK_RE.finditer(s):
            pre = s[last:m.start()]
            if pre:
                parts.append(_inline_escape(pre))
            # wikilink с картой
            target = (m.group(1) or "").strip()
            alias = (m.group(2) or "").strip() or target
            low = target.lower()
            dest = wikilink_map.get(low)
            if dest is not None and cur_html is not None and site_root is not None:
                href = _relative_href(cur_html, dest)
                parts.append(f'<a href="{_escape(href)}">{_escape(alias)}</a>')
            elif dest is not None:
                parts.append(f'<a href="{_escape(dest.name)}">{_escape(alias)}</a>')
            else:
                parts.append(_escape(alias))
            last = m.end()
        tail = s[last:]
        if tail:
            parts.append(_inline_escape(tail))
        return "".join(parts)

    def _inline_escape(s: str) -> str:
        # для фрагментов без wikilink
        s = _escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__(.+?)__", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"~~(.+?)~~", r"<del>\1</del>", s)
        s = re.sub(r"==(.+?)==", r"<mark>\1</mark>", s)
        s = re.sub(r"`([^`]+?)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        s = TAG_RE.sub(r'<span class="tag">#\1</span>', s)
        return s

    for line in lines:
        stripped = line.strip()
        if line.strip().startswith("```"):
            if not in_code:
                _flush_list()
                in_code = True
                code_buf = []
            else:
                in_code = False
                code_text = "\n".join(code_buf)
                out.append(f"<pre><code>{_escape(code_text)}</code></pre>")
                code_buf = []
            continue
        if in_code:
            code_buf.append(line)
            continue
        if not stripped:
            _flush_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            _flush_list()
            level = len(m.group(1))
            title = _inline(m.group(2).strip())
            out.append(f"<h{level}>{title}</h{level}>")
            continue
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            _flush_list()
            out.append("<hr/>")
            continue
        m = re.match(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*)$", line)
        if m:
            _flush_list()
            checked = m.group(1).lower() == "x"
            txt = _inline(m.group(2))
            box = "☑" if checked else "☐"
            out.append(f"<p>{box} {txt}</p>")
            continue
        m = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            bullet = m.group(1)
            txt = _inline(m.group(2))
            cur_tag = "ol" if re.match(r"\d+", bullet) else "ul"
            if not in_list or list_tag != cur_tag:
                _flush_list()
                out.append(f"<{cur_tag}>")
                in_list = True
                list_tag = cur_tag
            out.append(f"<li>{txt}</li>")
            continue
        if stripped.startswith(">"):
            _flush_list()
            q = stripped[1:].strip()
            out.append(f"<blockquote><p>{_inline(q)}</p></blockquote>")
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            _flush_list()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-:") and c for c in cells):
                continue
            row_html = "".join(f"<td>{_inline(c)}</td>" for c in cells)
            out.append(f"<table><tr>{row_html}</tr></table>")
            continue
        _flush_list()
        out.append(f"<p>{_inline(stripped)}</p>")

    _flush_list()
    if in_code and code_buf:
        out.append(f"<pre><code>{_escape(chr(10).join(code_buf))}</code></pre>")
    return "\n".join(out)


def markdown_to_html(md_text: str, wikilink_map: dict[str, Path] | None = None, cur_html: Path | None = None, site_root: Path | None = None) -> str:
    """Конвертирует markdown body в HTML.

    Пытается использовать ``markdown`` или ``markdown_it`` если установлены,
    иначе падает на наивный парсер. Wikilinks резолвятся через wikilink_map.
    """
    wm = wikilink_map or {}

    # Попытка 1: python-markdown (с сохранением wikilink ссылок)
    # Делаем препроцесс wikilink -> html <a> перед markdown рендером, чтобы не экранировалось
    def _pre_link(text: str) -> str:
        def _sub(m: re.Match[str]) -> str:
            target = (m.group(1) or "").strip()
            alias = (m.group(2) or "").strip() or target
            low = target.lower()
            dest = wm.get(low)
            if dest is not None and cur_html is not None and site_root is not None:
                href = _relative_href(cur_html, dest)
                return f"[{alias}]({href})"
            if dest is not None:
                return f"[{alias}]({dest.name})"
            return alias
        return WIKILINK_RE.sub(_sub, text)

    cleaned_for_md = _pre_link(md_text)

    try:
        import markdown as _md  # type: ignore[import-not-found]

        html_body = _md.markdown(
            cleaned_for_md,
            extensions=["extra", "codehilite", "tables", "fenced_code", "sane_lists"],
        )
        # теги #tag -> span
        html_body = TAG_RE.sub(r'<span class="tag">#\1</span>', html_body)
        return html_body
    except Exception:
        pass

    try:
        from markdown_it import MarkdownIt as _MI  # type: ignore[import-not-found]  # noqa: N814

        mi = _MI("commonmark", {"html": False, "linkify": True, "typographer": False})
        try:
            mi.enable("table")
        except Exception:
            pass
        html_body = mi.render(cleaned_for_md)
        html_body = TAG_RE.sub(r'<span class="tag">#\1</span>', html_body)
        return html_body
    except Exception:
        pass

    return _naive_markdown_to_html(md_text, wm, cur_html, site_root)


# ── Генерация сайта ───────────────────────────────────────────────────

def _build_html_document(title: str, body_html: str, rel_root: str = ".", extra_head: str = "") -> str:
    safe = _escape(title)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{safe} — FragileNotes</title>
<link rel="stylesheet" href="{rel_root}/style.css"/>
{extra_head}
</head>
<body>
<header>
  <a class="brand" href="{rel_root}/index.html">🧬 FragileNotes</a>
  <nav style="display:flex; gap:10px; font-size:0.92em">
    <a href="{rel_root}/index.html">Индекс</a>
    <a href="{rel_root}/graph.html">Граф</a>
  </nav>
  <span class="meta">publish · {datetime.now().strftime("%Y-%m-%d %H:%M")}</span>
</header>
<div class="container">
<div class="content">
<h1>{safe}</h1>
{body_html}
</div>
<div class="footer">Сгенерировано FragileNotes Publish · <a href="{rel_root}/graph.json">graph.json</a></div>
</div>
</body>
</html>
"""


def _write_style(output_dir: Path) -> None:
    (output_dir / "style.css").write_text(SITE_CSS, encoding="utf-8")


def _write_graph_assets(output_dir: Path) -> None:
    # graph.json генерится отдельно; здесь только проверка наличия
    pass


def build_graph_data(vault_root: Path, notes_info: list[dict[str, Any]], wikilink_map: dict[str, Path]) -> dict[str, Any]:
    """Собрать nodes/links из wikilinks где цель существует."""
    # nodes
    nodes: list[dict[str, Any]] = []
    path_to_idx: dict[str, int] = {}
    for idx, info in enumerate(notes_info):
        rel_html = info["rel_html"]  # Path relative to site root with .html
        nodes.append({
            "id": info["stem"],
            "title": info["title"],
            "path": info["rel"].as_posix(),
            "href": rel_html.as_posix(),
            "group": info["rel"].parent.as_posix() if str(info["rel"].parent) != "." else "",
            "mtime": info["mtime"],
        })
        # ключи для резолва: абсолютный путь md, stem lower, title lower, rel
        path_to_idx[str(info["path"])] = idx
        # также маппим по нормализованным ключам — найдём по wikilink_map dest
        # wikilink_map уже указывает на html Path, свяжем через stem
        # для edges нам нужен именно этот idx

    # быстрый lookup stem/title -> idx
    stem_to_idx: dict[str, int] = {}
    title_to_idx: dict[str, int] = {}
    for idx, info in enumerate(notes_info):
        stem_to_idx.setdefault(info["stem"].lower(), idx)
        title_to_idx.setdefault(info["title"].lower(), idx)

    links: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for info in notes_info:
        src_idx = path_to_idx[str(info["path"])]
        raw = info.get("raw", "") or ""
        for m in WIKILINK_RE.finditer(raw):
            target = (m.group(1) or "").strip()
            if not target:
                continue
            low = target.lower()
            dst_idx = stem_to_idx.get(low)
            if dst_idx is None:
                dst_idx = title_to_idx.get(low)
            if dst_idx is None:
                continue
            if dst_idx == src_idx:
                continue
            key = (src_idx, dst_idx)
            if key in seen:
                continue
            seen.add(key)
            # для D3 нужны id source/target = stem
            links.append({"source": nodes[src_idx]["id"], "target": nodes[dst_idx]["id"]})

    return {"nodes": nodes, "links": links, "meta": {"count": len(nodes), "edges": len(links)}}


def generate_index_html(notes_info: list[dict[str, Any]], output_dir: Path, vault_root: Path) -> None:
    """Создаёт index.html с фильтром и списком заметок."""
    # сортируем по mtime desc
    sorted_notes = sorted(notes_info, key=lambda x: x["mtime"], reverse=True)
    items: list[str] = []
    for info in sorted_notes:
        href = info["rel_html"].as_posix()
        title = _escape(info["title"])
        rel = _escape(info["rel"].as_posix())
        when = ""
        try:
            when = datetime.fromtimestamp(info["mtime"]).strftime("%Y-%m-%d")
        except Exception:
            when = ""
        preview = _escape(info.get("preview", "")[:140])
        items.append(
            f'<li data-title="{_escape(info["title"])}" data-path="{_escape(info["rel"].as_posix())}">'
            f'<a href="{_escape(href)}" style="flex:1; min-width:0">'
            f'<strong>{title}</strong><br/><span style="color:var(--faint); font-size:0.82em">{rel}</span>'
            f'<div style="color:var(--muted); font-size:0.86em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis">{preview}</div>'
            f"</a>"
            f'<span class="when">{when}</span>'
            f"</li>"
        )
    list_html = "\n".join(items) if items else '<li style="color:var(--faint); padding:12px">Vault пуст — нет заметок</li>'

    body = f"""
<div class="grid">
  <aside class="sidebar">
    <h3>Поиск</h3>
    <input id="q" class="search" placeholder="фильтр по имени, пути, содержимому…" autocomplete="off"/>
    <div id="count" class="count">{len(sorted_notes)} заметок</div>
    <h3 style="margin-top:14px">Навигация</h3>
    <a href="graph.html">🕸 Граф</a>
    <a href="graph.json">graph.json</a>
    <div style="margin-top:10px; color:var(--faint); font-size:0.82em">Vault: {_escape(str(vault_root))}<br/>{len(sorted_notes)} md файлов</div>
  </aside>
  <div class="content" style="padding:12px">
    <h2 style="margin-top:0">Индекс — {len(sorted_notes)} заметок</h2>
    <ul class="index-list">
      {list_html}
    </ul>
  </div>
</div>
<script>{INDEX_JS}</script>
"""
    html_doc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Индекс — FragileNotes</title>
<link rel="stylesheet" href="style.css"/>
</head>
<body>
<header>
  <a class="brand" href="index.html">🧬 FragileNotes</a>
  <nav style="display:flex; gap:10px; font-size:0.92em">
    <a href="index.html">Индекс</a>
    <a href="graph.html">Граф</a>
  </nav>
  <span class="meta">publish · {datetime.now().strftime("%Y-%m-%d %H:%M")} · {len(sorted_notes)} заметок</span>
</header>
<div class="container">
{body}
<div class="footer">Сгенерировано FragileNotes Publish</div>
</div>
</body>
</html>
"""
    (output_dir / "index.html").write_text(html_doc, encoding="utf-8")


def generate_graph_html(output_dir: Path) -> None:
    """graph.html — визуализация force-графа (D3 если доступен, иначе Canvas)."""
    body = r"""
<div class="content" style="margin-bottom:12px">
  <h1 style="margin-top:0">🕸 Граф связей</h1>
  <p style="color:var(--muted)">Узлы — файлы, рёбра — [[wikilink]]. Клик по узлу — открыть заметку. Масштабирование — колесо / pinch.</p>
  <p style="color:var(--faint); font-size:0.85em">D3 загружается с CDN; если оффлайн — используется Canvas fallback (60 итераций, как в приложении).</p>
</div>
<div id="graph" class="graph-wrap"></div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>""" + GRAPH_JS + r"""</script>
"""
    html_doc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Граф — FragileNotes</title>
<link rel="stylesheet" href="style.css"/>
</head>
<body>
<header>
  <a class="brand" href="index.html">🧬 FragileNotes</a>
  <nav style="display:flex; gap:10px; font-size:0.92em">
    <a href="index.html">Индекс</a>
    <a href="graph.html">Граф</a>
  </nav>
  <span class="meta">граф · {datetime.now().strftime("%Y-%m-%d %H:%M")}</span>
</header>
<div class="container">
{body}
<div class="footer"><a href="graph.json">graph.json</a> · сгенерировано FragileNotes Publish</div>
</div>
</body>
</html>
"""
    (output_dir / "graph.html").write_text(html_doc, encoding="utf-8")


def build_site(
    vault_root: Path | str,
    output_dir: Path | str | None = None,
    settings: dict | None = None,
    clean: bool = True,
) -> dict[str, Any]:
    """Собрать статический сайт из vault.

    Args:
        vault_root: путь к vault (или берётся из settings).
        output_dir: выходная директория; по умолчанию ``vault_root / "public"``.
        settings: опционально словарь настроек (для резолва vault_root/publish_dir).
        clean: если True — очистить output_dir перед сборкой.

    Returns:
        dict с ключами ``output_dir``, ``count``, ``nodes``, ``edges``, ``files``.
    """
    # резолв vault_root
    if vault_root is None and settings is not None:
        vr = _vault_root_from_settings(settings)
        if vr is not None:
            vault_root = vr
    if vault_root is None:
        raise ValueError("vault_root не указан и не найден в settings")
    vault_root = Path(str(vault_root)).expanduser().resolve()
    if not vault_root.is_dir():
        raise FileNotFoundError(f"vault не найден: {vault_root}")

    # output_dir
    if output_dir is None:
        if settings is not None and settings.get("publish_dir"):
            output_dir = Path(str(settings["publish_dir"])).expanduser()
            if not output_dir.is_absolute():
                output_dir = (vault_root / output_dir).resolve()
        else:
            # пробуем publish_output / public / _site
            for k in ("publish_output", "publish_dir", "output_dir"):
                if settings is not None and settings.get(k):
                    cand = Path(str(settings[k])).expanduser()
                    if not cand.is_absolute():
                        cand = (vault_root / cand).resolve()
                    output_dir = cand
                    break
            else:
                output_dir = vault_root / "public"
    output_dir = Path(str(output_dir)).expanduser().resolve()

    if clean and output_dir.exists():
        # безопасность: не удаляем vault_root случайно — только если output внутри vault или отдельный
        try:
            # если output == vault_root — ошибка
            if output_dir.resolve() == vault_root.resolve():
                raise ValueError(f"output_dir совпадает с vault_root: {output_dir}")
            shutil.rmtree(output_dir)
        except ValueError:
            raise
        except Exception:
            # если не удалось — просто очистим содержимое
            for child in output_dir.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except Exception:
                    pass
    output_dir.mkdir(parents=True, exist_ok=True)

    # собрать md файлы (исключая output_dir если он внутри vault)
    md_files = _collect_markdown_files(vault_root, exclude_dir=output_dir)

    # подготовить notes_info + wikilink_map (html-пути)
    notes_info: list[dict[str, Any]] = []
    # первый проход — собрать мета без конвертации
    for p in md_files:
        try:
            raw_full = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body, title = _parse_frontmatter_title(raw_full, p)
        rel = p.relative_to(vault_root)
        rel_html = rel.with_suffix(".html")
        # preview: первые 200 символов тела без frontmatter
        preview = " ".join(body.strip().split())[:200]
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        notes_info.append({
            "path": p,
            "rel": rel,
            "rel_html": rel_html,
            "title": title,
            "stem": p.stem,
            "fm": fm,
            "body": body,
            "raw": raw_full,
            "preview": preview,
            "mtime": mtime,
        })

    # wikilink_map: lower stem/title -> html абсолютный Path в output
    wikilink_map: dict[str, Path] = {}
    # stem -> html path (output absolute) — первый выигрывает
    for info in notes_info:
        low_stem = info["stem"].lower()
        low_title = str(info["title"] or "").lower()
        html_abs = output_dir / info["rel_html"]
        wikilink_map.setdefault(low_stem, html_abs)
        if low_title and low_title != low_stem:
            wikilink_map.setdefault(low_title, html_abs)
        # также полный относительный без расширения? для совместимости
        # например "folder/note" -> href
        try:
            no_suffix = info["rel"].with_suffix("").as_posix().lower()
            wikilink_map.setdefault(no_suffix, html_abs)
        except Exception:
            pass

    # второй проход — рендер каждого md -> html
    written: list[Path] = []
    for info in notes_info:
        cur_html_abs = output_dir / info["rel_html"]
        # относительный корень для ссылки на style.css / index.html
        try:
            depth = len(info["rel_html"].parent.parts) if str(info["rel_html"].parent) != "." else 0
            rel_root = "/".join([".."] * depth) if depth else "."
        except Exception:
            rel_root = "."
        body_html = markdown_to_html(info["body"], wikilink_map, cur_html_abs, output_dir)
        # добавим бэклинки секцию? опционально
        doc = _build_html_document(info["title"], body_html, rel_root=rel_root)
        cur_html_abs.parent.mkdir(parents=True, exist_ok=True)
        cur_html_abs.write_text(doc, encoding="utf-8")
        written.append(cur_html_abs)

    # статика
    _write_style(output_dir)

    # индекс
    generate_index_html(notes_info, output_dir, vault_root)

    # граф
    graph_data = build_graph_data(vault_root, notes_info, wikilink_map)
    (output_dir / "graph.json").write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    generate_graph_html(output_dir)

    # ассеты (изображения) — исключая output_dir
    assets = _collect_assets(vault_root, exclude_dir=output_dir)
    copied = 0
    for src in assets:
        # не копируем то что уже является md-generated? активы имеют другие расширения, так что нет коллизии
        rel = src.relative_to(vault_root)
        dst = output_dir / rel
        # не перезаписываем уже сгенерированные html
        if dst.suffix.lower() == ".html":
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except OSError:
            continue

    return {
        "output_dir": output_dir,
        "count": len(notes_info),
        "nodes": len(graph_data.get("nodes", [])),
        "edges": len(graph_data.get("links", [])),
        "files": written,
        "assets": copied,
    }


def publish_vault(
    vault_root: Path | str | None = None,
    output_dir: Path | str | None = None,
    settings: dict | None = None,
    clean: bool = True,
) -> dict[str, Any]:
    """Публичный API: экспорт vault в статический сайт (алиас build_site).

    Поддерживает вызовы:
      publish_vault("/path/to/vault", "/tmp/site")
      publish_vault(settings=my_settings)
      publish_vault(vault_root=..., output_dir=..., settings=...)
    """
    # если передан settings без vault_root — берём из настроек
    if vault_root is None and settings is not None:
        vault_root = _vault_root_from_settings(settings)
    # если оба None — пробуем загрузить settings из config
    if vault_root is None and settings is None:
        try:
            from fragilenotes.config import load_settings as _load  # type: ignore

            settings = _load()
            vault_root = _vault_root_from_settings(settings)
        except Exception:
            pass
    return build_site(vault_root, output_dir, settings=settings, clean=clean)


# ── CLI ───────────────────────────────────────────────────────────────

def _resolve_cli_paths(args: argparse.Namespace, settings: dict | None) -> tuple[Path, Path]:
    # vault
    vault_arg = getattr(args, "vault", None)
    if vault_arg:
        vault_root = Path(str(vault_arg)).expanduser().resolve()
    elif settings is not None:
        vr = _vault_root_from_settings(settings)
        vault_root = vr if vr is not None else Path.cwd()
    else:
        try:
            from fragilenotes.config import load_settings as _load  # type: ignore

            s = _load()
            vr = _vault_root_from_settings(s)
            vault_root = vr if vr is not None else Path.cwd()
            settings = s
        except Exception:
            vault_root = Path.cwd()

    # output
    out_arg = getattr(args, "output", None)
    if out_arg:
        output_dir = Path(str(out_arg)).expanduser().resolve()
    elif settings is not None and settings.get("publish_dir"):
        cand = Path(str(settings["publish_dir"])).expanduser()
        output_dir = cand if cand.is_absolute() else (vault_root / cand).resolve()
    elif settings is not None and settings.get("publish_output"):
        cand = Path(str(settings["publish_output"])).expanduser()
        output_dir = cand if cand.is_absolute() else (vault_root / cand).resolve()
    else:
        output_dir = vault_root / "public"

    return vault_root, output_dir


def main(argv: list[str] | None = None) -> int:
    """Точка входа ``fragile publish``.

    Поддерживает:
      fragile publish [--vault PATH] [--output PATH]
      python -m fragilenotes.core.publish publish [...]
      python -m fragilenotes.core.publish [--vault PATH] [...]
    """
    if argv is None:
        argv = sys.argv[1:]

    # если вызвано как `fragile publish` через entry_point fragile -> argv = ["publish", ...]
    # если вызвано как `fragile` без subcommand — считаем publish
    # для совместимости пробуем распарсить subparsers, но также поддержим прямые флаги
    parser = argparse.ArgumentParser(
        prog="fragile",
        description="FragileNotes — экспорт vault в статический сайт",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="примеры:\n  fragile publish\n  fragile publish --vault ~/notes --output /tmp/site\n  python -m fragilenotes.core.publish --vault ./vault --output ./public",
    )
    sub = parser.add_subparsers(dest="cmd")
    pub = sub.add_parser("publish", help="экспорт vault в статический сайт")

    # флаги для publish (добавляем и на верхний уровень для удобства `python -m ... --vault`)
    for p in (parser, pub):
        p.add_argument("--vault", dest="vault", default=None, help="путь к vault (по умолчанию из settings.json)")
        p.add_argument("--output", dest="output", default=None, help="выходная директория (по умолчанию vault/public)")
        p.add_argument("--force", action="store_true", help="перезаписать output если существует (по умолчанию чистит)")
        p.add_argument("--no-clean", action="store_true", help="не очищать output перед сборкой")

    # также алиас `fragile` без подкоманды
    args, unknown = parser.parse_known_args(argv)

    # если unknown содержит publish как позиционный? argparse уже его съел как cmd
    cmd = getattr(args, "cmd", None)
    # если пользователь вызвал `fragile --vault X` без subcommand — считаем publish
    if cmd is None:
        # если есть любые флаги — это publish
        # если пусто — тоже publish с дефолтами
        cmd = "publish"

    if cmd != "publish":
        parser.print_help()
        return 1

    # загрузить settings для резолва путей по умолчанию
    settings: dict | None = None
    try:
        from fragilenotes.config import load_settings as _load  # type: ignore

        settings = _load()
    except Exception:
        settings = None

    vault_root, output_dir = _resolve_cli_paths(args, settings)

    # защита: vault должен существовать
    if not vault_root.is_dir():
        print(f"ошибка: vault не найден: {vault_root}", file=sys.stderr)
        return 2

    clean = not bool(getattr(args, "no_clean", False))
    # --force игнорируется — мы всегда чистим если clean=True; оставили для совместимости

    print(f"vault:  {vault_root}")
    print(f"output: {output_dir}")
    print("сборка сайта…")
    try:
        result = build_site(vault_root, output_dir, settings=settings, clean=clean)
    except Exception as exc:
        print(f"ошибка сборки: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1

    print(f"готово: {result['count']} заметок, {result['nodes']} узлов, {result['edges']} рёбер")
    print(f"индекс: {output_dir / 'index.html'}")
    print(f"граф:   {output_dir / 'graph.html'}  ({output_dir / 'graph.json'})")
    if result.get("assets"):
        print(f"ассеты: {result['assets']} файлов")
    print(f"открой: file://{output_dir / 'index.html'}")
    return 0


# алиасы для совместимости и тестов
publish = publish_vault
generate_site = build_site

if __name__ == "__main__":
    raise SystemExit(main())

__all__ = [
    "build_site",
    "publish_vault",
    "publish",
    "generate_site",
    "markdown_to_html",
    "build_graph_data",
    "generate_index_html",
    "generate_graph_html",
    "main",
]
