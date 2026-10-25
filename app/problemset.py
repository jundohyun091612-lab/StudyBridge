"""StudyBridge Problem Set PDF: a printable worksheet for one subject.

Sources (all already stored, so building one makes NO AI calls and costs nothing):
  - practice : each chapter sheet's "Practice Problems" (+ matching worked answers)
  - quiz     : multiple-choice questions from quizzes the student already took
  - missed   : questions the student got wrong before (weak spots)
A saved practice exam can also be printed on its own (build_exam_pdf).

Layout: header band (logo, subject, Name / Date / Score boxes), problems grouped by
chapter with answer space, then an Answer Key at the back (optional).
"""
import re
import json
import random

from . import book
from .book import fitz, PAGE, MX, BAND_H, INK, MUTED, COBALT, BookError

# --------------------------------------------------------------------- parsing
_ITEM = re.compile(r"^(\d{1,3})[.)]\s+(.*)$")          # top-level numbered item only


def split_items(md_text):
    """'1. ...\\n2. ...' -> {1: '...', 2: '...'} (continuation lines stay with their item)."""
    items, cur = {}, None
    for line in (md_text or "").split("\n"):
        m = _ITEM.match(line)
        if m:
            cur = int(m.group(1))
            items[cur] = [m.group(2)]
        elif cur is not None:
            items[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in items.items()}


def _sheet_practice(content_md):
    """Returns [(problem_md, answer_md_or_None)] from one chapter sheet."""
    _t, secs = book.split_sheet(content_md)
    prac = next((b for n, t, b in secs if book._kind(n, t) == "practice"), "")
    ans = next((b for n, t, b in secs if book._kind(n, t) == "answers"), "")
    p_items, a_items = split_items(prac), split_items(ans)
    if not p_items and prac.strip():
        return [(prac.strip(), ans.strip() or None)]
    return [(p_items[k], a_items.get(k)) for k in sorted(p_items)]


def _chapter_quiz(conn, chapter_id):
    """Unique multiple-choice questions from every quiz saved for this chapter."""
    seen, out = set(), []
    for r in conn.execute("SELECT questions_json FROM quizzes WHERE chapter_id=? ORDER BY id DESC",
                          (chapter_id,)).fetchall():
        try:
            qs = json.loads(r["questions_json"] or "[]")
        except Exception:
            continue
        for q in qs:
            text = (q.get("q") or q.get("question") or "").strip()
            ch = q.get("choices") or []
            if not text or len(ch) < 2 or text in seen:
                continue
            seen.add(text)
            out.append({"q": text, "choices": [str(c) for c in ch],
                        "answer_index": q.get("answer_index"), "explanation": q.get("explanation") or ""})
    return out


def _chapter_missed(conn, user_id, chapter_id):
    return [dict(r) for r in conn.execute(
        "SELECT id, question, note FROM weak_spots WHERE user_id=? AND chapter_id=? AND resolved=0 ORDER BY id",
        (user_id, chapter_id)).fetchall() if (r["question"] or "").strip()]


def _chapters(conn, user_id, subject_id):
    subj = conn.execute("SELECT * FROM subjects WHERE id=? AND user_id=?", (subject_id, user_id)).fetchone()
    if not subj:
        raise BookError("Subject not found.")
    rows = []
    for c in conn.execute("SELECT * FROM chapters WHERE subject_id=? ORDER BY order_index, id", (subject_id,)).fetchall():
        if "in_scope" in c.keys() and c["in_scope"] == 0:
            continue
        s = conn.execute("SELECT content_md FROM summaries WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
                         (c["id"],)).fetchone()
        rows.append({"id": c["id"], "name": c["name"],
                     "practice": _sheet_practice(s["content_md"]) if s and s["content_md"] else [],
                     "quiz": _chapter_quiz(conn, c["id"]),
                     "missed": _chapter_missed(conn, user_id, c["id"])})
    return subj, rows


def options(conn, user_id, subject_id):
    """What the Problem Set panel can offer for this subject (counts only)."""
    subj, rows = _chapters(conn, user_id, subject_id)
    exams = [dict(r) for r in conn.execute(
        "SELECT id, title, created_at FROM mock_exams WHERE user_id=? AND subject_id=? ORDER BY id DESC",
        (user_id, subject_id)).fetchall()]
    return {"subject": subj["name"],
            "chapters": [{"id": r["id"], "name": r["name"], "practice": len(r["practice"]),
                          "quiz": len(r["quiz"]), "missed": len(r["missed"])} for r in rows],
            "exams": exams}


# --------------------------------------------------------------------- html
def _css(archive):
    return book._story_css(archive) + f"""
.intro {{ font-size: 9.8pt; color: {MUTED}; margin: 0 0 6pt 0; }}
.ps-ch {{ font-size: 13pt; font-weight: bold; color: {INK}; margin: 16pt 0 8pt 0; padding: 0 0 4pt 0;
          border-bottom: 1.4pt solid {COBALT}; }}
.ps-ch .num {{ color: {COBALT}; }}
.ps-first {{ margin-top: 2pt; }}
.q {{ margin: 0 0 12pt 0; }}
.q p {{ margin: 0 0 5pt 0; }}
.qn {{ color: {COBALT}; font-weight: bold; }}
.tag {{ font-size: 7.6pt; font-weight: bold; color: #b4530a; }}
.choices {{ margin: 2pt 0 0 14pt; font-size: 10.2pt; }}
.choices .opt {{ margin: 0 0 3pt 0; }}
.letter {{ font-weight: bold; color: {COBALT}; }}
.lines {{ margin: 4pt 0 0 14pt; }}
.ln {{ height: 20pt; border-bottom: 0.6pt solid #cfd7e4; }}
.akey {{ margin: 0 0 7pt 0; }}
.akey .qn {{ color: {COBALT}; }}
.akey-ch {{ font-size: 12.5pt; font-weight: bold; color: {INK}; margin: 14pt 0 6pt 0; padding: 0 0 3pt 0;
            border-bottom: 1.2pt solid #22d3ee; }}
.akey-first {{ margin-top: 0; }}
.none {{ color: {MUTED}; font-style: italic; }}
"""


def _md_inline(md_text, archive, dc):
    """Markdown -> story HTML with the outer <p> kept (block content is allowed)."""
    return book.md_to_story_html(book.clean(md_text), archive, dc)


def _choices(choices):
    """Multiple-choice options, one per line: 'A   text'."""
    return '<div class="choices">' + "".join(
        f'<p class="opt"><span class="letter">{chr(65 + j)}</span>&nbsp;&nbsp;&nbsp;{book._esc(c)}</p>'
        for j, c in enumerate(choices[:6])) + '</div>'


def _lines(n):
    return '<div class="lines">' + '<div class="ln"></div>' * n + '</div>'


def _problems_blocks(groups, archive, dc):
    """One html block per problem (kept on one page); a chapter heading rides with its first problem.
    groups: [(chapter_index, chapter_name, [item,...])], item = dict(kind, n, ...)."""
    out = []
    first = True
    for ci, cname, items in groups:
        cls = "ps-ch ps-first" if first else "ps-ch"
        first = False
        head = f'<p class="{cls}"><span class="num">Chapter {ci}</span> &nbsp;{book._esc(cname)}</p>'
        for k, it in enumerate(items):
            n = it["n"]
            if it["kind"] == "quiz":
                body = _md_inline(f'<span class="qn">{n}.</span> ' + it["q"], archive, dc)
                block = f'<div class="q">{body}{_choices(it["choices"])}</div>'
            elif it["kind"] == "missed":
                body = _md_inline(f'<span class="qn">{n}.</span> <span class="tag">MISSED BEFORE</span> '
                                  + it["q"], archive, dc)
                block = f'<div class="q">{body}{_lines(3)}</div>'
            else:
                body = _md_inline(f'<span class="qn">{n}.</span> ' + it["q"], archive, dc)
                block = f'<div class="q">{body}{_lines(4)}</div>'
            out.append((head if k == 0 else "") + block)
    return out


def _answers_blocks(groups, archive, dc):
    out, first = [], True
    for ci, cname, items in groups:
        cls = "akey-ch akey-first" if first else "akey-ch"
        first = False
        head = f'<p class="{cls}">Chapter {ci} &nbsp;{book._esc(cname)}</p>'
        for k, it in enumerate(items):
            n = it["n"]
            if it["kind"] == "quiz":
                ai = it.get("answer_index")
                if isinstance(ai, int) and 0 <= ai < len(it["choices"]):
                    ans = f"**{chr(65 + ai)}.** {it['choices'][ai]}"
                else:
                    ans = "(no answer stored)"
                if it.get("explanation"):
                    ans += f" \u2014 {it['explanation']}"
            elif it["kind"] == "missed":
                ans = it.get("a") or "(no answer stored)"
            else:
                ans = it.get("a") or "(this sheet has no worked answer for this problem)"
            block = '<div class="akey">' + _md_inline(f'<span class="qn">{n}.</span> ' + ans, archive, dc) + '</div>'
            out.append((head if k == 0 else "") + block)
    return out


_MD_ITEM = re.compile(r"^(\d{1,3})[.)]\s+")
_MD_HEAD = re.compile(r"^#{1,4}\s+")


_CHOICE_START = re.compile(r"(?:^|\s)\(?A[).]\s+")
_CHOICE = re.compile(r"\(?([A-F])[).]\s+(.*?)(?=\s+\(?[A-F][).]\s+|$)", re.S)


def _split_choices(text):
    """'Stem? A) x  B) y  C) z' -> ('Stem?', ['x', 'y', 'z']); no choices -> (text, [])."""
    m = _CHOICE_START.search(text)
    if not m:
        return text, []
    stem, rest = text[:m.start()].strip(), text[m.start():].strip()
    choices = [c.strip() for _l, c in _CHOICE.findall(rest)]
    return (stem, choices) if len(choices) >= 2 else (text, [])


def md_blocks(md_text, archive, dc, answer_space=False):
    """Split exam-style markdown into keep-together blocks: one per top-level numbered question
    (its number kept as written), headings ride with the next question. Multiple-choice options
    are laid out one per row; open questions get answer lines (more under an extended-response part)."""
    blocks, cur, pending = [], [], []
    part = [""]

    def flush():
        if cur:
            m = _MD_ITEM.match(cur[0])
            num, first = (m.group(1), cur[0][m.end():]) if m else (None, cur[0])
            joined = " ".join([first] + [ln.strip() for ln in cur[1:] if ln.strip()])
            stem, choices = _split_choices(joined) if answer_space else (joined, [])
            lead = f'<span class="qn">{num}.</span> ' if num else ""
            html = _md_inline(lead + stem, archive, dc)
            if choices:
                html += _choices(choices)
            elif answer_space:
                html += _lines(8 if "extended" in part[0].lower() else 4)
            blocks.append("".join(pending) + f'<div class="q">{html}</div>')
            pending.clear()
        cur.clear()

    for line in (md_text or "").split("\n"):
        if _MD_HEAD.match(line):
            flush()
            part[0] = line
            pending.append(_md_inline(line, archive, dc))
        elif _MD_ITEM.match(line):
            flush()
            cur.append(line)
        elif cur:
            cur.append(line)
        elif line.strip():
            pending.append(_md_inline(line, archive, dc))
    flush()
    if pending:
        blocks.append("".join(pending))
    return blocks


# --------------------------------------------------------------------- pages
def _draw_fields(page, total):
    """Name / Date / Score blanks under the header band, spread across the page width."""
    y = BAND_H + 34
    bold, reg = book._font("figbo"), book._font("figo")
    line_col = book._rgb("#9aa6b8")
    total_txt = f"/ {total}"
    score_w = 44
    right = PAGE.width - MX
    # Score block is pinned to the right edge
    tw = reg.text_length(total_txt, fontsize=11)
    s_line_end = right - tw - 6
    s_line_start = s_line_end - score_w
    s_label_x = s_line_start - 8 - bold.text_length("Score", fontsize=9)
    book._text(page, s_label_x, y, "Score", 9, "figbo", MUTED)
    page.draw_line((s_line_start, y + 2), (s_line_end, y + 2), color=line_col, width=0.8)
    book._text(page, right, y, total_txt, 11, "figo", INK, align="right")
    # Name and Date share the rest
    x = MX
    book._text(page, x, y, "Name", 9, "figbo", MUTED)
    nx = x + bold.text_length("Name", fontsize=9) + 8
    date_label_w = bold.text_length("Date", fontsize=9)
    avail = s_label_x - 24 - nx
    name_w = avail * 0.62
    page.draw_line((nx, y + 2), (nx + name_w, y + 2), color=line_col, width=0.8)
    dx = nx + name_w + 22
    book._text(page, dx, y, "Date", 9, "figbo", MUTED)
    page.draw_line((dx + date_label_w + 8, y + 2), (s_label_x - 24, y + 2), color=line_col, width=0.8)


# --------------------------------------------------------------------- public builders
def build_problem_set(conn, user_id, subject_id, chapter_ids=None, sources=("practice", "quiz", "missed"),
                      answer_key=True, shuffle=False):
    subj, rows = _chapters(conn, user_id, subject_id)
    order_names = [r["name"] for r in rows]
    if chapter_ids:
        want = {int(x) for x in chapter_ids}
        rows = [r for r in rows if r["id"] in want]
    sources = set(sources or ())
    groups, n, n_mc, n_fr = [], 0, 0, 0
    for r in rows:
        items = []
        if "practice" in sources:
            items += [{"kind": "practice", "q": q, "a": a} for q, a in r["practice"]]
        if "quiz" in sources:
            items += [dict(kind="quiz", **q) for q in r["quiz"]]
        if "missed" in sources:
            items += [{"kind": "missed", "q": m["question"], "a": m["note"]} for m in r["missed"]]
        if not items:
            continue
        if shuffle:
            random.shuffle(items)
        for it in items:
            n += 1
            it["n"] = n
            if it["kind"] == "quiz":
                n_mc += 1
            else:
                n_fr += 1
        ci = order_names.index(r["name"]) + 1 if r["name"] in order_names else len(groups) + 1
        groups.append((ci, r["name"], items))
    if not n:
        raise BookError("No problems found for that selection. Build study sheets or take a quiz first.")
    dc = [0]
    minutes = n_mc * 1.5 + n_fr * 4
    meta_bits = [f"{n} problems"]
    if len(groups) == 1:
        meta_bits.append(f"Chapter {groups[0][0]}: {groups[0][1]}")
    else:
        meta_bits.append(f"{len(groups)} chapters")
    meta_bits.append(f"about {int(round(minutes / 5.0) * 5) or 5} min")
    intro = ('<p class="intro">Show your work in the space under each problem. '
             + ('The answer key is at the back, so try everything before you look.' if answer_key
                else 'Answers are not included in this copy.') + '</p>')

    # the html is built inside _assemble_with, against the archive the story will use
    def build(archive):
        return ([intro] + _problems_blocks(groups, archive, dc),
                _answers_blocks(groups, archive, dc) if answer_key else None)
    return _assemble_with(build, "PROBLEM SET", subj["name"], "   \u00b7   ".join(meta_bits), n,
                          subj["name"], "Problem Set")


def _assemble_with(build, kicker, title, meta, total, subject, label):
    """Lay out body + answer blocks (html is built against the archive the story uses, because
    diagram images live in that archive), then add the header band, fields and page furniture."""
    archive = fitz.Archive()
    css = _css(archive)
    body_blocks, ans_blocks = build(archive)
    body_pdf = book._flow_blocks(body_blocks, css, archive, first_top=BAND_H + 58)
    ans_pdf = book._flow_blocks(ans_blocks, css, archive, first_top=BAND_H + 26, gap=4) if ans_blocks else None
    return _finish(kicker, title, meta, total, subject, body_pdf, ans_pdf, label)


def _finish(kicker, title, meta, total, subject, body_pdf, ans_pdf, label):
    logo_png = book._png(book._logo(160))
    band = book.band_art()
    doc = fitz.open()
    book._XREFS.clear()
    doc.insert_pdf(fitz.open("pdf", body_pdf))
    n_body = doc.page_count
    if ans_pdf:
        doc.insert_pdf(fitz.open("pdf", ans_pdf))
    left = f"{subject}  \u00b7  {label}"
    for pno in range(doc.page_count):
        page = doc[pno]
        if pno == 0:
            book.draw_band(page, band, kicker, title, meta, logo_png)
            _draw_fields(page, total)
            book._text(page, PAGE.width - MX, PAGE.height - 28, str(pno + 1), 9, "figbo", INK, align="right")
        elif ans_pdf and pno == n_body:
            book.draw_band(page, band, "ANSWER KEY", "Answers",
                           "Check your work after you finish. Redo anything you missed tomorrow.", logo_png)
            book._text(page, PAGE.width - MX, PAGE.height - 28, str(pno + 1), 9, "figbo", INK, align="right")
        else:
            book.draw_running(page, left, "Answer Key" if (ans_pdf and pno > n_body) else label, pno + 1)
    toc = [[1, label, 1]]
    if ans_pdf:
        toc.append([1, "Answer Key", n_body + 1])
    try:
        doc.set_toc(toc)
    except Exception:
        pass
    doc.set_metadata({"title": f"{subject} - {label}", "creator": "StudyBridge", "producer": "StudyBridge"})
    try:
        doc.subset_fonts()
    except Exception:
        pass
    out = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
    safe = re.sub(r"[^A-Za-z0-9 _.-]+", "", subject).strip() or "Subject"
    return out, f"StudyBridge - {safe} - {label}.pdf"


_AKEY = re.compile(r"^#{1,3}\s*Answer Key\s*$", re.I | re.M)


def build_exam_pdf(conn, user_id, exam_id, answer_key=True):
    """A saved practice exam as a printable paper (questions, then the answer key)."""
    e = conn.execute("SELECT e.*, s.name AS subject FROM mock_exams e JOIN subjects s ON s.id=e.subject_id "
                     "WHERE e.id=? AND e.user_id=?", (exam_id, user_id)).fetchone()
    if not e:
        raise BookError("Practice exam not found.")
    text = book.clean(e["content_md"] or "")
    m = _AKEY.search(text)
    questions, answers = (text[:m.start()], text[m.end():]) if m else (text, "")
    # drop the exam's own top title line; the band shows it
    questions = re.sub(r"^\s*#\s+.*\n", "", questions, count=1)
    total = len(re.findall(r"^\d{1,3}[.)]\s", questions, flags=re.M)) or 40
    label = "Practice Exam"

    def build(archive):
        dc = [0]
        intro = ('<p class="intro">Treat this like the real test: time yourself and don\'t look at your notes. '
                 + ('The answer key is at the back.' if answer_key and answers.strip() else '') + '</p>')
        body = [intro] + md_blocks(questions, archive, dc, answer_space=True)
        ans = md_blocks(answers, archive, dc) if (answer_key and answers.strip()) else None
        return body, ans
    title = (e["title"] or "Practice Exam").strip()
    return _assemble_with(build, "PRACTICE EXAM", e["subject"], f"{title}   ·   {total} questions",
                          total, e["subject"], label)
