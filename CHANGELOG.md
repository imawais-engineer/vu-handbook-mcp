# Changelog

## 0.2.0 — 2026-10-28

Make the server easy for an AI model to route, and answer common questions in
one call.

- **Routing guidance**: rewrote the server `instructions` as a decision guide
  (rules → handbook, where-to-click → LMS, whole question → `vu_ask`,
  fees/deadlines, GPA, credit-hour limits) and every tool description now
  says when to use it, with example calls.
- **New `vu_ask`**: one call over the handbook and the LMS guide; returns the
  best pages with trimmed, relevant excerpts (≤ ~1500 chars), citations, the
  exact follow-up call, suggested tools and answering rules.
- **Program/batch filter** on `vu_handbook_search` (`program=`, `batch=`):
  aliases (BSSE, BSCS, BS-Lateral, ADP, MSCS…), VU batch codes (`2404`,
  `Fall 2024`, `2024`), picks the right `[w.e.f Batch …]` version and reports
  remaining ambiguity in `note` / `variants`. Unchanged output when unused.
- **New `vu_key_dates_and_fees`**: cited fee amounts and deadline rules;
  states that current-semester calendar dates are not in the data.
- **New `vu_gpa_calculator`**: grading scheme parsed from Academic Rules and
  Regulations §20–21 (letters → range, marks → exact).
- **New `vu_course_load_check`**: credit-hour limits read from the student's
  program page for their batch; honest when a page's limits are missing.
- `data_last_updated` in rules-type outputs and the stats resource.
- Docker: root image now ships `docs/lms/`; deploy image ships
  `data/pages.jsonl` (crawl timestamps).
- Tests: `tests/test_tools.py` (pytest, incl. backward-compatibility checks
  against the previous release) and `tests/test_legacy_scripts.py`, which
  runs the existing stdio/HTTP/eval scripts under pytest.
