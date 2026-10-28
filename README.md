# vu-handbook-mcp

**An MCP server that gives AI assistants citable, read-only access to the Virtual University of Pakistan Student Handbook and a structured guide to the VU LMS.**

[![Secret scan](https://github.com/imawais-engineer/vu-handbook-mcp/actions/workflows/security-scan.yml/badge.svg)](https://github.com/imawais-engineer/vu-handbook-mcp/actions/workflows/security-scan.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![MCP](https://img.shields.io/badge/MCP-stdio%20%7C%20Streamable%20HTTP-black)

Built by [Muhammad Awais](https://imawais.engineer) · Aspiring Forward Deployed AI Engineer, currently AI Automation Engineer.

## Overview

Students ask the same policy questions every semester: semester freeze, unfair means, grade improvement, fee challans, where a form lives in the LMS. The answers exist in the official handbook (<https://handbook.vu.edu.pk/>), but it is a legacy ASP.NET frameset site that AI assistants cannot read reliably.

`vu-handbook-mcp` crawls the full handbook into a local SQLite FTS5 snapshot and exposes it through the [Model Context Protocol](https://modelcontextprotocol.io), so Claude, Cursor, ChatGPT or any agent framework can:

- **search** 310 handbook pages with ranked, cited results (URL + breadcrumb),
- **read** any page in full with parent/child/sibling navigation,
- **browse** the table of contents before answering,
- **locate** anything in the VU LMS portal (Grade Book, Account Book, Course Selection, Student Services) via a reverse-engineered LMS guide,
- **refuse to guess**: empty results return an explicit escalate-to-VU-Support note.

It serves public reference data only: no credentials, no student records, read-only database.

| | |
|---|---|
| Corpus | 319 TOC nodes, 310 pages, ~1.09 M characters, 5 levels deep, + VU Plagiarism Policy PDF |
| Search | SQLite FTS5, title-weighted BM25, 60 % term-coverage precision filter |
| Retrieval eval | **12/12** expected pages in top-3 on real student questions |
| Tests | 81 stdio protocol checks · 21 HTTP transport checks · 44 pytest unit tests · ingest parity tests (Python + JS) |
| Transports | stdio and Streamable HTTP (DNS-rebinding protection, `/healthz`, Docker) |

## Tools at a glance

| Tool | Purpose | Use it for |
|---|---|---|
| `vu_ask(question, program=None, batch=None, limit=3)` | **Start here.** One call over both corpora: best pages with trimmed, cited excerpts, the next tool to call, and answering rules | any student question |
| `vu_handbook_search(query, limit=8, program=None, batch=None)` | Ranked full-text search with snippets and citations; optional program/batch filter picks the right "w.e.f Batch" version | rules, eligibility, exam policy |
| `vu_key_dates_and_fees(topic, program=None)` | Cited fee amounts and deadline rules (freeze, rechecking, date sheet, readmission, refunds, tuition table rows) | "how much / by when" |
| `vu_gpa_calculator(courses, previous_cgpa=None, previous_credit_hours=None)` | SGPA/CGPA using the grading scheme parsed from Academic Rules and Regulations §20–21 | GPA from grades or marks |
| `vu_course_load_check(semester_number, cgpa, is_final_semester=False, program=None, batch=None)` | Credit-hour limit read from the student's own program page (version chosen by batch) | "how many credit hours can I take" |
| `vu_handbook_get(page)` | Full page by id, title or URL, with navigation |
| `vu_handbook_contents(path="", depth=2)` | Browse the handbook table of contents |
| `vu_lms_contents()` | Map of every VU LMS section |
| `vu_lms_get(section)` | One LMS section in full |
| `vu_lms_search(query, limit=5)` | Search the LMS guide and operational notes |

Old tools keep their exact behaviour when the new optional arguments are not
passed (a test compares them against the previous release). Rules-type
outputs carry `data_last_updated` (newest crawl timestamp of the snapshot).

Resources: `vu-handbook://toc`, `vu-handbook://page/{page_id}`, `vu-handbook://stats`, `vu-lms://sections`, `vu-lms://guide`. Details below.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/imawais-engineer/vu-handbook-mcp.git
cd vu-handbook-mcp
pip install .                 # installs the `vu-handbook-mcp` command (pins mcp<2)
vu-handbook-mcp               # stdio transport
vu-handbook-mcp --http        # Streamable HTTP on 127.0.0.1:8000/mcp
```

Without installing: `pip install "mcp>=1.13,<2" && python3 mcp_server.py`.
The snapshot in `data/` ships with the repo; point elsewhere with `VU_HANDBOOK_DATA=/path/to/data`.

> Written against the **mcp 1.x API** (verified on 1.30.0). mcp 2.x renamed `FastMCP`, so every entry point pins `<2`.

## Connect a client

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "vu-handbook": {
      "command": "python3",
      "args": ["/absolute/path/to/vu-handbook-mcp/mcp_server.py"]
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add vu-handbook -- python3 /absolute/path/to/vu-handbook-mcp/mcp_server.py
```

**Cursor** (`~/.cursor/mcp.json` or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "vu-handbook": {
      "command": "python3",
      "args": ["/absolute/path/to/vu-handbook-mcp/mcp_server.py"]
    }
  }
}
```

**ChatGPT / remote clients:** run with `--http` behind HTTPS (Docker image below) and register `https://your-host/mcp` as a remote MCP server.

## Usage

Example prompts once connected:

- "According to the VU handbook, how do I apply for a semester freeze? Cite the page."
- "What counts as unfair means in VU exams and what are the penalties?"
- "Where in the VU LMS do I download my fee challan?"

From code:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(command="python3", args=["mcp_server.py"])
async with stdio_client(params) as (r, w):
    async with ClientSession(r, w) as s:
        await s.initialize()
        hits = await s.call_tool("vu_handbook_search",
                                 {"query": "how do I apply for semester freeze"})
        page = await s.call_tool("vu_handbook_get", {"page": 64})
```

## Deploy (Azure VM, HTTPS, token auth)

A ready deployment kit lives in [`deploy/`](deploy/): Docker + Caddy (automatic HTTPS), bearer-token auth and rate limiting (`deploy/serve.py`), cloud-init, az CLI scripts, systemd alternative. Step-by-step guide with costs, smoke test and client configs: [`deploy/DEPLOY_AZURE.md`](deploy/DEPLOY_AZURE.md).

## Run over HTTP (production)

```bash
python3 mcp_server.py --http --host 0.0.0.0 --port 8000 --allow-host vumcp.example.com
```

| Flag | Meaning |
|---|---|
| `--http` | Streamable HTTP transport instead of stdio |
| `--host` / `--port` | bind address (loopback by default) |
| `--allow-host HOST` | extra allowed `Host` header — **required** for non-loopback binds (repeatable, `host:*` wildcards work) |
| `--allow-origin ORIGIN` | extra allowed `Origin` for browser-based clients (repeatable) |
| `--stateless` | no session ids, for multi-instance / load-balanced deployments |

- **Health probe:** `GET /healthz` → `{"status": "ok"}` (no DB access).
- **DNS-rebinding protection is ON:** requests with a spoofed `Host` are
  rejected with **421 Misdirected Request**, a foreign `Origin` with **403**.
  Loopback (`127.0.0.1`, `localhost`, `[::1]`) always works; anything else
  must be granted via `--allow-host` / `--allow-origin`.
- **Read-only:** the server opens SQLite with `mode=ro`, so an agent bug can
  never mutate the snapshot — only `tools/crawl_handbook.py` writes.
- **Concurrent sessions are safe:** verified by `tests/test_http.py`
  (8 simultaneous sessions, distinct session ids, shared read-only DB).

Connect from code:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("http://127.0.0.1:8000/mcp") as (r, w, sid):
    async with ClientSession(r, w) as s:
        await s.initialize()
        hits = await s.call_tool("vu_handbook_search",
                                 {"query": "how do I apply for semester freeze"})
```

Docker:

```bash
docker build -t vu-handbook-mcp .
docker run -p 8000:8000 vu-handbook-mcp --allow-host vumcp.example.com
```

The image entrypoint is `mcp_server.py`, so any flag above can be appended.
A `HEALTHCHECK` polls `/healthz`. The image ships the snapshot but not the
crawler — refresh on the host with `tools/refresh.py`, then rebuild.

## Paste ingestion (`ingest/`)

A companion pipeline for agents built on top of this server (see `docs/VISION.md`): a student copies a logged-in VULMS page and pastes it; the pipeline cleans, structures and session-stores it. No credentials are ever requested or handled, and the MCP server never serves live student data.

```bash
python3 ingest/ingest.py --session s1 paste.txt     # -> vu-paste/1 JSON (+ stored)
node ingest/ingest.js  --session s1 paste.txt       # identical output, JS twin
python3 ingest/tests/test_ingest.py                 # 39 checks
node    ingest/tests/test_ingest_js.js              # 47 cross-language parity checks
```

Cleaning removes HTML/ViewState junk, menu boilerplate (`boilerplate.txt` —
calibrate with real pastes), mojibake, smart punctuation and whitespace
noise, while preserving inner content (no course code is ever lost — tested).
Structuring emits text, blocks, tables and lightweight facts (course codes,
grades, credits, CGPA, dates) for agents to reason over. Student pastes land
in `sessions/` (gitignored).

## Tools

### `vu_handbook_contents(path="", depth=2)`
Navigate the table of contents.

- `path` — `""` (top level), a node id (`"11"`), a full URL, or a breadcrumb
  such as `"Home > Examinations Department"` (case-insensitive, `/` also works).
- `depth` — how many levels of children to return (1–6).

Returns each child with `type: "page"` (has content) or `"branch"`, plus
`child_count`. Use it to discover *what the handbook covers* before searching.

### `vu_handbook_search(query, limit=8)`
Full-text search across all 310 pages.

Returns ranked matches with `id`, `title`, `breadcrumb`, `url`, `snippet`,
`matched_terms` and `rank`. Empty `results` carries an explicit
"not covered — escalate to VU Support System" note so an agent never guesses.

### `vu_handbook_get(page)`
Read one page in full. `page` accepts an id (`20`), an exact or unique title
(`"Acts of Unfair Means"`), or a handbook URL.

Returns `title`, `breadcrumb`, `url`, `headings`, `content` (markdown,
capped at 40 000 chars with `truncated`), and `navigation` (`parent`,
`children`, `siblings`) so follow-up hops need no extra lookup.

An ambiguous title raises an error listing candidate ids rather than silently
returning the wrong page.

### `vu_handbook_search(..., program=None, batch=None)`
`program` accepts names and aliases: `"BS Software Engineering"`, `"BSSE"`,
`"BSCS"`, `"BS-Lateral IT"`, `"ADP Computer Science"`, `"MSCS"`, `"MBA"`.
`batch` accepts a VU batch code (`"2404"`; `YY02` = Spring, `YY04` = Fall),
`"Fall 2024"`, `"S25"` or a year (`"2024"` = both terms). The program's pages
rank first; the version shown as `selected` is the newest
`[w.e.f Batch NNNN]` page that started on or before the batch (unlabelled
pages are the oldest version). Each result gets `applies_to`; `variants`
lists every version and `note` says when the choice is still ambiguous.

### `vu_ask(question, program=None, batch=None, limit=3)`
Searches the handbook and the LMS guide, routes by intent ("where/click" →
LMS, rules → handbook), drops other-batch versions once a batch is known, and
returns per result: `source`, `title`, `breadcrumb`, `url`, an `excerpt`
(the most relevant paragraphs, ≤ ~1500 chars) and `read_more` (the exact
`vu_handbook_get` / `vu_lms_get` call). Also `suggested_tools` (calculator /
fees / load check when the question needs them) and `how_to_answer`.

### `vu_key_dates_and_fees(topic, program=None)`
Topic-mapped extraction over the fee pages (Fee Structure Local/Overseas,
Schedule of Other Charges, installments, blocking) and deadline-rule pages
(freeze/unfreeze, rechecking, rescheduling, date sheet, FYP late submission,
readmission, security refund, add/drop in Academic Rules §9–10). Each fact is
cited. The handbook states deadlines relative to the Academic Calendar; this
semester's actual dates are not in the data and the tool says so.

### `vu_gpa_calculator(courses, ...)`
`courses` is a list of `{grade, credit_hours}` or `{marks, credit_hours}`.
VU grade points depend on marks (B+ = 3.33–3.65), so letters alone give an
SGPA/CGPA *range*; marks give the exact value via the §20.1 formula (tested
against every row of the §20.2 table).

### `vu_course_load_check(semester_number, cgpa, ...)`
Reads the "credit hour limits" block of the program page chosen by
program + batch and applies it (two wording templates are parsed, covering 87
of 215 program pages). Otherwise it returns the verbatim rule text, or says
the limits are missing from the snapshot (44 pages, mostly
`[w.e.f Batch 2504]`, where the live page shows them as an image/table).
Guidance only: the LMS Course Selection page shows the real limit.

## LMS tools

### `vu_lms_contents()`
Map of the VULMS portal: every file, section and subsection (Home Dashboard,
Course Pages, Grade Book tabs, Account Book challans, Student Services forms,
Support System categories, …). Call before answering "where do I find X".

### `vu_lms_get(section)`
Read one section in full. `section` accepts a title (`"Grade Book"`), a unique
substring, or a full vulms URL (matches the section documenting it). Ambiguous
titles raise an error listing candidates.

### `vu_lms_search(query, limit=5)`
Keyword search across the LMS guide + operational notes (deadline windows,
date-sheet mechanics). Empty results carry an explicit "do not invent LMS
procedures" note.

## Resources

| URI | Contents |
|---|---|
| `vu-handbook://toc` | Complete nested table of contents |
| `vu-handbook://page/{page_id}` | Raw markdown of one page |
| `vu-handbook://stats` | Snapshot coverage statistics |
| `vu-lms://sections` | Every LMS guide section + subsections |
| `vu-lms://guide` | Full LMS guide + operational notes as markdown |

## Refreshing the snapshot (safe)

```bash
python3 tools/refresh.py                # recrawl -> gates -> atomic swap
```

The live site can change or break at any time, so a refresh never overwrites
a working snapshot directly:

1. crawl into `data/.staging/` (fresh TOC + all pages, 0 fetch failures required)
2. sanity gates — file presence, page count (>= 290), char count (>= 500k)
3. protocol gate — `tests/test_mcp.py` (73 checks) run against **staging**
4. retrieval gate — `tests/eval_queries.py`, at least **10/12** expected pages in top-3
5. only then swap — current files copied to `data/.last-good/`, then
   atomically replaced (`os.replace`)

Any failing gate keeps `data/` untouched and exits 1. Verified both paths:
a forced gate failure leaves the live snapshot byte-identical; a real
end-to-end refresh passed all gates and swapped cleanly.

Useful flags: `--min-eval 11`, `--min-pages 300`, `--skip-crawl`
(re-run gates on existing staging), `--keep-staging`, `--workers N`.

Manual / one-off runs:

```bash
pip install --target .deps pypdf        # PDF text extraction (workspace-local)
python3 tools/crawl_handbook.py --refresh-toc --workers 4
python3 tests/test_mcp.py               # 73 end-to-end protocol checks (stdio)
python3 tests/test_http.py              # 21 HTTP transport checks (spawns server)
python3 tests/eval_queries.py           # retrieval quality on real questions
```

Outputs land in `data/`:

```
data/toc.json       nested TOC (navigation)
data/pages.jsonl    raw records (portable dump)
data/handbook.db    SQLite + FTS5 index (search + fetch)
```

Point the server at another directory with `VU_HANDBOOK_DATA=/path/to/data`.

## How the crawl works

`handbook.vu.edu.pk` is a 2003-era ASP.NET frameset site with an invalid TLS
certificate. Three things are worth knowing:

1. **The TOC is server-side collapsed.** Only ~23 of 319 nodes render on first
   load; the rest are hydrated by `__doPostBack('PermissionTree', 't<path>')`.
   `tools/vu_handbook_client.py` replays those postbacks until every branch is
   expanded, then parses the final client-side render, tracking
   `PermissionTreen{N}Nodes` containers to recover the exact hierarchy. No
   `__VIEWSTATE` binary decoding required.
2. **Three encodings live in one corpus.** Most pages are raw windows-1252;
   some are UTF-8 carrying latin-1-decoded CP1252 smart quotes (C1 controls)
   while still *declaring* `windows-1252`; the Course Selection pages are
   UTF-16LE with a BOM. Trusting the declared charset double-mangles the second
   group into `Â“` and turns the third into ~59 000 NUL bytes per page.
   `extract.decode_page()` detects BOM → UTF-16, else UTF-8, else CP1252, then
   maps surviving C1 controls to their CP1252 glyphs.
3. **Invalid HTML needs a tolerant renderer.** Tables wrapped in `<b>`, lists
   inside `<p>`, and Word `if !supportLists` markers. `tools/extract.py` walks
   the tree in document order, promotes the grey section-header tables to `##`
   headings, and scrubs export leftovers.

## Search ranking

`OR` over the query's significant terms (stop words removed) for recall, BM25
with title > breadcrumb > headings > body weighting for ordering, then a
precision filter: a page must contain at least **60% of the query's
significant terms** (`ceil(3n/5)`).

Measured on `tests/eval_queries.py`, 12 realistic student questions:

```
retrieval: 12/12 expected pages in top-3 (100%)
```

Measured failure mode: a nonsense query (`zzzqqqxyz spaceship launch code`)
returns 0 results plus the escalation note rather than a lucky single hit.

## Known limitations

- **Lexical, not semantic.** Retrieval matches words, not meaning. A question
  phrased very differently from the handbook's wording can miss — e.g. the
  handbook says "awarded an F grade… repeat the course" and never says
  "fail". Add embeddings later if that matters.
- **Static snapshot.** The handbook changes; re-run the crawler to refresh.
  VU also publishes dated announcements and FAQs outside the handbook — those
  are separate sources for later agents.
- **Informational only.** This server has no access to student records and
  performs no actions. Account-specific questions must be escalated to VU's
  official Support System (<https://www.vu.edu.pk/supportsystem/>).

## Layout

```
mcp_server.py                 the MCP server (stdio + Streamable HTTP)
tools/refresh.py              gated refresh: staging -> tests -> atomic swap
tools/vu_handbook_client.py   TOC expansion + page fetch (postbacks, retries)
tools/extract.py              HTML/PDF -> markdown, encoding handling
tools/crawl_handbook.py       build data/ snapshot
tests/test_mcp.py             end-to-end protocol tests, stdio (73 checks)
tests/test_http.py            HTTP transport tests (21 checks)
tests/eval_queries.py         retrieval quality eval (12 questions)
pyproject.toml                 packaging + 'vu-handbook-mcp' entry point
Dockerfile                     production image (Streamable HTTP)
data/                         generated snapshot (refresh: data/.last-good/)
docs/                         product vision, VU LMS guide, operational notes
ingest/                       paste ingestion: clean -> structure -> store
```

## License

No license has been granted yet; all rights reserved by the author. Handbook content belongs to the Virtual University of Pakistan and is included as a snapshot for reference and citation.

---

[imawais.engineer](https://imawais.engineer) · [GitHub](https://github.com/imawais-engineer)
