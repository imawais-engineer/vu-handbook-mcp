#!/usr/bin/env python3
"""VU Handbook MCP server.

Model Context Protocol server exposing the Virtual University of Pakistan
Student Handbook (https://handbook.vu.edu.pk/) as a scoped, citable knowledge
source for AI agents.

The handbook is crawled ahead of time by ``tools/crawl_handbook.py`` into:

    data/toc.json       nested table of contents (navigation)
    data/pages.jsonl    raw records (portable dump)
    data/handbook.db    SQLite + FTS5 full-text index (search + fetch)

Serving from the local snapshot keeps queries fast and deterministic and avoids
hitting the VU web server on every agent turn. Refresh with::

    python3 tools/crawl_handbook.py --refresh-toc

Run (stdio transport, e.g. for Claude Desktop / any MCP client)::

    python3 mcp_server.py

Run (Streamable HTTP transport, for production / remote agents)::

    python3 mcp_server.py --http --host 0.0.0.0 --port 8000 --allow-host vumcp.example.com
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from mcp.server.streamable_http import TransportSecuritySettings

SOURCE_NAME = "Virtual University of Pakistan — Student Handbook"
SOURCE_URL = "https://handbook.vu.edu.pk/"

DATA_DIR = Path(os.environ.get("VU_HANDBOOK_DATA", Path(__file__).resolve().parent / "data"))
DB_PATH = DATA_DIR / "handbook.db"
TOC_PATH = DATA_DIR / "toc.json"

LMS_SOURCE_NAME = "VU LMS Reverse Engineering Guide (vulms.vu.edu.pk)"
LMS_DIR = Path(os.environ.get("VU_LMS_DIR", Path(__file__).resolve().parent / "docs" / "lms"))

MAX_CONTENT_CHARS = 40_000
DEFAULT_SEARCH_LIMIT = 8
MAX_SEARCH_LIMIT = 25

# bm25 column weights: title > breadcrumb > headings > markdown
_BM25_WEIGHTS = "10.0, 6.0, 3.0, 1.0"

mcp = FastMCP(
    "vu-student-support",
    instructions=(
        "Static, citable knowledge for Virtual University of Pakistan (VU) students. "
        f"Two corpora: the official {SOURCE_NAME} ({SOURCE_URL}) for RULES, and the "
        f"{LMS_SOURCE_NAME} for WHERE-TO-CLICK procedures.\n"
        "WHICH TOOL TO USE:\n"
        "- Any student question, first call: vu_ask(question, program?, batch?). It searches "
        "both corpora, returns trimmed cited excerpts and tells you which tool to call next.\n"
        "- Rules, eligibility, exam policy, course-selection rules, fees, deadlines -> "
        "vu_handbook_search (pass program/batch for program-specific rules), then "
        "vu_handbook_get(id) for the full page. Browse topics with vu_handbook_contents.\n"
        "- 'Where do I find/click X in the LMS', what a tab/page shows -> vu_lms_search, "
        "vu_lms_get(section); vu_lms_contents lists every LMS page.\n"
        "- Fee amounts and deadline rules (freeze, rechecking, date sheet, refunds...) -> "
        "vu_key_dates_and_fees(topic).\n"
        "- GPA/CGPA from grades or marks -> vu_gpa_calculator (uses the handbook grading scheme).\n"
        "- 'How many credit hours can I take' -> vu_course_load_check(semester_number, cgpa, "
        "program, batch).\n"
        "Rules differ by PROGRAM and BATCH (e.g. 'BS', 'BS [w.e.f Batch 2104]', "
        "'BS [w.e.f Batch 2504]', 'BS-Lateral'): ask the student for both when it matters "
        "and pass them as program='BS Software Engineering', batch='Fall 2024' or '2404'.\n"
        "ALWAYS cite the returned title/breadcrumb/url. Live student data (grades, fee "
        "vouchers, Notice Board, To Do Calendar deadlines, this semester's calendar dates) "
        "is never served here: ask the student to check or paste it from their LMS. If "
        "nothing relevant is returned, say so and refer them to VU's Support System "
        "instead of guessing."
    ),
)


# --------------------------------------------------------------------------
# data access
# --------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Handbook database not found at {DB_PATH}. "
            "Run: python3 tools/crawl_handbook.py"
        )
    # Read-only: the MCP server must never mutate the snapshot (the crawler owns writes).
    # journal_mode=delete (not WAL), so mode=ro opens cleanly without -shm access.
    con = sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _toc() -> list[dict]:
    if not TOC_PATH.exists():
        raise FileNotFoundError(
            f"Handbook TOC not found at {TOC_PATH}. "
            "Run: python3 tools/crawl_handbook.py"
        )
    return json.loads(TOC_PATH.read_text(encoding="utf-8"))


def _expand(node: dict, depth: int) -> dict:
    """Render `node` and up to `depth` levels of its children beneath it."""
    kids = node.get("children", [])
    out = {
        "id": node["id"],
        "title": node["title"],
        "url": node.get("url"),
        "type": "page" if node.get("url") else "branch",
        "child_count": len(kids),
    }
    if kids and depth > 0:
        out["children"] = [_expand(c, depth - 1) for c in kids]
    return out


def _find_node(nodes: list[dict], path: str) -> tuple[list[dict], dict] | None:
    """Resolve a TOC location from an id, a URL, or a breadcrumb string."""
    path = (path or "").strip()
    if not path:
        # The site's root is a single "Home" node; surface its sections
        # directly so callers see the real top level on the first call.
        if len(nodes) == 1:
            return [], nodes[0]
        return [], {"id": 0, "title": SOURCE_NAME, "url": None, "children": nodes}

    # numeric id
    if re.fullmatch(r"\d+", path):
        target = int(path)

        def by_id(ns, crumb):
            for n in ns:
                here = crumb + [n["title"]]
                if n["id"] == target:
                    return here, n
                hit = by_id(n.get("children", []), here)
                if hit:
                    return hit
            return None

        hit = by_id(nodes, [])
        return hit if hit else None

    # URL
    if path.startswith("http"):
        for n, crumb in _walk(nodes, []):
            if n.get("url") == path:
                return crumb, n
        return None

    # breadcrumb: split on '>' or '/', match titles in order (case-insensitive)
    parts = [p.strip() for p in re.split(r"[>/]", path) if p.strip()]
    pool = nodes
    crumb: list[dict] = []
    node: dict | None = None
    matched: list[str] = []
    for part in parts:
        low = part.lower()
        node = next((c for c in pool if c["title"].strip().lower() == low), None)
        if node is None:
            node = next((c for c in pool if low in c["title"].strip().lower()), None)
        if node is None:
            return None
        matched.append(node["title"])
        pool = node.get("children", [])
        node = dict(node, _crumb=matched)
    assert node is not None
    # rebuild breadcrumb as plain titles of ancestors + self
    return matched[:-1], node


def _walk(nodes: list[dict], crumb: list[str]):
    for n in nodes:
        here = crumb + [n["title"]]
        yield here, n
        yield from _walk(n.get("children", []), here)


def _resolve_page(page) -> sqlite3.Row | None:
    """Resolve a page from an id, URL, exact title, or unique title substring."""
    q = str(page).strip()
    with _db() as con:
        if re.fullmatch(r"\d+", q):
            row = con.execute("SELECT * FROM pages WHERE id = ?", (int(q),)).fetchone()
            if row:
                return row
        if q.startswith("http"):
            row = con.execute("SELECT * FROM pages WHERE url = ?", (q,)).fetchone()
            if row:
                return row
            row = con.execute(
                "SELECT * FROM pages WHERE url LIKE ?", (f"%{q}",)
            ).fetchone()
            if row:
                return row
        row = con.execute(
            "SELECT * FROM pages WHERE lower(title) = lower(?)", (q,)
        ).fetchone()
        if row:
            return row
        rows = con.execute(
            "SELECT * FROM pages WHERE instr(lower(title), lower(?)) > 0", (q,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise LookupError(
                json.dumps(
                    {
                        "error": "ambiguous_title",
                        "query": q,
                        "candidates": [
                            {"id": r["id"], "title": r["title"], "breadcrumb": r["breadcrumb"]}
                            for r in rows[:15]
                        ],
                        "hint": "Call vu_handbook_get again with one of these ids.",
                    },
                    ensure_ascii=False,
                )
            )
        rows = con.execute(
            "SELECT * FROM pages WHERE instr(lower(breadcrumb), lower(?)) > 0 LIMIT 10",
            (q,),
        ).fetchall()
        if rows:
            raise LookupError(
                json.dumps(
                    {
                        "error": "no_title_match",
                        "query": q,
                        "candidates": [
                            {"id": r["id"], "title": r["title"], "breadcrumb": r["breadcrumb"]}
                            for r in rows
                        ],
                    },
                    ensure_ascii=False,
                )
            )
    return None


def _fts_query(query: str) -> list[str]:
    """Build a safe FTS5 MATCH expression; FTS syntax errors otherwise."""
    terms = re.findall(r"[A-Za-z0-9']+", query)
    quoted = [f'"{t}"' for t in terms if t]
    return quoted


# Function words that carry no retrieval signal for student questions.
_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "of", "in",
    "on", "at", "to", "for", "and", "or", "but", "if", "as", "by", "with", "from",
    "it", "its", "this", "that", "these", "those", "there", "here",
    "do", "does", "did", "done", "can", "could", "should", "would", "will", "shall",
    "may", "might", "must", "have", "has", "had",
    "i", "you", "we", "they", "he", "she", "my", "your", "our", "their", "me", "us",
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "not", "no", "yes", "any", "all", "some", "every", "than", "then", "too",
    "very", "just", "also", "into", "about", "up", "out", "so",
    # weak verbs / generic nouns that students add to questions but that
    # discriminate between handbook pages almost as badly as stopwords
    "get", "got", "make", "made", "want", "need", "needs", "take", "give",
    "show", "find", "tell", "know", "see", "look", "go", "come", "keep",
    "let", "try", "ask", "help", "use", "used", "able", "happen", "happens",
    "happened", "occur", "arise", "regarding", "related", "information",
    "details", "detail", "really", "actually", "possible", "procedure_for",
}
CANDIDATE_POOL = 60


def _significant_terms(query: str) -> list[str]:
    """Alphanumeric terms minus stop words, preserving order and uniqueness."""
    out: list[str] = []
    seen: set[str] = set()
    for t in re.findall(r"[a-z0-9']+", query.lower()):
        t = t.strip("'")
        if len(t) < 2 or t in _STOP or t in seen:
            continue
        seen.add(t)
        out.append(t)
    if not out:  # query was entirely stop words - fall back to everything
        out = [t for t in re.findall(r"[a-z0-9']+", query.lower()) if len(t) >= 2]
    return out


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
@mcp.tool()
def vu_handbook_contents(path: str = "", depth: int = 2) -> dict:
    """Browse the VU Student Handbook table of contents (rules corpus).

    Use to see what the handbook covers or to list every program page under
    a branch, e.g. path="Home > Course Selection > BS", or
    path="Home > Examinations Department". For a specific question prefer
    vu_ask or vu_handbook_search.

    Args:
        path: Where to look. Empty string = top level. Otherwise a node id
            ("11"), a full page URL, or a breadcrumb such as
            "Home > Examinations Department". Case-insensitive; '/' also works
            as a separator. Unknown segments fall back to substring matching.
        depth: How many levels of children to return (1 = immediate children).

    Returns the node at `path` plus its children. Child objects carry
    "type": "page" (has content, fetch with vu_handbook_get) or "branch".
    Use this to discover what the handbook covers before searching.
    """
    if depth < 1:
        depth = 1
    if depth > 6:
        depth = 6
    toc = _toc()
    hit = _find_node(toc, path)
    if hit is None:
        top = [{"id": n["id"], "title": n["title"], "type": "page" if n.get("url") else "branch"}
               for n in toc]
        raise LookupError(json.dumps(
            {"error": "path_not_found", "path": path, "top_level": top},
            ensure_ascii=False,
        ))
    crumb, node = hit
    node = {k: v for k, v in node.items() if k != "_crumb"}
    return {
        "source": SOURCE_NAME,
        "path": crumb,
        "breadcrumb": " > ".join(crumb + [node["title"]]) if node["id"] else SOURCE_NAME,
        "node": _expand(node, depth),
    }


def _handbook_candidates(query: str, sig: list[str], need: int, limit: int) -> list[tuple]:
    """Score handbook pages for `sig`; returns [(score, bm25, row, hits)] sorted."""
    # OR for recall; bm25 (title-weighted) for ordering. Precision comes from
    # requiring each hit to cover ~60% of the query's significant terms
    # (ceil(3n/5)), which stops one or two incidental words from dragging in
    # unrelated pages while still letting a natural question through.
    match = " OR ".join(f'"{t}"' for t in sig)
    pool = min(max(limit * 4, 40), CANDIDATE_POOL)

    sql = """
        SELECT p.id, p.title, p.breadcrumb, p.url, p.kind, p.headings, p.markdown,
               bm25(pages_fts, %s) AS rank,
               snippet(pages_fts, 3, '[', ']', '…', 26) AS snippet
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.rowid
        WHERE pages_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """ % _BM25_WEIGHTS

    with _db() as con:
        try:
            rows = con.execute(sql, (match, pool)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            rows = con.execute(
                """SELECT id, title, breadcrumb, url, kind, headings, markdown,
                          0.0 AS rank, substr(markdown, 1, 200) AS snippet
                   FROM pages
                   WHERE instr(lower(title), lower(?)) > 0
                      OR instr(lower(markdown), lower(?)) > 0
                   ORDER BY id LIMIT ?""",
                (query.strip(), f"%{query.strip()}%", pool),
            ).fetchall()

    scored = []
    for r in rows:
        hit = _score_row(r, sig, need)
        if hit is not None:
            scored.append(hit)
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


def _score_row(r, sig: list[str], need: int) -> tuple | None:
    in_heading = f"{r['title']} {r['breadcrumb']} {r['headings']}".lower()
    body = (r["markdown"] or "").lower()
    hits = [t for t in sig if t in in_heading or t in body]
    if len(hits) < need:
        return None
    title_hits = sum(1 for t in sig if t in r["title"].lower())
    return (title_hits * 2 + len(hits), float(r["rank"] or 0.0), r, hits)


@mcp.tool()
def vu_handbook_search(query: str, limit: int = DEFAULT_SEARCH_LIMIT,
                       program: str | None = None, batch: str | None = None) -> dict:
    """Search the VU Student Handbook (official RULES: eligibility, course
    selection, exams, fees, freeze, readmission, discipline...).

    Examples: query="semester freeze rules"; query="course selection",
    program="BS Software Engineering", batch="Fall 2024".

    Args:
        query: Question or keywords.
        limit: Maximum results (1-25, default 8).
        program: Optional program, e.g. "BS Software Engineering", "BSCS",
            "BS-Lateral IT", "ADP Computer Science", "MSCS", "MBA". Pages for
            that program rank first.
        batch: Optional batch/intake, e.g. "2404", "Fall 2024", "Spring 2025"
            or a year "2024". Picks the matching "w.e.f Batch" variant.

    Returns ranked matches (id, title, breadcrumb, url, snippet). With
    program/batch, each result has `applies_to` and a `note` lists batch
    variants when still ambiguous. Read a page with vu_handbook_get(id).
    Empty `results` means the handbook has no match - do not guess.
    """
    if not query or not query.strip():
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))

    sig = _significant_terms(query)
    if not sig:
        return {
            "source": SOURCE_NAME, "query": query, "count": 0, "results": [],
            "note": "No searchable terms in query.",
        }

    match = " OR ".join(f'"{t}"' for t in sig)
    n = len(sig)
    need = max(1, (3 * n + 4) // 5)
    scored = _handbook_candidates(query, sig, need, limit)

    filtered = bool((program and program.strip()) or (batch and str(batch).strip()))
    filt: dict | None = None
    if filtered:
        filt = _program_filter(program, batch)
        scored = _apply_program_filter(scored, filt, sig, need)
    top = scored[:limit]

    out = {
        "source": SOURCE_NAME,
        "query": query,
        "match": match,
        "required_terms": need,
        "count": len(top),
        "results": [
            {
                "id": r["id"],
                "title": r["title"],
                "breadcrumb": r["breadcrumb"],
                "url": r["url"],
                "kind": r["kind"],
                "matched_terms": hits,
                "snippet": (r["snippet"] or "").strip(),
                "rank": round(rank, 3),
            }
            for _score, rank, r, hits in top
        ],
        "note": (
            "No match found - the handbook does not cover this. Say so and "
            "escalate to VU's Support System instead of guessing."
            if not top
            else "Call vu_handbook_get(id) to read a page in full."
        ),
    }
    if filtered and filt is not None:
        for res in out["results"]:
            res["applies_to"] = _applies_to(res["id"], filt)
        out["filters"] = filt["public"]
        if filt.get("variants"):
            out["variants"] = filt["variants"]
        if top and filt.get("note"):
            out["note"] = filt["note"] + " " + out["note"]
        out["data_last_updated"] = _data_last_updated()
    return out


@mcp.tool()
def vu_handbook_get(page: str | int) -> dict:
    """Read one handbook page in full (after vu_ask / vu_handbook_search
    gave you its id). Example: page=159, or page="Semester Freeze".

    Args:
        page: A page id ("20"), an exact/unique title ("Acts of Unfair Means"),
            or a full handbook URL.

    Returns title, breadcrumb, canonical url, headings, the full markdown
    `content`, plus surrounding navigation (parent, children, siblings) so a
    follow-up hop needs no extra lookup. Content longer than 40k characters is
    truncated with `truncated: true`.
    """
    row = _resolve_page(page)
    if row is None:
        raise LookupError(json.dumps(
            {"error": "page_not_found", "query": str(page),
             "hint": "Use vu_handbook_search to locate the page first."},
            ensure_ascii=False,
        ))

    crumb = [c for c in (row["breadcrumb"] or "").split(" > ") if c]
    content = row["markdown"] or ""
    truncated = len(content) > MAX_CONTENT_CHARS
    if truncated:
        content = content[:MAX_CONTENT_CHARS] + "\n\n[... truncated ...]"

    nav = _navigation(row["id"], crumb)
    return {
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "id": row["id"],
        "title": row["title"],
        "breadcrumb": crumb,
        "url": row["url"],
        "kind": row["kind"],
        "headings": [h for h in (row["headings"] or "").split("\n") if h],
        "content": content,
        "truncated": truncated,
        "navigation": nav,
    }


def _navigation(page_id: int, crumb: list[str]) -> dict:
    toc = _toc()
    found: dict | None = None

    def dig(nodes, path):
        nonlocal found
        for n in nodes:
            here = path + [n["title"]]
            if n["id"] == page_id:
                found = {"node": n, "crumb": here}
                return True
            if dig(n.get("children", []), here):
                return True
        return False

    dig(toc, [])
    if not found:
        return {}

    node = found["node"]
    # locate the parent's child list
    def parent_children(nodes, path):
        for n in nodes:
            if any(c["id"] == page_id for c in n.get("children", [])):
                return n.get("children", [])
            here = path + [n["title"]]
            res = parent_children(n.get("children", []), here)
            if res is not None:
                return res
        return None

    siblings = parent_children(toc, []) or []
    return {
        "parent": " > ".join(found["crumb"][:-1]) or SOURCE_NAME,
        "children": [
            {"id": c["id"], "title": c["title"], "type": "page" if c.get("url") else "branch"}
            for c in node.get("children", [])
        ],
        "siblings": [
            {"id": s["id"], "title": s["title"]}
            for s in siblings
            if s["id"] != page_id
        ][:25],
    }


# --------------------------------------------------------------------------
# resources
# --------------------------------------------------------------------------
@mcp.resource("vu-handbook://toc")
def toc_resource() -> str:
    """The complete nested table of contents of the VU Student Handbook."""
    return json.dumps(
        {"source": SOURCE_NAME, "source_url": SOURCE_URL, "toc": _toc()},
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("vu-handbook://page/{page_id}")
def page_resource(page_id: str) -> str:
    """Raw markdown of a handbook page by id."""
    row = _resolve_page(page_id)
    if row is None:
        raise LookupError(f"no page with id {page_id}")
    return row["markdown"] or ""


@mcp.resource("vu-handbook://stats")
def stats_resource() -> str:
    """Coverage statistics for the local handbook snapshot."""
    with _db() as con:
        pages = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        chars = con.execute("SELECT COALESCE(SUM(LENGTH(markdown)),0) FROM pages").fetchone()[0]
        ok = con.execute("SELECT COUNT(*) FROM pages WHERE status='ok'").fetchone()[0]
    return json.dumps(
        {
            "source": SOURCE_NAME,
            "pages": pages,
            "fully_fetched": ok,
            "total_characters": chars,
            "data_last_updated": _data_last_updated(),
            "db": str(DB_PATH),
            "toc": str(TOC_PATH),
            "refresh_command": "python3 tools/crawl_handbook.py --refresh-toc",
        },
        indent=2,
    )


# --------------------------------------------------------------------------
# LMS guide corpus (docs/lms/*.md -> h2 sections, small and static)
# --------------------------------------------------------------------------
_LMS_CACHE: list[dict] | None = None


def _lms() -> list[dict]:
    """Sections of every markdown file in LMS_DIR, split on h2 headings."""
    global _LMS_CACHE
    if _LMS_CACHE is not None:
        return _LMS_CACHE
    sections: list[dict] = []
    if LMS_DIR.exists():
        for path in sorted(LMS_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            header: list[str] = []
            title: str | None = None
            body: list[str] = []

            def flush() -> None:
                if title:
                    sections.append({"file": path.name, "title": title,
                                     "text": "\n".join(body).strip()})

            for line in text.splitlines():
                if line.startswith("## "):
                    flush()
                    title, body = line[3:].strip(), []
                elif title is None:
                    header.append(line)
                else:
                    body.append(line)
            flush()
            if any(l.strip() for l in header):
                sections.append({"file": path.name, "title": f"{path.stem} — introduction",
                                 "text": "\n".join(header).strip()})
    _LMS_CACHE = sections
    return sections


def _lms_resolve(section: str) -> dict:
    """Resolve a section by exact title, unique substring, or vulms URL."""
    sections = _lms()
    if not sections:
        raise FileNotFoundError(
            f"No LMS guide found at {LMS_DIR}. Expected docs/lms/*.md")
    want = section.strip().lower()
    for s in sections:
        if s["title"].lower() == want:
            return s
    if "://" in section:  # a URL: find sections documenting it
        hits = [s for s in sections if section in s["text"]]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise LookupError(json.dumps({"error": "ambiguous_url",
                                          "candidates": [h["title"] for h in hits]}))
    hits = [s for s in sections if want in s["title"].lower()
            or want in s["text"].lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise LookupError(json.dumps({"error": "ambiguous_section",
                                      "candidates": [h["title"] for h in hits[:10]]}))
    raise LookupError(json.dumps({"error": "section_not_found", "query": section,
                                  "hint": "call vu_lms_contents for the section list"}))


@mcp.tool()
def vu_lms_contents() -> dict:
    """List every VU LMS (vulms.vu.edu.pk) page/feature with its subsections
    (the WHERE-TO-CLICK corpus). Use when you need an overview of the LMS,
    e.g. "what can I do in the LMS?"; then read one with vu_lms_get."""
    sections = _lms()
    if not sections:
        raise FileNotFoundError(f"No LMS guide found at {LMS_DIR}")
    files: dict[str, list[dict]] = {}
    for s in sections:
        subs = re.findall(r"^### (.+)$", s["text"], re.M)
        files.setdefault(s["file"], []).append(
            {"title": s["title"], "subsections": subs})
    return {
        "source": LMS_SOURCE_NAME,
        "files": files,
        "dynamic_note": (
            "Notice Board and To Do Calendar change DAILY and are not served here: "
            "ask the student to paste them from their logged-in LMS."),
    }


@mcp.tool()
def vu_lms_get(section: str) -> dict:
    """Read one LMS guide section in full, e.g. section="Grade Book" or
    section="Student Services" (how to reach a page, its tabs and fields).

    Args:
    section: a section title ("Grade Book"), a unique substring, or a full
    vulms.vu.edu.pk URL (matches the section that documents it).

    Returns the section markdown plus its source file and neighbours, so a
    follow-up hop needs no extra lookup. Ambiguous titles raise an error
    listing candidates instead of guessing.
    """
    hit = _lms_resolve(section)
    idx = next(i for i, s in enumerate(_lms()) if s is hit)
    all_sections = _lms()
    neighbours = [s["title"] for s in
                  (all_sections[max(0, idx - 1):idx] + all_sections[idx + 1:idx + 2])]
    return {
        "source": LMS_SOURCE_NAME,
        "file": hit["file"],
        "section": hit["title"],
        "content": hit["text"][:MAX_CONTENT_CHARS],
        "truncated": len(hit["text"]) > MAX_CONTENT_CHARS,
        "neighbouring_sections": neighbours,
    }


@mcp.tool()
def vu_lms_search(query: str, limit: int = 5) -> dict:
    """Search the LMS guide for WHERE/HOW-TO-CLICK answers and typical
    activity windows (quiz/assignment/GDB), e.g. "where is the grade book",
    "where do I apply for semester freeze", "challan payment modes".

    Args:
    query: question or keywords. limit: max results (1-15).

    Returns ranked sections with a snippet and the source file; follow up with
    vu_lms_get(section). Empty results means the guide does not cover this -
    do not invent LMS procedures.
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 15))
    terms = _significant_terms(query)
    scored: list[tuple[float, dict, str]] = []
    for s in _lms():
        title_l = s["title"].lower()
        text_l = s["text"].lower()
        title_hits = sum(1 for t in terms if t in title_l)
        body_hits = sum(1 for t in terms if t in text_l)
        coverage = body_hits / len(terms) if terms else 0.0
        if not terms or coverage < 0.5 and title_hits == 0:
            continue
        score = 3.0 * title_hits + 1.0 * body_hits + (2.0 if coverage == 1.0 else 0.0)
        first = next((ln for ln in s["text"].splitlines()
                      if any(t in ln.lower() for t in terms)), s["text"][:120])
        scored.append((score, s, first.strip()[:220]))
    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]
    return {
        "source": LMS_SOURCE_NAME,
        "query": query,
        "count": len(top),
        "results": [
            {"title": s["title"], "file": s["file"], "snippet": snip,
             "matched_terms": [t for t in terms
                               if t in s["title"].lower() or t in s["text"].lower()],
             "rank": round(score, 3)}
            for score, s, snip in top
        ],
        "note": (
            "No match found - the LMS guide does not cover this. Say so instead "
            "of inventing LMS procedures; for account issues escalate to VU's "
            "Support System."
            if not top else None),
    }


# --------------------------------------------------------------------------
# transport: stdio (default) or Streamable HTTP (production)
# --------------------------------------------------------------------------
@mcp.resource("vu-lms://sections")
def lms_sections_resource() -> str:
    """Every LMS guide section: file, title, subsections."""
    return json.dumps(
        {"source": LMS_SOURCE_NAME,
         "sections": [{"file": s["file"], "title": s["title"],
                       "subsections": re.findall(r"^### (.+)$", s["text"], re.M)}
                      for s in _lms()]},
        ensure_ascii=False, indent=2,
    )


@mcp.resource("vu-lms://guide")
def lms_guide_resource() -> str:
    """The complete LMS guide + operational notes as one markdown document."""
    return "\n\n---\n\n".join(
        f"## {s['title']}\n\n{s['text']}" for s in _lms())


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------
_FRESHNESS: dict | None = None


def _data_last_updated() -> str | None:
    """Newest crawl timestamp in data/pages.jsonl, else the DB file mtime (UTC ISO)."""
    global _FRESHNESS
    if _FRESHNESS is None:
        stamp = None
        jsonl = DATA_DIR / "pages.jsonl"
        try:
            if jsonl.exists():
                with jsonl.open(encoding="utf-8") as fh:
                    for line in fh:
                        m = re.search(r'"fetched_at":\s*"([^"]+)"', line)
                        if m and (stamp is None or m.group(1) > stamp):
                            stamp = m.group(1)
        except OSError:
            stamp = None
        if stamp is None and DB_PATH.exists():
            from datetime import datetime, timezone
            stamp = datetime.fromtimestamp(DB_PATH.stat().st_mtime, timezone.utc).isoformat(
                timespec="seconds")
        _FRESHNESS = {"handbook": stamp}
    return _FRESHNESS["handbook"]


# --------------------------------------------------------------------------
# program / batch catalogue (Home > Course Selection > <group> > <program>)
# --------------------------------------------------------------------------
_CATALOG: list[dict] | None = None

# word-level aliases applied to a user's program string before parsing
_PROGRAM_ALIASES = {
    "bsse": "bs software engineering", "bscs": "bs computer science",
    "bsit": "bs information technology", "bsds": "bs data science",
    "mscs": "ms computer science", "bba": "bs business administration",
    "se": "software engineering", "cs": "computer science",
    "it": "information technology", "ds": "data science",
    "comm": "communication", "psych": "psychology", "psy": "psychology",
    "fin": "finance", "acc": "accounting", "dbms": "database management system",
}
# (level label, regex over the normalised program string, groups it maps to)
_LEVELS: list[tuple[str, str, tuple[str, ...]]] = [
    ("BS-Lateral", r"\blateral\b", ("BS-Lateral",)),
    ("Associate Degree", r"\b(associate|adp|ad)\b", ("Associate Degree Programs",)),
    ("Deficiency Semester(s)", r"\bdeficiency\b", ("Deficiency Semester(s)",)),
    ("Diploma", r"\b(pgd|diploma)\b", ("Diploma",)),
    ("Ph.D", r"\bph\s?d\b", ("Ph.D",)),
    ("B.Ed", r"\bb\s?ed\b", ("B.Ed", "BS")),
    ("MBA", r"\bmba\b", ("MBA", "Masters", "MS")),
    ("MS", r"\b(ms|msba|mphil|m\s?phil)\b", ("MS",)),
    ("Masters", r"\b(masters?|mcs|mit|msc|m\s?sc|mcom|m\s?com|mpa|mbs|ma)\b", ("Masters",)),
    ("BS", r"\b(bs|bbit|bachelor\s+of\s+science)\b", ("BS",)),
    ("Bachelors", r"\b(bachelors|ba|bcom|b\s?com|bsc|b\s?sc)\b", ("Bachelors",)),
]
_LEVEL_WORDS = {
    "bs", "lateral", "associate", "degree", "degrees", "program", "programs", "programme",
    "adp", "ad", "diploma", "pgd", "phd", "ph", "bed", "ms", "mphil", "phil", "masters",
    "master", "bachelors", "bachelor", "in", "of", "the", "and", "hons", "batch", "wef",
    "w", "e", "f", "year", "years", "cr", "hrs", "track", "revised", "i", "my", "am",
}


def _norm_words(text: str) -> list[str]:
    t = text.lower().replace("&", " and ").replace("b.ed", "bed").replace("ph.d", "phd")
    t = t.replace("m.phil", "mphil")
    return re.findall(r"[a-z0-9]+", t)


def _subject_tokens(words: list[str]) -> list[str]:
    out = []
    for w in words:
        if w in _LEVEL_WORDS or re.fullmatch(r"\d+", w) or w in {"fall", "spring"}:
            continue
        out.append(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def _catalog() -> list[dict]:
    """Every program-specific Course Selection page with its batch variant."""
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    with _db() as con:
        rows = con.execute(
            "SELECT id, title, breadcrumb, url FROM pages "
            "WHERE breadcrumb LIKE 'Home > Course Selection > %'").fetchall()
    groups = {r["breadcrumb"].split(" > ")[2] for r in rows}
    cat = []
    for r in rows:
        segs = r["breadcrumb"].split(" > ")
        group = segs[2]
        if r["title"] in groups:
            continue
        base = re.sub(r"\[.*?\]", "", group).strip()
        eff = None
        for src in (group, r["title"]):
            m = re.search(r"(?:Batch\s*|\[)(\d{4})", src)
            if m:
                eff = int(m.group(1))
        subj = _subject_tokens(_norm_words(re.sub(r"\((?:Batch|w\.e\.f)[^)]*\)|\[\d{4}\]", "",
                                                  r["title"])))
        cat.append({
            "id": r["id"], "title": r["title"], "breadcrumb": r["breadcrumb"],
            "url": r["url"], "group": group, "level": base,
            "effective_from_batch": eff, "subject": subj,
            "key": (base, " ".join(sorted(set(subj)))),
        })
    _CATALOG = cat
    return cat


def _parse_batch(batch) -> tuple[list[int], str | None]:
    """'2404' / 'Fall 2024' / 'Spring 2025' / 'F24' / '2024' -> VU batch codes.

    VU batch codes are YY + term: 02 = Spring, 04 = Fall (e.g. 'Batch 2104 -
    Fall 2021', 'Batch 1902 - Spring 2019'). A bare year maps to both terms.
    """
    if batch is None:
        return [], None
    raw = str(batch).strip().lower()
    if not raw:
        return [], None
    m = re.search(r"\b(fall|spring|f|s)\s*'?(\d{2}|\d{4})\b", raw)
    if m:
        yy = int(m.group(2)) % 100
        term = 4 if m.group(1).startswith("f") else 2
        return [yy * 100 + term], None
    m = re.search(r"\b(\d{4})\b", raw)
    if m:
        v = int(m.group(1))
        if 2010 <= v <= 2099:
            yy = v % 100
            return [yy * 100 + 2, yy * 100 + 4], None
        if v % 100 in (2, 4):
            return [v], None
    return [], (f"Could not read batch {batch!r}; use a code like '2404', "
                "'Fall 2024' or a year like '2024'.")


def _parse_program(program: str | None) -> dict:
    if not program or not str(program).strip():
        return {"level": None, "groups": None, "subject": []}
    words = []
    for w in _norm_words(str(program)):
        words.extend(_PROGRAM_ALIASES.get(w, w).split())
    text = " ".join(words)
    level, groups = None, None
    for label, pat, grp in _LEVELS:
        if re.search(pat, text):
            level, groups = label, grp
            break
    subject = _subject_tokens(words)
    if level == "MBA":
        subject = [t for t in subject if t != "mba"] + ["mba"]
    return {"level": level, "groups": groups, "subject": subject}


def _subject_match(want: list[str], have: list[str]) -> bool:
    if not want:
        return True
    hs = set(have)
    def same(h: str, w: str) -> bool:
        if h == w:
            return True
        short, long_ = sorted((h, w), key=len)
        return len(short) >= 4 and long_.startswith(short)
    return all(any(same(h, w) for h in hs) for w in want)


def _program_filter(program: str | None, batch) -> dict:
    """Resolve program + batch to catalogue pages.

    Returns {selected_ids, matched_ids, variants, note, public, by_id}."""
    prog = _parse_program(program)
    codes, batch_err = _parse_batch(batch)
    cat = _catalog()
    by_id = {c["id"]: c for c in cat}
    notes: list[str] = []
    if batch_err:
        notes.append(batch_err)

    matched: list[dict] = []
    if program and str(program).strip():
        for c in cat:
            if prog["groups"] and c["level"] not in prog["groups"]:
                continue
            if not prog["groups"] and not prog["subject"]:
                continue
            if _subject_match(prog["subject"], c["subject"]):
                matched.append(c)
        # a level-only string like "BS" or an alias like "MBA" with a subject
        # that matches nothing: retry the subject without the level words
        if not matched and prog["subject"] and prog["groups"]:
            notes.append(f"No {prog['level']} page matched {program!r}.")
        if not matched:
            notes.append(f"Program {program!r} not recognised; results are unfiltered. "
                         "Examples: 'BS Software Engineering', 'BSCS', 'BS-Lateral IT', "
                         "'ADP Computer Science', 'MS Computer Science', 'MBA'.")

    def choose(pages: list[dict]) -> tuple[set[int], bool]:
        """Pick the variant per (level, subject) for the batch codes; flag disagreement."""
        sel: set[int] = set()
        disagree = False
        groups: dict = {}
        for c in pages:
            groups.setdefault(c["key"], []).append(c)
        for key, variants in groups.items():
            picks = set()
            for code in codes:
                eligible = [v for v in variants if (v["effective_from_batch"] or 0) <= code]
                if eligible:
                    best = max(v["effective_from_batch"] or 0 for v in eligible)
                    picks.update(v["id"] for v in eligible
                                 if (v["effective_from_batch"] or 0) == best)
            if len(picks) > 1 and len({by_id[i]["effective_from_batch"] for i in picks}) > 1:
                disagree = True
            sel |= picks
        return sel, disagree

    selected: set[int] = set()
    if codes:
        selected, disagree = choose(matched if matched else cat)
        if disagree:
            notes.append("The year you gave spans two batches (Spring/Fall) with different "
                         "rule versions; give the exact batch, e.g. 'Fall 2024' or '2404'.")
    elif matched:
        selected = {c["id"] for c in matched}

    variants = []
    if matched:
        for c in sorted(matched, key=lambda c: (c["level"], c["effective_from_batch"] or 0)):
            variants.append({
                "id": c["id"], "title": c["title"], "group": c["group"],
                "effective_from_batch": c["effective_from_batch"], "url": c["url"],
                "selected_for_batch": (c["id"] in selected) if codes else None,
            })
        keys = {c["key"] for c in matched}
        if len(keys) > 1 and len({k[1] for k in keys}) > 1:
            notes.append(f"{program!r} matches several programs; be more specific "
                         "(e.g. 'BS Computer Science').")
        if not codes and len(matched) > 1:
            notes.append(
                "This program has several batch versions in the handbook "
                f"({', '.join(sorted({c['group'] for c in matched}))}). Rules differ between "
                "them: ask the student for their batch and pass batch=, e.g. 'Fall 2024'.")
        elif codes and selected:
            notes.append(
                "Batch version chosen from the handbook's 'w.e.f Batch' labels (newest "
                "version that started on or before the student's batch); unlabelled pages "
                "are the oldest version. Confirm against the student's scheme of studies.")

    public = {
        "program": program, "batch": batch,
        "resolved_level": prog["level"], "resolved_subject": " ".join(prog["subject"]) or None,
        "batch_codes": codes,
        "selected_pages": sorted(selected) if (matched or codes) else [],
    }
    return {
        "selected_ids": selected, "matched_ids": {c["id"] for c in matched},
        "has_program": bool(matched), "has_batch": bool(codes),
        "variants": variants, "note": " ".join(notes) or None,
        "public": public, "by_id": by_id,
    }


def _filter_tier(page_id: int, filt: dict) -> int:
    cat = filt["by_id"].get(page_id)
    if filt["has_program"]:
        if page_id in filt["selected_ids"]:
            return 4
        if page_id in filt["matched_ids"]:
            return 3
        return 2 if cat is None else 0
    if filt["has_batch"] and cat is not None:
        return 2 if page_id in filt["selected_ids"] else 1
    return 2


def _applies_to(page_id: int, filt: dict) -> dict:
    cat = filt["by_id"].get(page_id)
    if cat is None:
        return {"scope": "general (all programs)", "filter_match": "general"}
    tier = _filter_tier(page_id, filt)
    label = {4: "selected", 3: "other_batch_version", 2: "selected", 1: "other_batch_version",
             0: "other_program"}[tier]
    if filt["has_program"] and tier == 4 and not filt["has_batch"]:
        label = "program_match_batch_unknown"
    return {"scope": cat["group"], "program": cat["title"],
            "effective_from_batch": cat["effective_from_batch"], "filter_match": label}


def _apply_program_filter(scored: list[tuple], filt: dict, sig: list[str], need: int) -> list:
    """Inject the program's own pages if relevant, then rank by filter tier."""
    have = {r["id"] for _s, _rk, r, _h in scored}
    inject = [i for i in (filt["selected_ids"] | filt["matched_ids"]) if i not in have]
    if filt["has_program"] and inject:
        with _db() as con:
            q = ("SELECT id, title, breadcrumb, url, kind, headings, markdown, 0.0 AS rank, "
                 "substr(markdown, 1, 200) AS snippet FROM pages WHERE id IN (%s)"
                 % ",".join("?" * len(inject)))
            for r in con.execute(q, inject).fetchall():
                hit = _score_row(r, sig, need)
                if hit is not None:
                    scored.append(hit)
    scored.sort(key=lambda x: (-_filter_tier(x[2]["id"], filt), -x[0], x[1]))
    return scored


# --------------------------------------------------------------------------
# excerpting: the few paragraphs of a page that answer the question
# --------------------------------------------------------------------------
EXCERPT_CHARS = 1500
_EXPAND = {
    "deadline": ["last date", "on or before", "within", "days", "before", "after"],
    "deadlines": ["last date", "on or before", "within", "days"],
    "date": ["last date", "days"], "dates": ["last date", "days"],
    "fee": ["fee", "rs.", "pkr", "charged", "charges"], "fees": ["fee", "rs.", "charged"],
    "cost": ["fee", "rs.", "charged"], "gpa": ["grade point", "gpa"],
    "cgpa": ["cgpa"], "credit": ["credit hour"], "grades": ["grade"],
}


def _term_matchers(terms: list[str]) -> list[list[str]]:
    out = []
    for t in terms:
        alts = [t] + _EXPAND.get(t, [])
        if len(t) > 4 and t.endswith("s"):
            alts.append(t[:-1])
        out.append(alts)
    return out


def _blocks(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, block) units: paragraphs, list items, tables."""
    out: list[tuple[str, str]] = []
    heading = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        lines = para.splitlines()
        if lines[0].startswith("#"):
            heading = lines[0].lstrip("# ").strip()
            rest = "\n".join(lines[1:]).strip()
            out.append((heading, lines[0]))
            if not rest:
                continue
            lines = rest.splitlines()
        if all(ln.startswith("|") for ln in lines):
            out.append((heading, "\n".join(lines)))
            continue
        for ln in lines if len(lines) > 1 and all(
                re.match(r"\s*([-*]|\d+[.)]|[ivx]+\.)\s", ln) for ln in lines) else ["\n".join(lines)]:
            if len(ln) > 700:
                for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z(])|\s(?=[ivx]{1,4}\.\s)", ln):
                    if sent.strip():
                        out.append((heading, sent.strip()))
            else:
                out.append((heading, ln.strip()))
    return out


def _excerpt(text: str, terms: list[str], cap: int = EXCERPT_CHARS) -> str:
    """The most relevant blocks of `text` for `terms`, in page order, <= cap chars."""
    if not text:
        return ""
    if len(text) <= cap:
        return text.strip()
    matchers = _term_matchers(terms)
    blocks = _blocks(text)
    scores = []
    for i, (_h, b) in enumerate(blocks):
        low = b.lower()
        distinct = sum(1 for alts in matchers if any(a in low for a in alts))
        occ = sum(low.count(a) for alts in matchers for a in alts[:1])
        is_heading = b.startswith("#")
        scores.append(distinct * 2 + min(occ, 4) * 0.25 - (1.5 if is_heading else 0))
    order = sorted(range(len(blocks)), key=lambda i: (-scores[i], i))
    chosen: list[int] = []
    used = 0
    for i in order:
        if scores[i] <= 0:
            break
        b = blocks[i][1]
        if used + len(b) > cap:
            if used == 0:  # single huge block: window around the first hit
                low = b.lower()
                pos = min((low.find(a) for alts in matchers for a in alts if a in low),
                          default=0)
                start = max(0, pos - cap // 3)
                chosen.append(i)
                blocks[i] = (blocks[i][0], ("…" if start else "") + b[start:start + cap - 2] + "…")
                used = cap
            continue
        chosen.append(i)
        used += len(b) + 1
    if not chosen:
        return text[:cap].rstrip() + "…"
    chosen.sort()
    parts: list[str] = []
    last = -2
    last_heading = None
    for i in chosen:
        h, b = blocks[i]
        if i != last + 1 and parts:
            parts.append("…")
        if h and h != last_heading and not b.startswith("#") and used + len(h) + 4 <= cap + 80:
            parts.append(f"## {h}")
            used += len(h) + 4
        last_heading = h
        parts.append(b)
        last = i
    out = "\n".join(parts)
    if len(out) > cap + 60:  # headings/separators may overshoot slightly; trim on a line
        cut = out.rfind("\n", 0, cap)
        out = out[:cut if cut > cap // 2 else cap].rstrip() + "\n…"
    return out


def _lms_url(text: str) -> str | None:
    m = re.search(r"\*\*URL:\*\*\s*`?(https?://[^\s`]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"https?://(?:vulms|www)\.vu\.edu\.pk/[^\s`)\]]+", text)
    return m.group(0) if m else None


# --------------------------------------------------------------------------
# vu_ask: one-call answer helper over both corpora
# --------------------------------------------------------------------------
_LMS_CUES = re.compile(
    r"\b(where|click|lms|vulms|tab|button|menu|sidebar|portal|screen|link|open|"
    r"grade ?book|account ?book|challan|voucher|to ?do|notice ?board|gdb|mdb|quiz|"
    r"assignment|lecture|ticket|e-?id|study scheme|navigate|find)\b", re.I)
_RULE_CUES = re.compile(
    r"\b(rule|rules|policy|eligib\w*|allowed|allow|maximum|minimum|max|min|limit|fee|fees|"
    r"deadline|last date|cgpa|gpa|credit hours?|freeze|readmission|exam\w*|unfair|plagiarism|"
    r"scholarship|refund|degree|transcript|requirement|criteria|can i|am i|how many|"
    r"penalty|fine|duration|attendance|grading)\b", re.I)


def _suggest_tools(question: str) -> list[dict]:
    q = question.lower()
    out = []
    if re.search(r"\b(s?gpa|cgpa|grade points?)\b", q) and re.search(
            r"\b(calculat\w*|compute|what (?:is|will be) my|for grades|grades?\b.*\b(a|b|c|d)[+-]?)",
            q):
        out.append({"tool": "vu_gpa_calculator",
                    "why": "computes SGPA/CGPA with the handbook grading scheme"})
    if re.search(r"credit hours?|course load|how many (courses|credits)|max(imum)? credits?", q):
        out.append({"tool": "vu_course_load_check",
                    "why": "credit-hour limit for a semester/CGPA from the program's rules "
                           "(needs program and batch)"})
    if re.search(r"\b(fee|fees|charges?|cost|dues|deadline|last date|refund|installments?|"
                 r"late fee|fine)\b", q):
        out.append({"tool": "vu_key_dates_and_fees",
                    "why": "fee amounts and deadline rules with citations"})
    return out


@mcp.tool()
def vu_ask(question: str, program: str | None = None, batch: str | None = None,
           limit: int = 3) -> dict:
    """START HERE for any VU student question. One call searches both the
    Student Handbook (rules) and the LMS guide (where to click), and returns
    the best pages with a trimmed, relevant excerpt, a citation and the exact
    tool call to read more.

    Examples: question="course selection rules", program="BS Software
    Engineering", batch="Fall 2024"; question="where is the grade book in LMS".

    Args:
        question: The student's question in plain words.
        program: Optional program ("BSSE", "BS Computer Science", "ADP IT",
            "BS-Lateral", "MSCS") - ranks that program's pages first.
        batch: Optional batch ("2404", "Fall 2024", "2024") - picks the
            right "w.e.f Batch" version of program rules.
        limit: Number of results (1-6, default 3).

    Returns `results` (source handbook|lms, title, breadcrumb, url, excerpt
    <= ~1500 chars, read_more), `suggested_tools`, `how_to_answer`, notes.
    """
    if not question or not question.strip():
        raise ValueError("question must not be empty")
    limit = max(1, min(int(limit), 6))
    sig = _significant_terms(question)
    lms_cue = len(_LMS_CUES.findall(question))
    rule_cue = len(_RULE_CUES.findall(question))

    hb = vu_handbook_search(question, limit=8, program=program, batch=batch)
    lm = vu_lms_search(question, limit=5)

    cands: list[tuple[float, dict]] = []
    n = max(1, len(sig))
    narrowed = bool(hb.get("filters") and hb["filters"].get("selected_pages"))
    for i, r in enumerate(hb["results"]):
        at = r.get("applies_to") or {}
        if narrowed and at.get("filter_match") in ("other_batch_version", "other_program"):
            continue  # wrong version for this student; still listed under `variants`
        cov = len(r["matched_terms"]) / n
        title_hits = sum(1 for t in sig if t in r["title"].lower())
        # search order already encodes title weighting and the program filter
        score = 10 - i * 1.2 + cov * 4 + title_hits * 1.5 + rule_cue * 1.0
        if at.get("filter_match") in ("selected", "program_match_batch_unknown") \
                and at.get("scope", "").startswith(("BS", "Associate", "Diploma", "MS", "M",
                                                   "B.Ed", "Bachelors", "Ph.D", "Deficiency")):
            score += 3
        cands.append((score, {"source": "handbook", "id": r["id"], "row": r}))
    for i, r in enumerate(lm["results"]):
        cov = len(r["matched_terms"]) / n
        title_hits = sum(1 for t in sig if t in r["title"].lower())
        score = 8 - i * 1.2 + cov * 4 + title_hits * 1.5 + lms_cue * 2.0
        cands.append((score, {"source": "lms", "row": r}))
    cands.sort(key=lambda x: -x[0])

    # drop weak tail results (well below the best hit), keep at least one
    if cands:
        best = cands[0][0]
        cands = [c for c in cands if c[0] >= best - 9] or cands[:1]
    # keep the best of the other corpus too when it is genuinely relevant
    picked = cands[:limit]
    if limit >= 2 and picked and len({c[1]["source"] for c in picked}) == 1:
        other = next((c for c in cands[limit:] if c[1]["source"] != picked[0][1]["source"]
                      and c[0] >= picked[0][0] - 8), None)
        if other:
            picked = picked[:limit - 1] + [other]

    results = []
    lms_sections = {(s["file"], s["title"]): s for s in _lms()}
    with _db() as con:
        for score, c in picked:
            r = c["row"]
            if c["source"] == "handbook":
                md = con.execute("SELECT markdown FROM pages WHERE id = ?",
                                 (r["id"],)).fetchone()[0] or ""
                item = {
                    "source": "handbook", "title": r["title"], "breadcrumb": r["breadcrumb"],
                    "url": r["url"], "page_id": r["id"],
                    "excerpt": _excerpt(md, sig),
                    "read_more": {"tool": "vu_handbook_get", "args": {"page": r["id"]}},
                }
                if r.get("applies_to"):
                    item["applies_to"] = r["applies_to"]
            else:
                sec = lms_sections.get((r["file"], r["title"]), {"text": ""})
                item = {
                    "source": "lms", "title": r["title"],
                    "breadcrumb": f"{LMS_SOURCE_NAME} > {r['file']} > {r['title']}",
                    "url": _lms_url(sec["text"]),
                    "excerpt": _excerpt(sec["text"], sig),
                    "read_more": {"tool": "vu_lms_get", "args": {"section": r["title"]}},
                }
            item["relevance"] = round(score, 2)
            results.append(item)

    notes = []
    if hb.get("filters") and hb.get("note") and "Call vu_handbook_get" not in hb["note"][:30]:
        notes.append(hb["note"].replace(" Call vu_handbook_get(id) to read a page in full.", ""))
    elif hb.get("note") and hb.get("filters"):
        pass
    prog_pages = [r for r in hb["results"] if "Course Selection >" in r["breadcrumb"]]
    picked_prog = any(i.get("page_id") in {r["id"] for r in prog_pages} for i in results)
    if not program and picked_prog and len({r["title"] for r in prog_pages}) >= 2:
        notes.append("Results include rules for several different programs, which differ. "
                     "Ask the student's program and batch and call again with program= and "
                     "batch=.")
    return {
        "question": question,
        "filters": hb.get("filters"),
        "count": len(results),
        "results": results,
        "variants": hb.get("variants"),
        "suggested_tools": _suggest_tools(question),
        "note": " ".join(notes) or None,
        "how_to_answer": (
            "Answer only from these excerpts (read_more for the full page) and cite title + "
            "url for every fact. If the excerpts do not answer the question, say so - do not "
            "guess rules, dates or fees. Program rules differ by program and batch: say which "
            "version you used. Live student data (grades, vouchers/fees due, Notice Board, "
            "To Do Calendar, this semester's dates) is not served here - ask the student to "
            "check their LMS. Unresolved issues: VU Support System."),
        "sources": {"handbook": SOURCE_URL, "lms": LMS_SOURCE_NAME},
        "data_last_updated": _data_last_updated(),
    }


# --------------------------------------------------------------------------
# vu_key_dates_and_fees: deadline rules and fee amounts, cited
# --------------------------------------------------------------------------
# (topic, trigger words, handbook page ids, LMS sections, row/sentence keywords)
_FACT_TOPICS: list[tuple[str, tuple[str, ...], tuple[int, ...], tuple[str, ...], tuple[str, ...]]] = [
    ("semester unfreeze", ("unfreeze",), (65,), ("Student Services",), ("unfreeze",)),
    ("semester freeze", ("freeze", "freezing"), (64, 46), ("Student Services",), ("freeze",)),
    ("paper rechecking", ("recheck", "re-check", "rechecking", "re-checking"), (28, 46), (),
     ("recheck", "re-check")),
    ("missed paper rescheduling", ("reschedul", "missed paper", "missed exam"), (18, 46),
     ("Date Sheet",), ("reschedul", "missed")),
    ("date sheet", ("date sheet", "datesheet"), (13, 46), ("Date Sheet",), ("date sheet",)),
    ("final year project late submission", ("final year project", "fyp", "deliverable",
                                            "cs619"), (56, 46), (), ("deliverable", "project")),
    ("readmission", ("readmission", "re-admission", "readmit"), (69, 46), (), ("readmission",
                                                                             "re-admission")),
    ("security refund", ("security", "refund"), (53, 52, 44), (), ("security", "refund")),
    ("installments and blocking", ("installment", "instalment", "blocking", "blocked", "dues",
                                   "voucher", "challan"), (51, 50, 55), ("Account Book",),
     ("installment", "block", "voucher", "dues")),
    ("scholarships", ("scholarship",), (47, 48), (), ("scholarship", "cgpa")),
    ("transcripts, degree and certificates", ("transcript", "degree fee", "certificate",
                                              "verification", "duplicate", "id card", "noc"),
     (46, 34, 35, 36), (), ("transcript", "degree", "certificate", "verification",
                           "duplicate", "card")),
    ("convocation", ("convocation",), (41, 44), (), ("convocation",)),
    ("admission withdrawal", ("withdraw",), (61,), (), ("withdraw",)),
    ("degree duration", ("duration", "maximum time", "time limit"), (308,), (), ("duration",)),
    ("add / drop course selection", ("add", "drop", "course selection", "enrol", "enroll"),
     (312,), ("Course Selection",), ("drop", "add course", "add, drop", "add/drop",
                                     "last date of course selection")),
    ("tuition and semester fees", ("tuition", "fee structure", "semester fee", "credit hour",
                                   "admission fee", "enrollment fee", "enrolment fee", "fees",
                                   "fee", "lab fee"), (44, 45, 55, 51), ("Account Book",),
     ("fee",)),
]
_SIGNAL = re.compile(
    r"(last date|on or before|\bbefore\b|\bafter\b|\bwithin\b|deadline|\bfee|\brs\b|pkr|\$|"
    r"\busd\b|\bdays?\b|\bweeks?\b|\bmonths?\b|charged|refund|\d{1,3}(?:,\d{3})+|\d+\s*%)",
    re.I)
_ENUM = re.compile(r"^\s*(?:[-*o]|\(?\d+[.)]|\(?[ivx]+[.)]|\(?[a-h][.)])\s+", re.I)
_WEAK_FEE_WORDS = ("fees", "fee")


def _kw_in(keywords, text: str) -> bool:
    """Keyword at a word start (so 'mess' does not match 'message')."""
    for k in keywords:
        if k.endswith("$"):  # whole word only
            if re.search(r"(?<![a-z])" + re.escape(k[:-1]) + r"(?![a-z])", text):
                return True
        elif re.search(r"(?<![a-z])" + re.escape(k), text):
            return True
    return False
_LEVEL_ROW = {
    "BS-Lateral": "BS-Lateral", "Associate Degree": "Associate Degree Programs",
    "Diploma": "Diploma", "Ph.D": "Ph.D", "B.Ed": "B.Ed", "MS": "MS", "Masters": "Masters",
    "BS": "BS", "Bachelors": "Bachelors", "MBA": "MBA Top-up",
}


def _fee_header(md: str) -> list[str] | None:
    m = re.search(r"\| Degree Program \|(.*?)\| (?:Virtual Stem School|Associate Degree)", md)
    if not m:
        return None
    return [c.strip() for c in m.group(1).split("|") if c.strip()]


def _facts_from_page(md: str, keywords: tuple[str, ...], whole_page: bool,
                     level_row: str | None) -> list[str]:
    facts: list[str] = []
    header = _fee_header(md)
    seen: set[str] = set()

    def add(t: str) -> None:
        t = re.sub(r"\s+", " ", t).strip()
        if t and t not in seen and len(t) < 700:
            seen.add(t)
            facts.append(t)

    for ln in md.splitlines():
        ln = ln.strip()
        if ln.startswith("|") and len(ln) < 260:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            label = cells[0].lower()
            if len(cells) < 2:
                if "w.e.f" in label or "applicable" in label:
                    add(cells[0])
                continue
            if _kw_in(keywords, label) or (level_row and cells[0] == level_row):
                if header and len(cells) - 1 == len(header):
                    add(f"{cells[0]}: " + "; ".join(f"{h} = {v}" for h, v in
                                                     zip(header, cells[1:])))
                elif len(cells) == 3 and re.fullmatch(r"[\d,]+", cells[1] or "x"):
                    add(f"{cells[0]}: Rs. {cells[1]} (local) / US$ {cells[2]} (overseas)")
                else:
                    add(" | ".join(cells))
    if facts and not whole_page:
        return facts[:12]
    for _h, b in _blocks(md):
        if b.startswith("|") or b.startswith("#"):
            continue
        low = b.lower()
        if not _SIGNAL.search(_ENUM.sub("", b)) or re.search(r"\bclick\b|from within the lms",
                                                             low):
            continue
        if whole_page or _kw_in(keywords, low):
            add(b)
    return facts[:14]


@mcp.tool()
def vu_key_dates_and_fees(topic: str, program: str | None = None) -> dict:
    """Fee amounts and deadline RULES from the handbook (and LMS guide),
    each fact cited. Topics: semester freeze/unfreeze, tuition & semester
    fees, other charges (transcript, degree, ID card), installments and
    LMS blocking, rechecking, missed-paper rescheduling, date sheet, FYP late
    submission, readmission, security refund, scholarships, add/drop.

    Examples: topic="semester freeze deadline"; topic="tuition fee per
    credit hour", program="BS".

    Args:
        topic: What the student asks about, in plain words.
        program: Optional program/level to pick the matching fee-table row.

    The handbook states deadlines relative to the Academic Calendar (e.g.
    "on or before the last date of course selection"); actual calendar
    dates for the current semester are NOT in this data.
    """
    if not topic or not topic.strip():
        raise ValueError("topic must not be empty")
    low = topic.lower()
    hits = [t for t in _FACT_TOPICS if _kw_in(t[1], low)]
    if len(hits) > 1:  # bare "fee" should not drag the tuition tables into a specific topic
        hits = [t for t in hits if t[0] != "tuition and semester fees"
                or _kw_in([w for w in t[1] if w not in _WEAK_FEE_WORDS], low)]
    level = _parse_program(program)["level"] if program else None
    level_row = _LEVEL_ROW.get(level) if level else None
    out_facts: list[dict] = []
    pages: list[dict] = []
    matched_topics = [t[0] for t in hits[:2]]

    with _db() as con:
        page_ids: list[tuple[int, tuple[str, ...], bool]] = []
        for _name, _w, ids, _lms_secs, kws in hits[:2]:
            for j, pid in enumerate(ids):
                page_ids.append((pid, kws, j == 0))  # whole-page only if small (below)
        if not hits:  # fall back to search, but only keep sentences with a date/fee signal
            res = vu_handbook_search(topic, limit=3)
            sig = tuple(t + "$" for t in _significant_terms(topic))
            page_ids = [(r["id"], sig, False) for r in res["results"]]
        done: set[int] = set()
        for pid, kws, whole in page_ids:
            if pid in done:
                continue
            done.add(pid)
            row = con.execute("SELECT id, title, breadcrumb, url, markdown FROM pages "
                              "WHERE id = ?", (pid,)).fetchone()
            if row is None:
                continue
            md = row["markdown"] or ""
            facts = _facts_from_page(md, kws, whole and len(md) < 6000, level_row)
            if not facts:
                continue
            pages.append({"page_id": row["id"], "title": row["title"],
                          "breadcrumb": row["breadcrumb"], "url": row["url"]})
            for f in facts:
                out_facts.append({"fact": f, "source": "handbook", "title": row["title"],
                                  "url": row["url"], "page_id": row["id"]})

    lms_facts = []
    sections = {s["title"]: s for s in _lms()}
    for _name, _w, _ids, secs, kws in hits[:2]:
        for sec_title in secs:
            s = sections.get(sec_title)
            if not s:
                continue
            for _h, b in _blocks(s["text"]):
                bl = b.lower()
                if _kw_in(kws, bl) and _SIGNAL.search(_ENUM.sub("", b)) or (
                        "fee" in kws and "challan" in bl and "|" in b):
                    lms_facts.append({"fact": re.sub(r"\s+", " ", b)[:400], "source": "lms",
                                      "title": sec_title, "url": _lms_url(s["text"])})
    if any(w in low for w in ("date sheet", "quiz", "assignment", "gdb", "reschedul", "result")):
        ops = sections.get("To Do Calendar — Typical Deadline Windows")
        ds = sections.get("Date Sheet")
        for s in (ds, ops):
            if s and s["file"] == "OPERATIONAL_NOTES.md":
                lms_facts.append({"fact": _excerpt(s["text"], _significant_terms(topic), 700),
                                  "source": "lms_operational_notes", "title": s["title"],
                                  "url": None,
                                  "caveat": "operator-provided typical windows, not official"})
    out_facts.extend(lms_facts[:6])

    return {
        "topic": topic,
        "matched_topics": matched_topics or None,
        "count": len(out_facts),
        "facts": out_facts,
        "pages": pages,
        "calendar_note": (
            "Deadlines in the handbook are rules relative to the Academic Calendar (e.g. 'on "
            "or before the last date of course selection'). This semester's actual dates are "
            "not in this data: the student must check the Academic Calendar / To Do Calendar "
            "/ announcements in their LMS."),
        "fee_note": (
            "Fee tables state their own effective date (e.g. 'w.e.f. Fall 2023'). Amounts the "
            "student actually owes are on their LMS Account Book / challan; the University "
            "may revise fees."),
        "note": None if out_facts else (
            "No fee or deadline rule found for this topic in the handbook. Say so and refer "
            "the student to VU's Support System; do not guess."),
        "data_last_updated": _data_last_updated(),
    }


# --------------------------------------------------------------------------
# vu_gpa_calculator: grading scheme parsed from the handbook itself
# --------------------------------------------------------------------------
GRADING_PAGE_TITLE = "Academic Rules and Regulation"
_GRADING: dict | None = None


class CourseGrade(BaseModel):
    """One course: a letter grade and/or marks percentage, plus credit hours."""
    credit_hours: float = Field(description="Credit hours of the course, e.g. 3")
    grade: str | None = Field(default=None, description="Letter grade: A+, A, A-, B+, B, B-, "
                              "C, D or F")
    marks: float | None = Field(default=None, description="Optional total marks percentage "
                                "(0-100); gives the exact grade point")


def _grading() -> dict:
    """Parse '20. GRADING SCHEME' from the Academic Rules page. Raises if absent."""
    global _GRADING
    if _GRADING is not None:
        return _GRADING
    with _db() as con:
        row = con.execute("SELECT id, title, url, markdown FROM pages WHERE title = ?",
                          (GRADING_PAGE_TITLE,)).fetchone()
    if row is None:
        raise LookupError("Grading scheme page not found in the handbook snapshot.")
    md = row["markdown"] or ""
    start = md.find("GRADING SCHEME")
    if start < 0:
        raise LookupError("GRADING SCHEME section not found in the handbook snapshot.")
    section = md[start:start + 6000]
    scale = {}
    for m in re.finditer(r"^\|\s*([A-DF][+-]?)\s*\|\s*([\d.]+)(?:\s*-\s*([\d.]+))?\s*\|\s*"
                         r"([\d]+)\s*[–-]\s*([\d]+)\s*\|", section, re.M):
        lo = float(m.group(2))
        hi = float(m.group(3)) if m.group(3) else lo
        scale[m.group(1)] = {"grade_points_min": lo, "grade_points_max": hi,
                             "marks": f"{m.group(4)}-{m.group(5)}",
                             "marks_min": int(m.group(4)), "marks_max": int(m.group(5))}
    needed = {"A+", "A", "A-", "B+", "B", "B-", "C", "D", "F"}
    if not needed.issubset(scale):
        raise LookupError("Could not parse the full grading table from the handbook "
                          f"(found {sorted(scale)}).")
    gpa_rule = re.search(r"GPA = Sum over all courses in a semester[^\n]*", section)
    cgpa_rule = re.search(r"CGPA = Sum over all taken courses[^\n]*", section)
    repeat_rule = re.search(r"21\.3 [^\n]*", section)
    _GRADING = {
        "page_id": row["id"], "title": row["title"], "url": row["url"], "scale": scale,
        "section": "20. GRADING SCHEME / 20.1 / 21. FORMULA FOR CALCULATING THE GPA AND CGPA",
        "gpa_rule": gpa_rule.group(0) if gpa_rule else None,
        "cgpa_rule": cgpa_rule.group(0) if cgpa_rule else None,
        "repeat_rule": repeat_rule.group(0) if repeat_rule else None,
    }
    return _GRADING


def _gp_from_marks(score: float) -> tuple[str, float]:
    """Section 20.1 'FORMULA FOR CALCULATING THE GRADE POINTS (GP)'."""
    bands = [  # (letter, lower score, GP at lower bound, slope numerator, slope denominator)
        ("A-", 80, 3.66, 0.34, 4.99), ("B+", 75, 3.33, 0.32, 4.99),
        ("B", 71, 3.00, 0.32, 3.99), ("B-", 68, 2.66, 0.33, 2.99),
        ("C", 61, 2.00, 0.65, 6.99), ("D", 50, 1.00, 0.99, 10.99),
    ]
    if score >= 90:
        return "A+", 4.0
    if score >= 85:
        return "A", 4.0
    for letter, lo, base, num, den in bands:
        if score >= lo:
            return letter, (score - lo) * (num / den) + base
    return "F", 0.0


@mcp.tool()
def vu_gpa_calculator(courses: list[CourseGrade], previous_cgpa: float | None = None,
                      previous_credit_hours: float | None = None) -> dict:
    """Compute semester GPA (SGPA) and optionally new CGPA with VU's official
    grading scheme (Academic Rules and Regulations, sections 20-21).

    Example: courses=[{"grade":"A","credit_hours":3},{"grade":"B+",
    "credit_hours":3}], previous_cgpa=3.2, previous_credit_hours=30.

    VU grade points depend on MARKS, not only the letter (B+ = 3.33-3.65).
    With letters only you get a min-max range; add "marks" (percentage) per
    course for the exact value. Repeated/improved courses count once with the
    best grade (rule 21.3) - this calculator does not detect repeats.
    """
    if not courses:
        raise ValueError("courses must contain at least one course")
    g = _grading()
    scale = g["scale"]
    rows = []
    total_ch = 0.0
    lo_sum = hi_sum = 0.0
    for i, c in enumerate(courses):
        if isinstance(c, dict):
            c = CourseGrade(**c)
        ch = float(c.credit_hours)
        if ch <= 0 or ch > 12:
            raise ValueError(f"course {i + 1}: credit_hours must be between 0 and 12")
        letter = (c.grade or "").strip().upper().replace(" ", "") or None
        if c.marks is not None:
            if not 0 <= c.marks <= 100:
                raise ValueError(f"course {i + 1}: marks must be 0-100")
            m_letter, gp = _gp_from_marks(float(c.marks))
            if letter and letter != m_letter:
                raise ValueError(f"course {i + 1}: marks {c.marks} give grade {m_letter}, "
                                 f"not {letter}")
            letter, lo, hi = m_letter, gp, gp
        elif letter:
            if letter not in scale:
                raise ValueError(f"course {i + 1}: unknown grade {c.grade!r}; VU grades are "
                                 + ", ".join(scale))
            lo, hi = scale[letter]["grade_points_min"], scale[letter]["grade_points_max"]
        else:
            raise ValueError(f"course {i + 1}: give grade or marks")
        total_ch += ch
        lo_sum += lo * ch
        hi_sum += hi * ch
        rows.append({"course": i + 1, "grade": letter, "credit_hours": ch,
                     "grade_points": round(lo, 2) if lo == hi else [round(lo, 2), round(hi, 2)],
                     "marks": c.marks})
    exact = lo_sum == hi_sum
    sg_lo, sg_hi = lo_sum / total_ch, hi_sum / total_ch
    out: dict = {
        "courses": rows,
        "semester_credit_hours": total_ch,
        "exact": exact,
        "sgpa": round(sg_lo, 2) if exact else None,
        "sgpa_range": None if exact else [round(sg_lo, 2), round(sg_hi, 2)],
    }
    if previous_cgpa is not None or previous_credit_hours is not None:
        if previous_cgpa is None or previous_credit_hours is None:
            raise ValueError("give both previous_cgpa and previous_credit_hours")
        if not 0 <= previous_cgpa <= 4 or previous_credit_hours < 0:
            raise ValueError("previous_cgpa must be 0-4 and previous_credit_hours >= 0")
        base = previous_cgpa * previous_credit_hours
        denom = previous_credit_hours + total_ch
        c_lo, c_hi = (base + lo_sum) / denom, (base + hi_sum) / denom
        out["cumulative_credit_hours"] = denom
        out["cgpa"] = round(c_lo, 2) if exact else None
        out["cgpa_range"] = None if exact else [round(c_lo, 2), round(c_hi, 2)]
    out.update({
        "grading_scale_used": {k: {"grade_points": (v["grade_points_min"]
                                                    if v["grade_points_min"] == v["grade_points_max"]
                                                    else [v["grade_points_min"],
                                                          v["grade_points_max"]]),
                                   "marks_percent": v["marks"]} for k, v in scale.items()},
        "formula": g["gpa_rule"], "cgpa_formula": g["cgpa_rule"],
        "note": ("Letter grades A-, B+, B, B-, C and D cover a range of grade points, so the "
                 "result is a range; pass marks (%) per course for the exact figure. "
                 + (g["repeat_rule"] or "") + " The LMS Grade Book has a 'Projected CGPA "
                 "Calculator' tab with the student's real records."),
        "source": {"title": g["title"], "section": g["section"], "url": g["url"],
                   "page_id": g["page_id"]},
        "data_last_updated": _data_last_updated(),
    })
    return out


# --------------------------------------------------------------------------
# vu_course_load_check: credit-hour limits from the program's own page
# --------------------------------------------------------------------------
_LIMIT_BLOCK = re.compile(
    r"credit hour limits are as below:?(.*?)(?:Note:\s*Maximum limits prescribed[^\n]*|"
    r"\n- Students taking|\n## )", re.S | re.I)
_RX = {
    "a_first_two": r"first and second semesters? a maximum of (\d+) credit hours",
    "a_first": r"For the first semester,? a maximum of (\d+) credit hours",
    "a_second": r"For the second semester,? a maximum of (\d+) credit hours",
    "a_certain": r"in certain cases,? the University may allow to enroll (?:a )?maximum (?:of )?"
                 r"(\d+) credit hours\s*\.",
    "a_third_plus": r"third and subsequent semesters?(?:\(s\))? a maximum of (\d+) credit hours",
    "a_cgpa_max": r"may allow to enroll (?:a )?maximum (?:of )?(\d+) credit hours only if "
                  r"student\S* CGPA is (?:≥|>=|greater than or equal to)\s*([\d.]+)",
    "b_per_sem": r"A maximum of (\d+) credit hours per semester are allowed",
    "b_special": r"special circumstances,? (?:the )?university may allow to enroll (?:a )?"
                 r"maximum (?:of )?(\d+) credit hours",
    "b_last_two": r"one more course beyond the specified credit hours limit in the last two "
                  r"semesters.*?CGPA is ([\d.]+) or above",
    "deficiency": r"Deficiency courses will enroll a maximum of (\d+) credit hours for the 1st "
                  r"and 2nd semester",
    "final": r"Final semester,? maximum (\d+) credit hours (?:of coursework )?are allowed",
    "last_two_max": r"For last two semesters,? maximum (\d+) credit hours of coursework are "
                    r"allowed in addition to Project/Internship",
    "minimum": r"minimum of 0?(\d+)[- ]credit hours",
}


def _load_rules(md: str) -> tuple[str | None, dict]:
    m = _LIMIT_BLOCK.search(md)
    block = m.group(0).strip() if m else None
    found: dict = {}
    if block:
        for k, rx in _RX.items():
            mm = re.search(rx, block, re.I | re.S)
            if mm:
                found[k] = [float(x) if "." in x else int(x) for x in mm.groups()]
    return block, found


def _compute_load(found: dict, sem: int, cgpa: float, final: bool,
                  total_semesters: int | None) -> dict | None:
    expl: list[str] = []
    first = found.get("a_first_two") or found.get("a_first")
    second = found.get("a_first_two") or found.get("a_second")
    last_two = final or (total_semesters is not None and sem >= total_semesters - 1)
    if first and second and "a_third_plus" in found:
        if final and "final" in found:
            std = possible = found["final"][0]
            expl.append(f"Final semester: up to {std} credit hours of coursework in addition "
                        "to Project/Internship, if all degree requirements finish that semester.")
        elif last_two and "last_two_max" in found:
            std = possible = found["last_two_max"][0]
            expl.append(f"Last two semesters: up to {std} credit hours of coursework in addition "
                        "to Project/Internship, if all degree requirements finish then.")
        elif sem <= 2:
            std = possible = (first if sem == 1 else second)[0]
            expl.append(f"Semester {sem}: maximum {std} credit hours.")
            if sem == 1:
                expl.append("First-semester students get pre-selected courses.")
        else:
            std = possible = found["a_third_plus"][0]
            expl.append(f"Semester 3 onward: maximum {std} credit hours.")
            if "a_cgpa_max" in found:
                cmax, ccg = found["a_cgpa_max"]
                if cgpa >= ccg:
                    possible = cmax
                    expl.append(f"Your CGPA {cgpa} is >= {ccg}, so the University MAY allow up "
                                f"to {cmax} 'in certain cases' (not automatic).")
                else:
                    expl.append(f"Up to {cmax} is possible only with CGPA >= {ccg}; yours is "
                                f"{cgpa}.")
            elif "a_certain" in found:
                possible = found["a_certain"][0]
                expl.append(f"In certain cases the University may allow up to {possible} "
                            "(University discretion).")
            if "last_two_max" in found and total_semesters is None and sem >= 6:
                expl.append(f"If this is one of your last two semesters, up to "
                            f"{found['last_two_max'][0]} plus Project/Internship may apply "
                            "(pass total_semesters to check).")
        return {"template": "semester-banded (15/18/21 style)", "max_standard": std,
                "max_possible": possible, "explanation": expl}
    if "b_per_sem" in found:
        std = found["b_per_sem"][0]
        possible = std
        if final and "final" in found:
            std = possible = found["final"][0]
            expl.append(f"Final semester: up to {std} credit hours of coursework in addition "
                        "to Project/Internship, if all degree requirements finish that semester.")
        else:
            expl.append(f"Maximum {std} credit hours per semester.")
            if "b_special" in found:
                possible = found["b_special"][0]
                expl.append(f"In special circumstances the University may allow up to "
                            f"{possible} (University discretion, not tied to CGPA).")
        if "b_last_two" in found:
            ccg = found["b_last_two"][0]
            if last_two:
                if cgpa >= ccg:
                    expl.append(f"In your last two semesters you may request ONE more course "
                                f"beyond the limit (to the Rector through the Dean) since your "
                                f"CGPA is >= {ccg}.")
                else:
                    expl.append(f"One extra course in the last two semesters needs CGPA >= "
                                f"{ccg}; yours is {cgpa}.")
            else:
                expl.append(f"(The one-extra-course option with CGPA >= {ccg} applies only "
                            "to the last two semesters.)")
        if "deficiency" in found and sem <= 2:
            expl.append(f"Students admitted on deficiency courses: maximum "
                        f"{found['deficiency'][0]} in semesters 1-2.")
        if "minimum" in found:
            expl.append(f"Minimum load follows the study scheme; in special circumstances it "
                        f"can be {found['minimum'][0]} credit hours.")
        return {"template": "per-semester cap (18 + special 21 style)", "max_standard": std,
                "max_possible": possible, "explanation": expl}
    return None


@mcp.tool()
def vu_course_load_check(semester_number: int, cgpa: float, is_final_semester: bool = False,
                         program: str | None = None, batch: str | None = None,
                         total_semesters: int | None = None) -> dict:
    """How many credit hours may a student enrol in this semester? Reads the
    credit-hour limits from the student's own program page in the handbook.

    Example: semester_number=5, cgpa=3.8, program="BS Software Engineering",
    batch="Fall 2024".

    Args:
        semester_number: Semester being enrolled (1, 2, 3...).
        cgpa: Current CGPA out of 4.
        is_final_semester: True if all degree requirements finish this semester.
        program: The student's program (limits differ by program).
        batch: The student's batch, e.g. "2404" / "Fall 2024" (limits differ by batch).
        total_semesters: Optional program length, to apply last-two-semester rules.

    Guidance only: the LMS Course Selection page shows the real "Maximum
    Credits Limit" and enforces it. Without program/batch, answers for each
    matching version are returned.
    """
    if not 1 <= int(semester_number) <= 20:
        raise ValueError("semester_number must be 1-20")
    if not 0 <= float(cgpa) <= 4:
        raise ValueError("cgpa must be between 0 and 4")
    sem, cg = int(semester_number), float(cgpa)
    disclaimer = ("Guidance from the handbook snapshot. The LMS Course Selection page shows your "
                  "actual 'Maximum Credits Limit' and enforces the real rules.")
    if not program or not str(program).strip():
        return {
            "computed": False,
            "note": ("Credit-hour limits differ by program and batch in the VU handbook (e.g. "
                     "BS Software Engineering older batches: 15 in semesters 1-2 then 18; "
                     "w.e.f. Batch 2104: 18 per semester). Ask the student's program and batch "
                     "and call again with program= and batch=."),
            "example_call": {"semester_number": sem, "cgpa": cg,
                             "program": "BS Software Engineering", "batch": "Fall 2024"},
            "disclaimer": disclaimer, "data_last_updated": _data_last_updated(),
        }
    filt = _program_filter(program, batch)
    ids = sorted(filt["selected_ids"]) if filt["selected_ids"] else sorted(filt["matched_ids"])
    if not ids:
        return {"computed": False, "note": filt["note"], "disclaimer": disclaimer,
                "data_last_updated": _data_last_updated()}
    answers = []
    with _db() as con:
        for pid in ids[:8]:
            row = con.execute("SELECT id, title, breadcrumb, url, markdown FROM pages "
                              "WHERE id = ?", (pid,)).fetchone()
            block, found = _load_rules(row["markdown"] or "")
            res = _compute_load(found, sem, cg, bool(is_final_semester), total_semesters)
            cat = filt["by_id"].get(pid, {})
            item = {
                "program_page": {"page_id": row["id"], "title": row["title"],
                                 "version": row["breadcrumb"].split(" > ")[-1],
                                 "effective_from_batch": cat.get("effective_from_batch")},
                "source_url": row["url"],
                "computed": res is not None,
            }
            if res:
                item.update(res)
            elif block and re.search(r"\d+\s*credit hours", block, re.I):
                item["note"] = ("The limits on this page use wording this tool does not parse; "
                                "read rule_text and answer from it.")
            else:
                item["note"] = ("This page's credit-hour limits are not in the snapshot text "
                                "(probably an image or table on the live page). Open source_url "
                                "or check 'Maximum Credits Limit' on the LMS Course Selection "
                                "page.")
            item["rule_text"] = (block or "")[:1500] or None
            answers.append(item)
    out = {
        "semester_number": sem, "cgpa": cg, "is_final_semester": bool(is_final_semester),
        "filters": filt["public"],
        "computed": any(a["computed"] for a in answers),
        "answers": answers,
        "note": filt["note"],
        "disclaimer": disclaimer,
        "data_last_updated": _data_last_updated(),
    }
    return out


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(request):
    """Liveness probe for HTTP deployments (no DB access, cheap)."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "source": SOURCE_NAME})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcp_server.py",
        description="VU Handbook MCP server. stdio by default; --http for Streamable HTTP.",
    )
    parser.add_argument("--http", action="store_true",
                        help="serve over Streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind address (use 0.0.0.0 to expose; then pass --allow-host)")
    parser.add_argument("--port", type=int, default=8000,
                        help="HTTP port (default: 8000)")
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                        help="extra allowed Host header, e.g. 'vumcp.example.com' or '10.0.0.5:*' (repeatable)")
    parser.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                        help="extra allowed Origin for browser clients, e.g. 'https://app.example.com' (repeatable)")
    parser.add_argument("--stateless", action="store_true",
                        help="stateless HTTP mode: no session ids (for multi-instance deployments)")
    args = parser.parse_args(argv)

    if not args.http:
        mcp.run(transport="stdio")
        return

    # DNS-rebinding protection stays ON: start from the SDK's localhost-only
    # policy and add only what the operator explicitly allows.
    security = mcp.settings.transport_security
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=security.enable_dns_rebinding_protection,
        allowed_hosts=list(dict.fromkeys([*security.allowed_hosts, *args.allow_host])),
        allowed_origins=list(dict.fromkeys([*security.allowed_origins, *args.allow_origin])),
    )
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.stateless_http = args.stateless

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_host:
        print(
            "warning: binding to non-loopback address without --allow-host; "
            "requests with remote Host headers will be rejected "
            "(DNS-rebinding protection). Add --allow-host <your-hostname>.",
            file=sys.stderr,
        )

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
