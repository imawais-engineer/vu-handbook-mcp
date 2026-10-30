"""Unit tests for the routing-oriented tools (vu_ask, filters, fees, GPA, load)
and backward compatibility of the original six tools."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcp_server as m  # noqa: E402


# ------------------------------------------------------------------ routing
def test_instructions_route_every_tool():
    text = m.mcp.instructions
    for name in ("vu_ask", "vu_handbook_search", "vu_handbook_get", "vu_lms_search",
                 "vu_lms_get", "vu_key_dates_and_fees", "vu_gpa_calculator",
                 "vu_course_load_check"):
        assert name in text, name


@pytest.mark.anyio
async def test_tool_descriptions_have_examples():
    tools = await m.mcp.list_tools()
    assert len(tools) == 10
    for t in tools:
        assert t.description and len(t.description) > 120, t.name
        assert "e.g." in t.description or "Example" in t.description, t.name


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ------------------------------------------------------------------ backward compat
def _old_module():
    """The server as committed before this change (git HEAD of main), if available."""
    if not shutil.which("git"):
        pytest.skip("git not available")
    try:
        src = subprocess.run(["git", "show", "main:mcp_server.py"], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout
    except subprocess.CalledProcessError:
        pytest.skip("main branch not available")
    if "def vu_ask" in src:
        pytest.skip("main already contains the new tools")
    path = ROOT / ".pytest_old_mcp_server.py"
    path.write_text(src, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("old_mcp_server", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        path.unlink(missing_ok=True)
    return mod


@pytest.mark.parametrize("query", [
    "course selection software engineering", "semester freeze deadline",
    "using mobile phone in exam is unfair means", "fee voucher: (2024) AND \"x\"* OR {}",
    "zzzqqqxyz spaceship launch code", "how many credit hours can I take",
])
def test_search_without_filters_is_unchanged(query):
    old = _old_module()
    assert m.vu_handbook_search(query, 8) == old.vu_handbook_search(query, 8)


def test_other_original_tools_unchanged():
    old = _old_module()
    assert m.vu_handbook_get(20) == old.vu_handbook_get(20)
    assert m.vu_handbook_contents("Home > Examinations Department", 2) == \
        old.vu_handbook_contents("Home > Examinations Department", 2)
    assert m.vu_lms_search("where is semester freeze", 3) == \
        old.vu_lms_search("where is semester freeze", 3)
    assert m.vu_lms_get("Grade Book") == old.vu_lms_get("Grade Book")
    assert m.vu_lms_contents() == old.vu_lms_contents()


# ------------------------------------------------------------------ program / batch filter
@pytest.mark.parametrize("raw,codes", [
    ("2404", [2404]), ("Fall 2024", [2404]), ("spring 2025", [2502]), ("F24", [2404]),
    ("2024", [2402, 2404]), ("2104", [2104]), (None, []),
])
def test_parse_batch(raw, codes):
    assert m._parse_batch(raw)[0] == codes


@pytest.mark.parametrize("program,level,subject", [
    ("BS Software Engineering", "BS", "software engineering"),
    ("BSSE", "BS", "software engineering"),
    ("bs-lateral se", "BS-Lateral", "software engineering"),
    ("ADP Computer Science", "Associate Degree", "computer science"),
    ("MSCS", "MS", "computer science"),
])
def test_parse_program(program, level, subject):
    p = m._parse_program(program)
    assert p["level"] == level and " ".join(p["subject"]) == subject


def test_search_program_and_batch_picks_2104_version():
    r = m.vu_handbook_search("course selection software engineering", 5,
                             program="BS Software Engineering", batch="2024")
    assert r["results"][0]["id"] == 159  # BS [w.e.f Batch 2104- Fall 2021]
    assert r["results"][0]["applies_to"]["filter_match"] == "selected"
    assert r["filters"]["selected_pages"] == [159]
    ids = [v["id"] for v in r["variants"]]
    assert set(ids) == {146, 159, 177}
    assert "data_last_updated" in r


def test_search_program_without_batch_lists_variants_in_note():
    r = m.vu_handbook_search("course selection", 5, program="BSSE")
    top = {x["id"] for x in r["results"][:3]}
    assert top == {146, 159, 177}
    assert "batch" in r["note"].lower()


def test_search_batch_2504_and_lateral():
    r = m.vu_handbook_search("course selection software engineering", 3,
                             program="BS Software Engineering", batch="Fall 2025")
    assert r["results"][0]["id"] == 177
    r = m.vu_handbook_search("course selection software engineering", 3, program="BS-Lateral SE")
    assert r["results"][0]["id"] == 195


def test_unknown_program_is_reported_not_silently_applied():
    r = m.vu_handbook_search("course selection", 3, program="zzz nothing")
    assert "not recognised" in r["note"]
    assert r["count"] > 0


# ------------------------------------------------------------------ vu_ask
def test_vu_ask_course_selection():
    r = m.vu_ask("course selection software engineering", "BS Software Engineering", "2024")
    first = r["results"][0]
    assert first["source"] == "handbook" and first["page_id"] == 159
    assert first["read_more"] == {"tool": "vu_handbook_get", "args": {"page": 159}}
    assert len(first["excerpt"]) <= m.EXCERPT_CHARS + 60
    # wrong-batch versions are not returned as answers, only listed as variants
    assert all(x.get("page_id") not in (146, 177) for x in r["results"])
    assert "cite" in r["how_to_answer"].lower() and "live student data" in r["how_to_answer"].lower()
    assert r["data_last_updated"]


def test_vu_ask_lms_question_routes_to_lms():
    r = m.vu_ask("where is the grade book in LMS")
    first = r["results"][0]
    assert first["source"] == "lms" and first["title"] == "Grade Book"
    assert first["url"] == "https://vulms.vu.edu.pk/GradeBook/GradeBook.aspx"
    assert first["read_more"]["tool"] == "vu_lms_get"


def test_vu_ask_freeze_and_suggestions():
    r = m.vu_ask("semester freeze deadline")
    assert r["results"][0]["title"] == "Semester Freeze"
    assert "last date of course selection" in r["results"][0]["excerpt"]
    assert any(t["tool"] == "vu_key_dates_and_fees" for t in r["suggested_tools"])


def test_vu_ask_suggests_calculators():
    r = m.vu_ask("GPA for grades A,B+,B in 3-credit courses")
    assert any(t["tool"] == "vu_gpa_calculator" for t in r["suggested_tools"])
    r = m.vu_ask("how many credit hours can I take in semester 5 with CGPA 3.8")
    assert any(t["tool"] == "vu_course_load_check" for t in r["suggested_tools"])
    assert "program" in (r["note"] or "")


def test_excerpt_is_trimmed_and_relevant():
    md = m.vu_handbook_get(312)["content"]
    ex = m._excerpt(md, ["grading", "scheme"])
    assert len(ex) <= m.EXCERPT_CHARS + 60
    assert "GRADING SCHEME" in ex


# ------------------------------------------------------------------ dates & fees
def test_fees_semester_freeze():
    r = m.vu_key_dates_and_fees("semester freeze deadline")
    facts = " ".join(f["fact"] for f in r["facts"])
    assert "on or before the last date of course selection" in facts
    assert "Semester Freeze: Rs. 2,000 (local) / US$ 50 (overseas)" in facts
    assert all(f.get("url") or f["source"] != "handbook" for f in r["facts"])
    assert "not in this data" in r["calendar_note"]


def test_fees_tuition_row_for_level():
    r = m.vu_key_dates_and_fees("tuition fee per credit hour", program="BS")
    facts = [f["fact"] for f in r["facts"]]
    assert any(f.startswith("BS: ") and "Tuition Fee per credit hour = 1,000" in f for f in facts)
    assert any("w.e.f. Fall 2023" in f for f in facts)


def test_fees_unknown_topic_says_so():
    r = m.vu_key_dates_and_fees("hostel mess charges")
    assert r["count"] == 0 and "do not guess" in r["note"]


# ------------------------------------------------------------------ GPA
def test_grading_scale_parsed_from_handbook():
    g = m._grading()
    assert g["page_id"] == 312
    assert g["scale"]["B+"]["grade_points_min"] == 3.33
    assert g["scale"]["B+"]["grade_points_max"] == 3.65
    assert g["scale"]["A"]["grade_points_min"] == 4.0


def test_marks_formula_matches_handbook_table():
    md = m.vu_handbook_get(312)["content"]
    rows = re.findall(r"\|\s*\d+\s*\|\s*([\d.]+)%\s*\|\s*([A-DF][+-]?)\s*\|\s*([\d.]+)\s*\|", md)
    assert len(rows) >= 40
    for score, letter, gp in rows:
        got_letter, got = m._gp_from_marks(float(score))
        assert got_letter == letter and round(got, 2) == float(gp), score


def test_gpa_letters_give_range():
    r = m.vu_gpa_calculator([{"grade": "A", "credit_hours": 3}, {"grade": "B+", "credit_hours": 3},
                             {"grade": "B", "credit_hours": 3}])
    assert r["exact"] is False and r["sgpa"] is None
    assert r["sgpa_range"] == [round((4 + 3.33 + 3.0) / 3, 2), round((4 + 3.65 + 3.32) / 3, 2)]
    assert r["source"]["page_id"] == 312


def test_gpa_marks_exact_and_cgpa():
    r = m.vu_gpa_calculator([{"marks": 87, "credit_hours": 3}, {"marks": 77, "credit_hours": 3},
                             {"marks": 72, "credit_hours": 3}], previous_cgpa=3.2,
                            previous_credit_hours=30)
    sg = (4.0 + 3.46 + 3.08) / 3  # table 20.2 values for 87/77/72 %
    assert r["exact"] and abs(r["sgpa"] - sg) < 0.01
    assert abs(r["cgpa"] - (3.2 * 30 + sg * 9) / 39) < 0.01


def test_gpa_rejects_unknown_grade():
    with pytest.raises(ValueError):
        m.vu_gpa_calculator([{"grade": "C+", "credit_hours": 3}])
    with pytest.raises(ValueError):
        m.vu_gpa_calculator([{"grade": "A", "credit_hours": 3}], previous_cgpa=3.0)


# ------------------------------------------------------------------ course load
def test_course_load_2104_batch():
    r = m.vu_course_load_check(5, 3.8, program="BS Software Engineering", batch="2024")
    (a,) = r["answers"]
    assert a["program_page"]["page_id"] == 159 and a["computed"]
    assert a["max_standard"] == 18 and a["max_possible"] == 21
    assert "Course%20Selection%20BS%20(Software%20Engineering)" in a["source_url"]
    assert "LMS" in r["disclaimer"]


def test_course_load_pre_2021_page_rules():
    r = m.vu_course_load_check(2, 2.5, program="BSSE", batch="Fall 2019")
    (a,) = r["answers"]
    assert a["program_page"]["page_id"] == 146 and a["max_standard"] == 15
    r = m.vu_course_load_check(5, 3.8, program="BSSE", batch="Fall 2019")
    assert r["answers"][0]["max_standard"] == 18 and r["answers"][0]["max_possible"] == 21
    r = m.vu_course_load_check(5, 2.9, program="BSSE", batch="Fall 2019")
    assert r["answers"][0]["max_possible"] == 18
    r = m.vu_course_load_check(8, 2.9, program="BSSE", batch="Fall 2019", is_final_semester=True)
    assert r["answers"][0]["max_standard"] == 21


def test_course_load_without_program_asks_for_it():
    r = m.vu_course_load_check(5, 3.8)
    assert r["computed"] is False and "program" in r["note"]


def test_course_load_missing_limits_is_honest():
    r = m.vu_course_load_check(5, 3.8, program="BS Software Engineering", batch="Fall 2025")
    (a,) = r["answers"]
    assert a["program_page"]["page_id"] == 177 and a["computed"] is False
    assert "not in the snapshot" in a["note"]


def test_course_load_validation():
    with pytest.raises(ValueError):
        m.vu_course_load_check(0, 3.0, program="BSSE")
    with pytest.raises(ValueError):
        m.vu_course_load_check(3, 4.5, program="BSSE")


def test_results_are_json_serialisable():
    for out in (m.vu_ask("semester freeze"), m.vu_key_dates_and_fees("rechecking"),
                m.vu_gpa_calculator([{"grade": "A", "credit_hours": 3}]),
                m.vu_course_load_check(3, 3.1, program="BSCS", batch="2404")):
        json.dumps(out, ensure_ascii=False)
