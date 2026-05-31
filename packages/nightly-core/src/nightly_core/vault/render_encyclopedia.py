"""Encyclopedia renderer — markdown vault → per-node static HTML pages.

Walks the SQLite index, reads each node's markdown body, renders to HTML
with `markdown-it-py`, and emits `_site/<kind-dir>/<slug>.html`. Also
emits `_site/index.html` — a list view grouped by kind with status pills.

Wiki-links `[[node_id]]` in markdown bodies resolve to relative links
to the target node's page. Dangling targets render as plain text with a
`.dangling` class so the operator sees what didn't resolve.

The renderer reads SQLite for the graph (edges, backlinks, node metadata)
and markdown for the prose. Both must already exist — call
`index.rebuild()` before `render()`.
"""

from __future__ import annotations

import html
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

from .index import INDEX_DB_NAME
from .model import NODE_KINDS, node_kind_dir

__all__ = ["RenderResult", "render"]


_MD = MarkdownIt("commonmark", {"html": False, "breaks": False})
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_ASSETS_SRC = Path(__file__).parent / "assets"


@dataclass(frozen=True)
class RenderResult:
    """Outcome of one `render()` call."""

    site_root: Path
    pages_written: int
    index_path: Path


def render(vault_root: Path) -> RenderResult:
    """Render the entire vault encyclopedia under `<vault_root>/_site/`.

    Requires the SQLite index to exist; raises FileNotFoundError if not.
    """
    db_path = vault_root / INDEX_DB_NAME
    if not db_path.exists():
        msg = f"index DB not found; run index.rebuild() first: {db_path}"
        raise FileNotFoundError(msg)

    site_root = vault_root / "_site"
    site_root.mkdir(parents=True, exist_ok=True)
    _copy_assets(site_root)

    conn = sqlite3.connect(db_path)
    try:
        nodes = list(
            conn.execute(
                "SELECT id, kind, title, status, created, updated, body_path "
                "FROM nodes ORDER BY kind, created DESC"
            )
        )
        id_to_title = {row[0]: (row[2] or row[0]) for row in nodes}
        backlinks_by_dst = _build_backlinks_index(conn, id_to_title)

        pages_written = 0
        for node_id, kind, title, status, created, updated, body_path in nodes:
            body_md = _read_body(vault_root, body_path) if body_path else ""
            page_html = _render_page(
                node_id=node_id,
                kind=kind,
                title=title,
                status=status,
                created=created,
                updated=updated,
                body_md=body_md,
                id_to_title=id_to_title,
                backlinks=backlinks_by_dst.get(node_id, []),
            )
            page_path = _page_path(site_root, node_id, kind)
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(page_html, encoding="utf-8")
            pages_written += 1

        index_path = site_root / "index.html"
        index_path.write_text(_render_index(nodes), encoding="utf-8")

        return RenderResult(
            site_root=site_root,
            pages_written=pages_written,
            index_path=index_path,
        )
    finally:
        conn.close()


# ── internals ─────────────────────────────────────────────────────────────


def _copy_assets(site_root: Path) -> None:
    assets_dst = site_root / "assets"
    assets_dst.mkdir(exist_ok=True)
    if not _ASSETS_SRC.is_dir():
        return
    for src in _ASSETS_SRC.iterdir():
        if src.is_file():
            shutil.copy2(src, assets_dst / src.name)


def _build_backlinks_index(
    conn: sqlite3.Connection, id_to_title: dict[str, str]
) -> dict[str, list[tuple[str, str, str]]]:
    """Per-dst list of (src_id, edge_type, src_title) triples."""
    backlinks: dict[str, list[tuple[str, str, str]]] = {}
    for src_id, dst_id, edge_type in conn.execute(
        "SELECT src_id, dst_id, edge_type FROM edges ORDER BY edge_type, src_id"
    ):
        backlinks.setdefault(dst_id, []).append(
            (src_id, edge_type, id_to_title.get(src_id, src_id))
        )
    return backlinks


def _read_body(vault_root: Path, body_path: str) -> str:
    """Read the markdown body (everything after the closing `---`)."""
    full = (vault_root / body_path).read_text(encoding="utf-8")
    if not full.startswith("---\n"):
        return full
    end = full.find("\n---\n", 4)
    if end == -1:
        return full
    return full[end + 5 :]


def _resolve_wiki_links(body: str, id_to_title: dict[str, str]) -> str:
    """Replace resolved `[[id]]` with markdown links. Dangling targets are
    left verbatim and picked up by `_post_render_dangling()` after
    markdown rendering — avoids encoding sentinels that markdown-it might
    interpret (e.g. `__text__` becomes bold)."""

    def replace(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        if "/" not in target or target not in id_to_title:
            return m.group(0)
        target_kind, target_slug = target.split("/", 1)
        title = id_to_title.get(target, target)
        href = f"../{_kind_dir(target_kind)}/{target_slug}.html"
        return f"[{title}]({href})"

    return _WIKI_LINK_RE.sub(replace, body)


def _kind_dir(kind: str) -> str:
    if kind in NODE_KINDS:
        return node_kind_dir(kind)  # type: ignore[arg-type]
    # `unknown` and unrecognized kinds get their own dir; lets the renderer
    # link to placeholder rows without crashing.
    return kind


def _page_path(site_root: Path, node_id: str, kind: str) -> Path:
    slug = node_id.split("/", 1)[1] if "/" in node_id else node_id
    return site_root / _kind_dir(kind) / f"{slug}.html"


def _render_page(  # noqa: PLR0913 - keyword-only fields, all required
    *,
    node_id: str,
    kind: str,
    title: str | None,
    status: str | None,
    created: str | None,
    updated: str | None,
    body_md: str,
    id_to_title: dict[str, str],
    backlinks: list[tuple[str, str, str]],
) -> str:
    """Render one node page as HTML."""
    display_title = html.escape(title or node_id)
    eyebrow = html.escape(kind)
    body_with_links = _resolve_wiki_links(body_md, id_to_title)
    body_html = _MD.render(body_with_links)
    body_html = _post_render_dangling(body_html)

    meta_bits = []
    if status:
        meta_bits.append(
            f'<b>Status</b>&nbsp; <span class="pill {html.escape(status)}">{html.escape(status)}</span>'
        )
    if created:
        meta_bits.append(f"<b>Created</b>&nbsp; {html.escape(created)}")
    if updated and updated != created:
        meta_bits.append(f"<b>Updated</b>&nbsp; {html.escape(updated)}")
    meta_bits.append(f"<b>ID</b>&nbsp; <code>{html.escape(node_id)}</code>")
    meta_html = " &nbsp;·&nbsp; ".join(meta_bits)

    backlinks_html = _render_backlinks(node_id, kind, backlinks) if backlinks else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{display_title} — vault</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<main class="shell">
  <div class="eyebrow"><span class="dot kind-{eyebrow}"></span>{eyebrow}</div>
  <h1>{display_title}</h1>
  <div class="meta">{meta_html}</div>
  <div class="prose">{body_html}</div>
  {backlinks_html}
  <footer><a href="../index.html">← back to vault index</a></footer>
</main>
</body>
</html>
"""


def _post_render_dangling(html_body: str) -> str:
    """Wrap any remaining `[[id]]` patterns in the rendered HTML with a
    `.dangling` span. These are targets the indexer didn't know — the
    resolved ones were already turned into anchors in `_resolve_wiki_links()`."""
    return _WIKI_LINK_RE.sub(
        lambda m: f'<span class="dangling">[[{html.escape(m.group(1))}]]</span>',
        html_body,
    )


def _render_backlinks(dst_id: str, dst_kind: str, backlinks: list[tuple[str, str, str]]) -> str:
    by_type: dict[str, list[tuple[str, str]]] = {}
    for src_id, edge_type, src_title in backlinks:
        by_type.setdefault(edge_type, []).append((src_id, src_title))

    sections = []
    for edge_type in sorted(by_type):
        items = by_type[edge_type]
        lis = []
        for src_id, src_title in items:
            src_kind = src_id.split("/", 1)[0] if "/" in src_id else "unknown"
            src_slug = src_id.split("/", 1)[1] if "/" in src_id else src_id
            href = f"../{_kind_dir(src_kind)}/{src_slug}.html"
            lis.append(
                f'<li><span class="chip {html.escape(src_kind)}"><span class="dot"></span>{html.escape(src_kind)}</span> '
                f'<a href="{href}">{html.escape(src_title or src_id)}</a></li>'
            )
        sections.append(
            f'<div class="backref-group"><h3>{html.escape(edge_type)} ({len(items)})</h3>'
            f"<ul>{''.join(lis)}</ul></div>"
        )

    return (
        '<section class="backlinks">\n'
        "<h2>Referenced by</h2>\n" + "\n".join(sections) + "\n</section>"
    )


def _render_index(nodes: list[tuple]) -> str:
    """Render `_site/index.html` — list view grouped by kind."""
    by_kind: dict[str, list[tuple[str, str, str, str]]] = {}
    for node_id, kind, title, status, created, _updated, _body in nodes:
        by_kind.setdefault(kind, []).append((node_id, title, status, created))

    chips = " ".join(
        f'<span class="chip {kind}"><span class="dot"></span>{kind} ({len(rows)})</span>'
        for kind, rows in by_kind.items()
    )

    sections = []
    for kind in (*NODE_KINDS, "unknown"):
        rows = by_kind.get(kind, [])
        if not rows:
            continue
        lis = []
        for node_id, title, status, created in rows:
            slug = node_id.split("/", 1)[1] if "/" in node_id else node_id
            href = f"{_kind_dir(kind)}/{slug}.html"
            pill = (
                f'<span class="pill {html.escape(status)}">{html.escape(status)}</span>'
                if status
                else ""
            )
            date = f'<span class="node-date">{html.escape(created)}</span>' if created else ""
            lis.append(
                f'<li><span class="node-title">'
                f'<a href="{html.escape(href)}">{html.escape(title or node_id)}</a>'
                f"</span> {pill} {date}</li>"
            )
        sections.append(
            f'<section class="node-section">'
            f"<h2>{_kind_dir(kind) if kind in NODE_KINDS else kind} ({len(rows)})</h2>"
            f'<ul class="node-list">{"".join(lis)}</ul>'
            f"</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vault — index</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<main class="shell">
  <div class="eyebrow"><span class="dot"></span>vault · encyclopedia</div>
  <h1>Vault</h1>
  <div class="kinds-nav">{chips}</div>
  {"".join(sections)}
  <footer>nightly · vault encyclopedia · for the graph view, open <a href="../_dashboard/index.html">the dashboard</a></footer>
</main>
</body>
</html>
"""
