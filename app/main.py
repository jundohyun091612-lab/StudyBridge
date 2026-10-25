"""
main.py  --  the StudyBridge web server (FastAPI).

Screens:
  /            login
  /dashboard   input everything (subjects, syllabus, files, dates, scope, free time),
               confirm file->chapter matches, press "Generate study materials"
  /study       the study screen (per-chapter sheets + follow-up questions)
  /plan        the day-by-day study plan (editable)

The heavy "generate" step runs in the background and reports progress as "n / N chapters"
so the screen can show a progress bar.
"""
import os
import secrets
import hashlib as _hashlib
import time
import asyncio
import threading
import traceback
from datetime import date

import markdown as md
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, extract, ai, presets
from .logger import get_logger

log = get_logger()

app = FastAPI(title="StudyBridge")
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
# Cache-busting token for static assets: changes every server start (run.ps1 restarts on launch),
# so the browser always fetches the latest CSS/JS instead of a stale cached copy.
templates.env.globals["ASSET_V"] = int(__import__("time").time())

# Simple local session store: cookie token -> user_id (fine for a single-user local app)
SESSIONS = {}

# Background generation progress (one job at a time)
GEN = {"state": "idle", "done": 0, "total": 0, "message": "", "error": None,
       "started_at": 0, "eta_sec": 0,
       "chapter": "", "phase": "", "sub_done": 0, "sub_total": 0,
       "ch_started_at": 0, "cancel": False}

# Upload/extract progress (polled by the dashboard while files are processed).
UP = {"state": "idle", "done": 0, "total": 0, "now": "",
      "added": 0, "excluded": 0, "warned": 0, "failed": 0,
      "started_at": 0, "eta_sec": 0}


@app.on_event("startup")
def _startup():
    db.init_db()
    log.info("Server startup complete. Open http://%s:%d", config.HOST, config.PORT)


# ---- global error logging ----------------------------------------------------
@app.exception_handler(Exception)
async def _all_errors(request: Request, exc: Exception):
    log.error("Unhandled error on %s\n%s", request.url.path, traceback.format_exc())
    return JSONResponse(status_code=500, content={"error": str(exc)})


import re as _re

_SRC_BADGE = {
    "ai": '<span class="src-badge ai">🤖 AI-written</span>',
    "mixed": '<span class="src-badge mixed">🤖 AI-expanded</span>',
    "material": '<span class="src-badge mat">📄 From your notes</span>',
}


def _clear_subject_chapters(conn, subject_id):
    """Delete a subject's chapters in FK-safe order (summaries/qa first), detaching plan
    rows and materials. Used wherever chapters are rebuilt or a subject is removed."""
    chap_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chapters WHERE subject_id=?", (subject_id,)).fetchall()]
    if chap_ids:
        qs = ",".join("?" * len(chap_ids))
        conn.execute(f"DELETE FROM summaries WHERE chapter_id IN ({qs})", chap_ids)
        conn.execute(f"DELETE FROM qa_history WHERE chapter_id IN ({qs})", chap_ids)
        conn.execute(f"UPDATE plan_items SET chapter_id=NULL WHERE chapter_id IN ({qs})", chap_ids)
    conn.execute("DELETE FROM chapters WHERE subject_id=?", (subject_id,))
    return len(chap_ids)


def md_to_html(text: str) -> str:
    text = text or ""
    # Pull mermaid code blocks out BEFORE markdown so their arrows/syntax aren't escaped.
    diagrams = {}
    def _stash_mermaid(m):
        key = f"@@MERMAID{len(diagrams)}@@"
        diagrams[key] = m.group(1).strip()
        return key
    text = _re.sub(r"```mermaid\s*\n(.*?)```", _stash_mermaid, text, flags=_re.DOTALL)
    # Pull provenance tags off the headings BEFORE markdown so they render as clean badges.
    # A heading looks like:  ## 2. Key Concepts {src:mixed}
    badges = {}
    def _stash(m):
        key = f"@@SRC{len(badges)}@@"
        badges[key] = _SRC_BADGE.get(m.group(1), "")
        return key
    text = _re.sub(r"\{src:(ai|mixed|material)\}", _stash, text)
    html = md.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    for key, span in badges.items():
        html = html.replace(key, span)
    # any stragglers
    html = _re.sub(r"\{src:(ai|mixed|material)\}", "", html)
    # Put mermaid blocks back as <div class="mermaid"> for client-side rendering.
    for key, code in diagrams.items():
        block = f'<div class="mermaid">{code}</div>'
        html = html.replace(f"<p>{key}</p>", block).replace(key, block)
    return html


# ---- auth helpers ------------------------------------------------------------
def current_user(request: Request):
    token = request.cookies.get("sb_session")
    uid = SESSIONS.get(token)
    if not uid:
        return None
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()


def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="not logged in")
    return u


# ============================ PAGES ===========================================
@app.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/app", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"default": db.default_login()})


@app.get("/app", response_class=HTMLResponse)
def app_shell(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "shell.html", {"user": u})


@app.post("/login")
def do_login(request: Request, login_id: str = Form(...), password: str = Form(...)):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE login_id=? AND password=?",
            (login_id.strip(), password),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        log.info("Login failed for %s", login_id)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect email or password.", "default": db.default_login()}
        )
    token = secrets.token_hex(16)
    SESSIONS[token] = row["id"]
    log.info("Login ok: %s", login_id)
    resp = RedirectResponse("/app", status_code=302)
    resp.set_cookie("sb_session", token, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("sb_session")
    SESSIONS.pop(token, None)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("sb_session")
    return resp


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "dashboard.html", {"user": u})


@app.get("/study", response_class=HTMLResponse)
def study(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "study.html", {"user": u})


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "plan.html", {"user": u})


# ============================ SUBJECTS & SYLLABUS =============================
@app.get("/api/subjects")
def api_subjects(request: Request):
    u = require_user(request)
    conn = db.connect()
    try:
        subs = conn.execute("SELECT * FROM subjects WHERE user_id=? ORDER BY id", (u["id"],)).fetchall()
        out = []
        for s in subs:
            chapters = conn.execute(
                "SELECT * FROM chapters WHERE subject_id=? ORDER BY order_index, id", (s["id"],)
            ).fetchall()
            ch_list = []
            for c in chapters:
                mats = conn.execute(
                    "SELECT id, filename FROM materials WHERE chapter_id=? AND confirmed=1", (c["id"],)
                ).fetchall()
                ch_list.append({
                    "id": c["id"], "name": c["name"],
                    "materials": [dict(m) for m in mats],
                    "has_material": len(mats) > 0,
                    "in_scope": (c["in_scope"] if "in_scope" in c.keys() and c["in_scope"] is not None else 1),
                    "weight": (c["weight"] if "weight" in c.keys() and c["weight"] is not None else 2),
                })
            out.append({
                "id": s["id"], "name": s["name"], "exam_date": s["exam_date"],
                "exam_scope": s["exam_scope"], "available_time": s["available_time"],
                "syllabus_source": s["syllabus_source"], "level": s["level"] or "",
                "dirty": bool(s["dirty"]) if "dirty" in s.keys() else False,
                "chapters": ch_list,
            })
        return {"subjects": out}
    finally:
        conn.close()


@app.post("/api/subject")
def api_save_subject(request: Request, name: str = Form(...), exam_date: str = Form(""),
                     exam_scope: str = Form(""), available_time: str = Form(""),
                     level: str = Form(""), subject_id: str = Form("")):
    u = require_user(request)
    conn = db.connect()
    try:
        if subject_id:
            conn.execute(
                "UPDATE subjects SET name=?, exam_date=?, exam_scope=?, available_time=?, level=?, dirty=1 "
                "WHERE id=? AND user_id=?",
                (name.strip(), exam_date, exam_scope, available_time, level, subject_id, u["id"]),
            )
            sid = int(subject_id)
        else:
            cur = conn.execute(
                "INSERT INTO subjects (user_id, name, exam_date, exam_scope, available_time, level, syllabus_source, dirty) "
                "VALUES (?,?,?,?,?,?, 'none', 1)",
                (u["id"], name.strip(), exam_date, exam_scope, available_time, level),
            )
            sid = cur.lastrowid
        conn.commit()
        log.info("Saved subject id=%s name=%s level=%s", sid, name, level)
        return {"subject_id": sid}
    finally:
        conn.close()


@app.delete("/api/subject/{sid}")
def api_delete_subject(request: Request, sid: int):
    u = require_user(request)
    conn = db.connect()
    try:
        # delete in FK-safe order: summaries/qa (ref chapters) -> chapters -> detach materials/plan -> subject
        n = _clear_subject_chapters(conn, sid)
        conn.execute("UPDATE materials SET subject_id=NULL, chapter_id=NULL, confirmed=0 WHERE subject_id=?", (sid,))
        conn.execute("DELETE FROM subjects WHERE id=? AND user_id=?", (sid, u["id"]))
        conn.commit()
        log.info("Deleted subject id=%s (%d chapters)", sid, n)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/syllabus/preview")
def api_syllabus_preview(request: Request, name: str = Form(...), exam_scope: str = Form("")):
    """Generate a chapter list from subject name + scope, WITHOUT saving anything."""
    u = require_user(request)
    try:
        chapters = ai.generate_syllabus(name.strip(), u["grade"], exam_scope or "")
        log.info("Syllabus preview for '%s': %d chapters", name, len(chapters))
        return {"chapters": chapters}
    except Exception as e:
        log.exception("syllabus preview failed")
        return {"error": f"AI could not generate the syllabus: {e}"}


@app.post("/api/syllabus/from-file")
async def api_syllabus_from_file(request: Request, name: str = Form(""), file: UploadFile = File(...)):
    """Read an uploaded syllabus file (or zip of files), extract its text, and pull out the
    subject name + chapter list. The subject is auto-detected so the name field can be left blank."""
    u = require_user(request)
    try:
        raw = await file.read()
        members = extract.expand_upload(file.filename, raw)
        texts = []
        for mname, mdata in members:
            try:
                texts.append(extract.extract_text(mname, mdata))
            except Exception:
                log.exception("syllabus member extract failed | %s", mname)
        text = "\n\n".join(t for t in texts if t and t.strip())
        if not text.strip():
            return {"error": "No text could be read from that file."}
        result = ai.extract_syllabus_from_text(name.strip(), text)
        if not result.get("chapters"):
            return {"error": "No chapters were found in that file. You can type them in instead."}
        log.info("Syllabus from file '%s': subject=%s, %d chapters",
                 file.filename, result.get("subject"), len(result["chapters"]))
        return {"subject": result.get("subject", ""), "chapters": result["chapters"]}
    except Exception as e:
        log.exception("syllabus from-file failed")
        return {"error": f"Could not read the syllabus file: {e}"}


def _merge_chapter_names(existing, new):
    """Append new chapter names not already present (case-insensitive), preserving order."""
    seen = {n.strip().lower() for n in existing}
    out = list(existing)
    for n in new:
        k = n.strip().lower()
        if k and k not in seen:
            out.append(n.strip()); seen.add(k)
    return out


@app.post("/api/syllabus/preview-import")
async def api_syllabus_preview_import(request: Request, files: list[UploadFile] = File(...)):
    """Read one/many files (or zips/folders), then make ONE AI call to group them into the
    few real subjects. Returns a PREVIEW for the student to confirm — nothing is created yet."""
    u = require_user(request)
    members = []
    for f in files:
        raw = await f.read()
        try:
            members.extend(extract.expand_upload(f.filename, raw))
        except Exception:
            log.exception("syllabus preview: could not open %s", f.filename)

    bundle, errors = [], []
    for mname, mdata in members:
        try:
            text = extract.extract_text(mname, mdata)
            if text.strip():
                bundle.append({"name": os.path.basename(mname), "text": text})
            else:
                errors.append(mname)
        except Exception:
            log.exception("syllabus preview extract failed | %s", mname)
            errors.append(mname)

    if not bundle:
        return {"error": "No readable text was found in those files."}

    try:
        result = ai.analyze_syllabus_bundle(bundle)
    except ai.AIError as e:
        log.warning("syllabus bundle analyze failed: %s | detail=%s", e.reason, e.detail)
        return {"error": e.reason}
    except Exception:
        log.exception("syllabus bundle analyze failed (unexpected)")
        return {"error": "Couldn’t read those files. Check the newest file in the logs folder."}

    subjects = result.get("subjects", [])
    if not subjects:
        return {"error": "Couldn't recognize any subjects in those files."}

    # Over-split guard: many subjects usually means class NOTES were uploaded, not a syllabus.
    warning = ""
    if len(subjects) >= 5:
        warning = (f"That produced {len(subjects)} subjects, which is a lot. If you uploaded "
                   f"class notes or photos by mistake, those belong in Step 2 (Add your class "
                   f"files), not here. Review the list below before creating.")
    log.info("Syllabus preview | %d files -> %d subjects (warn=%s)",
             len(bundle), len(subjects), bool(warning))
    return {"subjects": subjects, "file_count": len(bundle),
            "skipped": len(errors), "warning": warning}


@app.post("/api/syllabus/commit-import")
async def api_syllabus_commit_import(request: Request):
    """Create/merge the subjects the student confirmed from the preview."""
    u = require_user(request)
    body = await request.json()
    subjects = body.get("subjects", [])
    if not subjects:
        return {"error": "Nothing to create."}
    conn = db.connect()
    created = []
    try:
        for s in subjects:
            subj = (s.get("name") or "").strip()
            chs = [c.strip() for c in s.get("chapters", []) if c and c.strip()]
            exam_date = (s.get("exam_date") or "").strip()
            if not subj or not chs:
                continue
            row = conn.execute(
                "SELECT id FROM subjects WHERE user_id=? AND lower(name)=lower(?)", (u["id"], subj)
            ).fetchone()
            if row:
                sid = row["id"]
                if exam_date:
                    conn.execute("UPDATE subjects SET exam_date=? WHERE id=?", (exam_date, sid))
                existing = [r["name"] for r in conn.execute(
                    "SELECT name FROM chapters WHERE subject_id=? ORDER BY order_index", (sid,)).fetchall()]
                merged = _merge_chapter_names(existing, chs)
                conn.execute("UPDATE materials SET chapter_id=NULL, confirmed=0 WHERE subject_id=?", (sid,))
                _clear_subject_chapters(conn, sid)
                for i, nm in enumerate(merged):
                    conn.execute("INSERT INTO chapters (subject_id, name, order_index) VALUES (?,?,?)", (sid, nm, i))
                action, total = "updated", len(merged)
            else:
                cur = conn.execute(
                    "INSERT INTO subjects (user_id, name, exam_date, exam_scope, syllabus_source, dirty) VALUES (?,?,?,?,?,1)",
                    (u["id"], subj, exam_date, "ALL", "file"))
                sid = cur.lastrowid
                for i, nm in enumerate(chs):
                    conn.execute("INSERT INTO chapters (subject_id, name, order_index) VALUES (?,?,?)", (sid, nm, i))
                action, total = "created", len(chs)
            created.append({"subject": subj, "chapters": total, "action": action})
        conn.commit()
        log.info("Syllabus commit: %d subjects (%s)",
                 len(created), ", ".join(f'{c["subject"]}:{c["action"]}' for c in created))
        return {"created": created}
    finally:
        conn.close()


@app.post("/api/syllabus")
def api_set_syllabus(request: Request, subject_id: int = Form(...),
                     mode: str = Form(...), chapters_text: str = Form(""),
                     scopes: str = Form("")):
    """
    mode = 'user'  -> chapters_text is the student's own list (one per line)
    mode = 'ai'    -> generate the chapter list with AI (marked as AI-made)
    scopes = optional comma list ("1,0,1,...") aligned to the chapter order: 1 = in the
             exam scope (default), 0 = not on this exam (gets no sheet and is left out of the plan).
    """
    u = require_user(request)
    conn = db.connect()
    try:
        sub = conn.execute("SELECT * FROM subjects WHERE id=? AND user_id=?", (subject_id, u["id"])).fetchone()
        if not sub:
            raise HTTPException(404, "subject not found")

        if mode == "ai":
            names = ai.generate_syllabus(sub["name"], u["grade"], sub["exam_scope"] or "")
            source = "ai"
        else:
            names = [ln.strip() for ln in chapters_text.splitlines() if ln.strip()]
            source = "user"

        scope_list = [x.strip() for x in scopes.split(",")] if scopes.strip() else []

        def _scope_at(i):
            if i < len(scope_list):
                return 0 if scope_list[i] in ("0", "false", "False", "no", "off") else 1
            return 1

        # Replace existing chapters for this subject (full reset keeps things simple).
        conn.execute("UPDATE materials SET chapter_id=NULL, confirmed=0 WHERE subject_id=?", (subject_id,))
        _clear_subject_chapters(conn, subject_id)
        for i, nm in enumerate(names):
            conn.execute("INSERT INTO chapters (subject_id, name, order_index, in_scope) VALUES (?,?,?,?)",
                         (subject_id, nm, i, _scope_at(i)))
        conn.execute("UPDATE subjects SET syllabus_source=?, dirty=1 WHERE id=?", (source, subject_id))
        conn.commit()
        log.info("Syllabus set subject=%s mode=%s chapters=%d in_scope=%d",
                 subject_id, mode, len(names), sum(1 for i in range(len(names)) if _scope_at(i)))
        return {"chapters": names, "source": source}
    finally:
        conn.close()


# ============================ UPLOAD (auto-matches to chapters) ==============
@app.post("/api/upload")
async def api_upload(request: Request, files: list[UploadFile] = File(...)):
    """
    Extract text from each file, then AUTO-MATCH every new file to the best chapter
    (the student never has to do a separate matching step). If the AI step cannot run
    (e.g. no key, no chapters yet), files are still saved and the response says so.
    """
    u = require_user(request)
    added, failed, excluded, warned, duplicate = [], [], [], [], []
    conn = db.connect()
    # Existing content hashes for this user, so re-uploads of the same file are skipped.
    seen_hashes = set(
        r["content_hash"] for r in conn.execute(
            "SELECT content_hash FROM materials WHERE user_id=? AND content_hash IS NOT NULL",
            (u["id"],),
        ).fetchall()
    )
    try:
        # Turn the progress indicator on RIGHT AWAY so the screen never looks frozen,
        # even while we're still reading large files / expanding archives.
        UP.update(state="preparing", done=0, total=len(files), now="Preparing files...",
                  added=0, excluded=0, warned=0, failed=0, started_at=time.time(), eta_sec=0)

        # First pass: expand archives so we know the real total for the progress bar.
        members_all = []   # (mname, mdata)
        for fi, f in enumerate(files, 1):
            UP.update(done=fi - 1, now=f"Opening {f.filename}...")
            raw = await f.read()
            big = len(raw) >= config.WARN_FILE_BYTES
            if big:
                warned.append({"file": f.filename, "size_mb": round(len(raw) / 1024 / 1024)})
                log.warning("Large file over 1GB | %s | %d bytes", f.filename, len(raw))
            try:
                members = extract.expand_upload(f.filename, raw)
            except Exception as e:
                log.warning("Archive open failed | %s | %s", f.filename, e)
                failed.append({"file": f.filename, "reason": "couldn't open archive"})
                continue
            for mname, mdata in members:
                members_all.append((mname, mdata))

        UP.update(state="processing", done=0, total=len(members_all), now="",
                  added=0, excluded=0, warned=len(warned), failed=0,
                  started_at=time.time(), eta_sec=0)

        # Second pass: extract + store, updating progress per file.
        for idx, (mname, mdata) in enumerate(members_all, 1):
            await asyncio.sleep(0)  # yield so /api/upload/progress polls can be served
            done_so_far = idx - 1
            elapsed = time.time() - UP.get("started_at", time.time())
            eta = int(elapsed / done_so_far * (len(members_all) - done_so_far)) if done_so_far > 0 else 0
            UP.update(done=done_so_far, now=mname, eta_sec=eta)
            # Skip media/binary types cleanly (video, audio, ...) -> "excluded".
            if extract.is_unsupported(mname):
                excluded.append({"file": mname, "reason": "not a study file (media/binary)"})
                log.info("Excluded unsupported file | %s", mname)
                UP.update(done=idx, excluded=len(excluded))
                continue
            try:
                text = extract.extract_text(mname, mdata)
                # Duplicate check: same extracted content (any non-empty text) already stored.
                chash = None
                if text and text.strip():
                    chash = _hashlib.sha256(text.encode("utf-8")).hexdigest()
                    if chash in seen_hashes:
                        duplicate.append({"file": mname, "reason": "same content already uploaded"})
                        log.info("Duplicate skipped | %s", mname)
                        UP.update(done=idx, now=mname)
                        continue
                conn.execute(
                    "INSERT INTO materials (user_id, filename, filetype, raw_text, content_hash) VALUES (?,?,?,?,?)",
                    (u["id"], mname, os.path.splitext(mname)[1].lower(), text, chash),
                )
                conn.commit()
                if chash:
                    seen_hashes.add(chash)
                added.append(mname)
                UP.update(done=idx, added=len(added))
            except extract.UnsupportedFile as e:
                excluded.append({"file": mname, "reason": str(e)})
                UP.update(done=idx, excluded=len(excluded))
            except Exception as e:
                log.exception("Extract failed on upload | %s", mname)
                failed.append({"file": mname, "reason": _friendly_reason(e)})
                UP.update(done=idx, failed=len(failed))

        UP.update(state="matching", now="Sorting into chapters...")

        # --- auto-match the freshly uploaded (unconfirmed) materials ---
        matched, match_note = [], ""
        chapters = _global_chapter_index(conn, u["id"])
        if not chapters:
            match_note = "Files added. Add a subject with chapters, then press “Sort automatically”."
        else:
            try:
                matched = _run_automatch(conn, u["id"], chapters)
                log.info("Auto-matched %d materials on upload", len(matched))
            except ai.AIError as e:
                log.warning("Auto-match on upload failed: %s | detail=%s", e.reason, e.detail)
                match_note = (f"Files added, but sorting didn’t finish: {e.reason} "
                              f"Press “Sort automatically” below to retry.")
            except Exception:
                log.exception("Auto-match on upload failed (unexpected)")
                match_note = ("Files added, but sorting didn’t finish. Press “Sort automatically” "
                              "below to retry. If it keeps failing, check the newest file in the logs folder.")

        # new files can change matching / chapter contents -> mark subjects out of date
        try:
            conn.execute("UPDATE subjects SET dirty=1 WHERE user_id=?", (u["id"],))
            conn.commit()
        except Exception:
            pass

        UP.update(state="done", now="")
        return {
            "added": len(added),
            "failed": failed,            # [{file, reason}]
            "excluded": excluded,        # [{file, reason}] media/binary skipped
            "warned": warned,            # [{file, size_mb}] over 1GB (still processed)
            "duplicate": duplicate,      # [{file, reason}] same content already uploaded
            "matched": matched,
            "match_note": match_note,
            "summary": {
                "added": len(added), "excluded": len(excluded),
                "warned": len(warned), "failed": len(failed),
                "duplicate": len(duplicate),
            },
        }
    finally:
        conn.close()


@app.get("/api/upload/progress")
def api_upload_progress(request: Request):
    require_user(request)
    return UP


def _run_automatch(conn, user_id, chapters=None):
    """Run the AI matcher over all not-yet-confirmed materials and place them into chapters.
    Returns a list of {filename, label} (label=None means no chapter fit)."""
    if chapters is None:
        chapters = _global_chapter_index(conn, user_id)
    matched = []
    if not chapters:
        return matched
    proposals = _match_proposals(conn, user_id, chapters)
    for p in proposals:
        cid = p["guess_chapter_id"]
        if cid:
            conn.execute(
                "UPDATE materials SET chapter_id=?, subject_id=?, confirmed=1, note='' "
                "WHERE id=? AND user_id=?",
                (cid, p["guess_subject_id"], p["material_id"], user_id),
            )
            matched.append({"filename": p["filename"], "label": p["guess_label"]})
        else:
            conn.execute(
                "UPDATE materials SET note='out of scope' WHERE id=? AND user_id=?",
                (p["material_id"], user_id),
            )
            matched.append({"filename": p["filename"], "label": None})
    conn.commit()
    return matched


@app.post("/api/resort")
def api_resort(request: Request):
    """Re-run automatic sorting on every file that isn't in a chapter yet.
    Used after the student adds chapters, or when matching was interrupted."""
    u = require_user(request)
    conn = db.connect()
    try:
        chapters = _global_chapter_index(conn, u["id"])
        if not chapters:
            return {"error": "Add a subject with chapters first, then sorting can run."}
        # reset prior 'out of scope' marks so everything gets a fresh chance
        conn.execute("UPDATE materials SET note='' WHERE user_id=? AND chapter_id IS NULL", (u["id"],))
        conn.commit()
        try:
            matched = _run_automatch(conn, u["id"], chapters)
        except ai.AIError as e:
            log.warning("Resort failed: %s | detail=%s", e.reason, e.detail)
            return {"error": e.reason}
        except Exception:
            log.exception("Resort failed (unexpected)")
            return {"error": "Sorting failed. Check the newest file in the logs folder."}
        sorted_n = sum(1 for m in matched if m["label"])
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM materials WHERE user_id=? AND chapter_id IS NULL", (u["id"],)
        ).fetchone()["n"]
        log.info("Resort: %d sorted, %d still unsorted", sorted_n, left)
        return {"sorted": sorted_n, "unsorted": left}
    finally:
        conn.close()


def _friendly_reason(exc) -> str:
    """Turn a raw exception into a short, non-technical reason for the UI."""
    s = str(exc).lower()
    name = type(exc).__name__
    if "rate_limit" in s or "429" in s or name == "RateLimitError":
        return "service was busy — try again shortly"
    if "key not found" in s or "api key" in s or "authentication" in name.lower():
        return "AI key not set up"
    if "google shortcut" in s:
        return "Google shortcut — download the real file first"
    if "password" in s or "encrypted" in s:
        return "file is password-protected"
    return "couldn't read this file"


@app.get("/api/material/{mid}/review")
def api_material_review(request: Request, mid: int):
    """Full content of one file + AI's recommended chapters, so the student can
    decide where an unsorted file should go."""
    u = require_user(request)
    conn = db.connect()
    try:
        m = conn.execute(
            "SELECT id, filename, filetype, raw_text FROM materials WHERE id=? AND user_id=?",
            (mid, u["id"]),
        ).fetchone()
        if not m:
            return {"error": "File not found."}
        text = (m["raw_text"] or "").strip()
        is_image = (m["filetype"] or "").lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")

        suggestions = []
        chapters = _global_chapter_index(conn, u["id"])
        if chapters and text:
            try:
                labels = [c["label"] for c in chapters]
                res = ai.match_materials(
                    "ALL SUBJECTS", labels,
                    [{"id": m["id"], "filename": m["filename"], "preview": text[:600]}],
                )
                idxs = []
                for match in res.get("matches", []):
                    if match.get("chapter", -1) is not None and match.get("chapter", -1) >= 0:
                        idxs.append(match["chapter"])
                    idxs.extend([i for i in (match.get("multi") or []) if isinstance(i, int) and i >= 0])
                seen = set()
                for i in idxs:
                    if 0 <= i < len(chapters) and i not in seen:
                        seen.add(i)
                        suggestions.append({"chapter_id": chapters[i]["chapter_id"],
                                            "label": chapters[i]["label"]})
                    if len(suggestions) >= 3:
                        break
            except Exception:
                log.exception("Review suggestion failed for material %s", mid)

        return {
            "filename": m["filename"],
            "filetype": m["filetype"],
            "is_image": is_image,
            "text": text[:6000],
            "truncated": len(text) > 6000,
            "suggestions": suggestions,
        }
    finally:
        conn.close()


@app.get("/api/materials")
def api_materials(request: Request):
    u = require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, filename, filetype, chapter_id, subject_id, confirmed, note, "
            "length(raw_text) AS chars FROM materials WHERE user_id=? ORDER BY id", (u["id"],)
        ).fetchall()
        return {"materials": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/unsorted")
def api_unsorted(request: Request):
    """Files that aren't in any chapter yet — out-of-scope or unmatched. Each with a
    short text preview so the student can recognize it and place it by hand."""
    u = require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, filename, filetype, note, "
            "substr(replace(replace(raw_text, char(10), ' '), char(13), ' '), 1, 140) AS preview "
            "FROM materials WHERE user_id=? AND chapter_id IS NULL ORDER BY id", (u["id"],)
        ).fetchall()
        return {"unsorted": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/material/assign")
def api_material_assign(request: Request, material_id: int = Form(...), chapter_id: int = Form(...)):
    """Manually place a material into a chapter."""
    u = require_user(request)
    conn = db.connect()
    try:
        ch = conn.execute(
            "SELECT c.id, c.subject_id FROM chapters c JOIN subjects s ON c.subject_id=s.id "
            "WHERE c.id=? AND s.user_id=?", (chapter_id, u["id"])
        ).fetchone()
        if not ch:
            return {"error": "That chapter no longer exists."}
        conn.execute(
            "UPDATE materials SET chapter_id=?, subject_id=?, confirmed=1, note='' "
            "WHERE id=? AND user_id=?", (chapter_id, ch["subject_id"], material_id, u["id"]),
        )
        conn.commit()
        log.info("Manual assign | material=%s -> chapter=%s", material_id, chapter_id)
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/material/{mid}")
def api_material_delete(request: Request, mid: int):
    """Delete a material (e.g. a junk file the student doesn't want)."""
    u = require_user(request)
    conn = db.connect()
    try:
        conn.execute("DELETE FROM materials WHERE id=? AND user_id=?", (mid, u["id"]))
        conn.commit()
        log.info("Material deleted | id=%s", mid)
        return {"ok": True}
    finally:
        conn.close()


def _global_chapter_index(conn, user_id):
    """Build a flat list of every chapter across the user's subjects, with labels."""
    rows = conn.execute(
        "SELECT c.id AS cid, c.name AS cname, s.id AS sid, s.name AS sname "
        "FROM chapters c JOIN subjects s ON c.subject_id=s.id WHERE s.user_id=? "
        "ORDER BY s.id, c.order_index, c.id", (user_id,)
    ).fetchall()
    return [{"subject_id": r["sid"], "chapter_id": r["cid"],
             "label": f'{r["sname"]} / {r["cname"]}'} for r in rows]


def _match_proposals(conn, user_id, chapters):
    """One AI call: best-guess a chapter for each not-yet-confirmed material."""
    mats = conn.execute(
        "SELECT id, filename, raw_text FROM materials WHERE user_id=? AND confirmed=0", (user_id,)
    ).fetchall()
    if not mats:
        return []
    payload = [{"id": m["id"], "filename": m["filename"],
                "preview": (m["raw_text"] or "")[:600]} for m in mats]
    labels = [c["label"] for c in chapters]
    result = ai.match_materials("ALL SUBJECTS", labels, payload)

    proposals = []
    by_id = {m["id"]: m for m in mats}
    for match in result.get("matches", []):
        mid = match.get("material_id")
        if mid not in by_id:
            continue
        idx = match.get("chapter", -1)
        multi = match.get("multi", []) or []
        if multi and len(multi) > 1:
            idx = multi[0]  # auto-pick the first chapter when a file spans several
        guess = chapters[idx] if isinstance(idx, int) and 0 <= idx < len(chapters) else None
        proposals.append({
            "material_id": mid,
            "filename": by_id[mid]["filename"],
            "guess_chapter_id": guess["chapter_id"] if guess else None,
            "guess_subject_id": guess["subject_id"] if guess else None,
            "guess_label": guess["label"] if guess else None,
        })
    log.info("Match proposals built: %d", len(proposals))
    return proposals


# ============================ GENERATE (background) ==========================
@app.post("/api/generate/preview")
def api_generate_preview(request: Request):
    """Show which in-scope chapters have material and which are empty (need a decision)."""
    u = require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT c.id AS cid, c.name AS cname, s.name AS sname, "
            "(SELECT COUNT(*) FROM materials m WHERE m.chapter_id=c.id AND m.confirmed=1) AS mat "
            "FROM chapters c JOIN subjects s ON c.subject_id=s.id WHERE s.user_id=? "
            "AND (c.in_scope=1 OR c.in_scope IS NULL) "
            "ORDER BY s.id, c.order_index", (u["id"],)
        ).fetchall()
        with_material, empty = [], []
        for r in rows:
            entry = {"chapter_id": r["cid"], "chapter": r["cname"], "subject": r["sname"]}
            (with_material if r["mat"] > 0 else empty).append(entry)

        # ---- pre-generation estimate (chapters + rough cost + how many are cached) ----
        ver = ai.prompt_version()
        cached = 0
        for r in rows:
            if r["mat"] > 0:
                sh = None  # we can't hash material cheaply here without loading it; estimate by version match
            row = conn.execute(
                "SELECT 1 FROM summaries WHERE chapter_id=? AND prompt_version=? AND source_hash IS NOT NULL",
                (r["cid"], ver),
            ).fetchone()
            if row:
                cached += 1
        total_ch = len(with_material) + len(empty)
        to_generate = max(0, total_ch - cached)
        # ~$0.002 per chapter on the mini tier (compress + sheet). Rough, shown as guidance.
        est_cost = round(to_generate * 0.002, 3)
        estimate = {
            "total_chapters": total_ch,
            "already_cached": cached,
            "to_generate": to_generate,
            "rough_cost_usd": est_cost,
            "cache_enabled": config.CACHE_ENABLED,
            "prompt_version": ver,
        }
        return {"with_material": with_material, "empty": empty, "estimate": estimate}
    finally:
        conn.close()


def _run_generation(user_id, approved_empty_ids, force=False):
    """Background worker: summarize chapters, then build the plan. Updates GEN progress.

    force=True bypasses the sheet cache (always regenerates) — used by the Regenerate button.
    """
    conn = db.connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        rows = conn.execute(
            "SELECT c.id AS cid, c.name AS cname, s.id AS sid, s.name AS sname "
            "FROM chapters c JOIN subjects s ON c.subject_id=s.id WHERE s.user_id=? "
            "AND (c.in_scope=1 OR c.in_scope IS NULL) "
            "ORDER BY s.id, c.order_index", (user_id,)
        ).fetchall()

        # Decide the work list: chapters with material, plus empty chapters the student approved.
        work = []
        for r in rows:
            mats = conn.execute(
                "SELECT raw_text FROM materials WHERE chapter_id=? AND confirmed=1", (r["cid"],)
            ).fetchall()
            has_mat = len(mats) > 0
            if has_mat:
                work.append((r, True, "\n\n".join(m["raw_text"] or "" for m in mats)))
            elif r["cid"] in approved_empty_ids:
                work.append((r, False, ""))
            # else: skipped (empty + not approved)

        GEN.update(state="running", done=0, total=len(work), message="Preparing...", error=None,
                   started_at=time.time(), eta_sec=0,
                   chapter="", phase="prep", sub_done=0, sub_total=0,
                   ch_started_at=0, cancel=False)
        log.info("Generation started: %d chapters | cache=%s | prompt_ver=%s",
                 len(work), "on" if config.CACHE_ENABLED else "OFF", ai.prompt_version())

        n_hit = n_miss = 0
        # ETA model: average wall-clock per chapter that ACTUALLY generated. Cached chapters
        # finish instantly and would otherwise drag the average down and make the ETA lie, so
        # they're excluded. COLD_CHAPTER_SEC seeds an estimate so the ETA is non-zero from
        # chapter 1 instead of blank until the first one finishes.
        COLD_CHAPTER_SEC = 35.0
        gen_acc = {"t": 0.0, "n": 0}

        def _avg_chapter_sec():
            return (gen_acc["t"] / gen_acc["n"]) if gen_acc["n"] else COLD_CHAPTER_SEC

        for i, (r, has_mat, text) in enumerate(work, 1):
            # Stop cleanly between chapters if the user pressed Stop. Everything finished
            # so far is already committed + cached, so a later run resumes from here.
            if GEN.get("cancel"):
                raise ai.GenerationCancelled()

            # Boundary ETA: avg time per generated chapter * chapters not yet done.
            # This is refined live inside the write phase below as tokens stream in.
            eta = int(_avg_chapter_sec() * (len(work) - (i - 1)))
            GEN.update(done=i - 1, eta_sec=eta,
                       chapter=f'{r["sname"]} / {r["cname"]}',
                       phase="prep", sub_done=0, sub_total=0,
                       ch_started_at=time.time(),
                       message=f'Summarizing {r["sname"]} / {r["cname"]}...')

            # ---- cache key for this chapter (material + code version + model) ----
            src_hash = ai.material_hash(text) if has_mat else ai.material_hash("(ai)" + r["cname"])
            ver = ai.prompt_version()

            # ---- CACHE LOOKUP (sheet layer) ----
            if config.CACHE_ENABLED and not force:
                cached = conn.execute(
                    "SELECT id FROM summaries WHERE chapter_id=? AND source_hash=? AND prompt_version=?",
                    (r["cid"], src_hash, ver),
                ).fetchone()
                if cached:
                    n_hit += 1
                    log.info("cache HIT  chapter=%s key=%s ver=%s (skip generate)",
                             r["cname"], src_hash, ver)
                    GEN.update(done=i)
                    continue

            n_miss += 1
            log.info("cache MISS chapter=%s key=%s ver=%s (generating)", r["cname"], src_hash, ver)
            ch_real_start = time.time()   # for the ETA average (real work only)

            # ---- STAGE 1 (map/compress) with its own cache, only when there is material ----
            notes = None
            if has_mat:
                if config.CACHE_ENABLED:
                    nc = conn.execute(
                        "SELECT notes_md FROM note_cache WHERE source_hash=?", (src_hash,)
                    ).fetchone()
                else:
                    nc = None
                if nc:
                    notes = nc["notes_md"]
                    log.info("notes cache HIT chapter=%s", r["cname"])
                else:
                    GEN.update(phase="read", sub_done=0, sub_total=0,
                               message=f'Reading {r["sname"]} / {r["cname"]}...')

                    def _sub(d, t):
                        GEN.update(sub_done=d, sub_total=t)

                    notes, n_chunks = ai.compress_material(
                        r["cname"], r["sname"], text,
                        progress=_sub, should_cancel=lambda: GEN.get("cancel"),
                    )
                    if notes and len(text) > config.COMPRESS_TRIGGER_CHARS:
                        conn.execute(
                            "INSERT OR REPLACE INTO note_cache (source_hash, notes_md, n_chunks) VALUES (?,?,?)",
                            (src_hash, notes, n_chunks),
                        )
                        conn.commit()

            # ---- STAGE 2 (sheet) from compressed notes ----
            # This single AI call is where most of the time goes. We stream it so the bar and
            # ETA move WHILE the sheet is being written, instead of sitting frozen until the
            # call returns. The callback turns live token count into a 0-100% sub-bar and a
            # real-time ETA = (time left on THIS sheet) + (avg time * chapters still after it).
            GEN.update(phase="write", sub_done=0, sub_total=100,
                       message=f'Writing sheet: {r["sname"]} / {r["cname"]}...')
            _write_started = time.time()

            def _sheet_progress(got, expected, _i=i, _start=_write_started):
                exp = expected or ai.SHEET_EXPECTED_TOKENS
                frac = max(0.0, min(0.99, got / exp))
                el = time.time() - _start
                spd = (got / el) if (el > 0.5 and got > 0) else 0.0      # tokens/sec, measured live
                this_left = int((exp - got) / spd) if (spd > 0 and got < exp) else 0
                others_left = int(_avg_chapter_sec() * (len(work) - _i))  # chapters after this one
                GEN.update(sub_done=int(frac * 100), sub_total=100,
                           eta_sec=this_left + others_left)

            sheet = ai.summarize_chapter(
                r["cname"], r["sname"], u["grade"], text, has_mat,
                precompressed_notes=notes, on_progress=_sheet_progress,
            )
            html = md_to_html(sheet)
            conn.execute("DELETE FROM summaries WHERE chapter_id=?", (r["cid"],))
            conn.execute(
                "INSERT INTO summaries (chapter_id, content_md, content_html, is_ai_generated, "
                "source_hash, prompt_version) VALUES (?,?,?,?,?,?)",
                (r["cid"], sheet, html, 0 if has_mat else 1, src_hash, ver),
            )
            conn.commit()
            GEN.update(done=i)
            # record how long this real generation took, to sharpen the ETA for later chapters
            gen_acc["t"] += time.time() - ch_real_start
            gen_acc["n"] += 1

        log.info("Generation sheets done | %d generated, %d from cache", n_miss, n_hit)

        # Build the study plan from the in-scope chapters.
        if GEN.get("cancel"):
            raise ai.GenerationCancelled()
        GEN.update(phase="plan", chapter="", sub_done=0, sub_total=0,
                   message="Building your study plan...")
        subs = conn.execute("SELECT * FROM subjects WHERE user_id=?", (user_id,)).fetchall()
        payload = []
        for s in subs:
            chs = conn.execute(
                "SELECT c.name, c.weight FROM chapters c WHERE c.subject_id=? AND "
                "(c.in_scope=1 OR c.in_scope IS NULL) AND "
                "(EXISTS (SELECT 1 FROM materials m WHERE m.chapter_id=c.id AND m.confirmed=1) "
                " OR c.id IN (%s)) ORDER BY c.order_index"
                % (",".join(str(x) for x in approved_empty_ids) or "0"),
                (s["id"],),
            ).fetchall()
            payload.append({
                "name": s["name"], "exam_date": s["exam_date"] or "",
                "available_time": s["available_time"] or "",
                "importance": (s["importance"] if "importance" in s.keys() and s["importance"] is not None else 2),
                "chapters": [{"name": c["name"], "weight": c["weight"] or 2} for c in chs],
            })
        items = ai.generate_plan(payload, date.today().isoformat())
        # Map (subject, chapter) names back to real chapter ids so Plan links to Study.
        cmap = {}
        for r in conn.execute(
            "SELECT c.id AS cid, c.name AS cname, s.name AS sname FROM chapters c "
            "JOIN subjects s ON c.subject_id=s.id WHERE s.user_id=?", (user_id,)
        ).fetchall():
            cmap[(r["sname"].strip().lower(), r["cname"].strip().lower())] = r["cid"]
        cur = conn.execute("INSERT INTO plans (user_id, generated_at) VALUES (?,?)",
                           (user_id, date.today().isoformat()))
        pid = cur.lastrowid
        order = 0
        # exam date per subject (lowercased name -> date), to keep study OFF exam day
        exam_by_subj = {}
        for s in subs:
            ed = (s["exam_date"] or "").strip()
            try:
                exam_by_subj[s["name"].strip().lower()] = date.fromisoformat(ed)
            except Exception:
                pass
        for it in items:
            subj = (it.get("subject", "") or "").strip()
            chap = (it.get("chapter", "") or "").strip()
            day = (it.get("day", "") or "").strip()
            # Never schedule study ON or AFTER the exam day — the exam day is for the exam.
            ex = exam_by_subj.get(subj.lower())
            if ex:
                try:
                    if date.fromisoformat(day) >= ex:
                        continue
                except Exception:
                    pass
            cid = cmap.get((subj.lower(), chap.lower()))
            conn.execute(
                "INSERT INTO plan_items (plan_id, day, subject, chapter, task, order_index, chapter_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, day, subj, chap, it.get("task", ""), order, cid),
            )
            order += 1

        # ---- Deterministic finish: a practice-exam day and a full final-review day,
        # BOTH strictly before the exam (exam day itself stays study-free).
        from datetime import timedelta as _td
        today_d = date.today()
        for s in subs:
            ed = (s["exam_date"] or "").strip()
            try:
                exam_d = date.fromisoformat(ed)
            except Exception:
                continue
            day_before = exam_d - _td(days=1)
            two_before = exam_d - _td(days=2)
            # review = day before exam; practice = two days before. Both clamped to >= today
            # and ALWAYS strictly before the exam (never on exam day).
            review_d = day_before if day_before >= today_d else min(today_d, day_before)
            practice_d = two_before if two_before >= today_d else min(today_d, two_before)
            # guard: never land on or after exam day
            if review_d >= exam_d:
                review_d = day_before
            if practice_d >= exam_d:
                practice_d = day_before
            # if exam already passed or is today, skip injecting study work
            if exam_d <= today_d:
                continue
            conn.execute(
                "INSERT INTO plan_items (plan_id, day, subject, chapter, task, order_index, chapter_id) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (pid, practice_d.isoformat(), s["name"], "All chapters",
                 "[Practice] Take the practice exam for this subject (Generate practice exam), then review "
                 "every question you missed.", order),
            )
            order += 1
            conn.execute(
                "INSERT INTO plan_items (plan_id, day, subject, chapter, task, order_index, chapter_id) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (pid, review_d.isoformat(), s["name"], "All chapters",
                 "[Final review] Review ALL chapters in scope: must-memorize points, formulas, and the questions "
                 "you missed on the practice exam. No new material today.", order),
            )
            order += 1
        conn.commit()

        # everything is regenerated -> subjects are now up to date
        conn.execute("UPDATE subjects SET dirty=0 WHERE user_id=?", (user_id,))
        conn.commit()
        GEN.update(state="done", message="Done!")
        log.info("Generation finished. plan_id=%s", pid)
    except ai.GenerationCancelled:
        finished = GEN.get("done", 0)
        log.info("Generation CANCELLED by user after %d chapter(s).", finished)
        GEN.update(state="cancelled", error=None,
                   message=f"Stopped. {finished} finished chapter(s) are saved.")
    except ai.AIError as e:
        log.error("Generation FAILED | reason=%s | detail=%s", e.reason, e.detail)
        GEN.update(state="error", error=e.reason, message=e.reason)
    except Exception as e:
        log.exception("Generation FAILED (unexpected)")
        GEN.update(state="error", error=str(e), message="Something went wrong. Check the newest file in the logs folder.")
    finally:
        conn.close()


@app.post("/api/generate/run")
async def api_generate_run(request: Request):
    u = require_user(request)
    if GEN["state"] == "running":
        return {"error": "Generation is already running."}
    body = await request.json()
    approved = set(int(x) for x in body.get("approved_empty", []))
    force = bool(body.get("force", False))
    GEN.update(state="running", done=0, total=0, message="Starting...", error=None, cancel=False)
    t = threading.Thread(target=_run_generation, args=(u["id"], approved, force), daemon=True)
    t.start()
    return {"started": True}


@app.get("/api/generate/progress")
def api_generate_progress(request: Request):
    require_user(request)
    out = dict(GEN)
    # seconds spent on the CURRENT chapter — a heartbeat so a long single sheet write
    # (one big API call, no chunks) still visibly ticks instead of looking frozen.
    if GEN.get("state") == "running" and GEN.get("ch_started_at"):
        out["ch_elapsed"] = int(time.time() - GEN["ch_started_at"])
    else:
        out["ch_elapsed"] = 0
    return out


@app.post("/api/generate/cancel")
def api_generate_cancel(request: Request):
    """Ask the running job to stop. It stops between chapters (and between chunks during
    the read step), so it may take a few seconds. Finished chapters stay saved + cached;
    nothing is corrupted and a later Start resumes from where it left off."""
    require_user(request)
    if GEN.get("state") == "running":
        GEN["cancel"] = True
        GEN.update(message="Stopping after the current step…")
        log.info("Cancel requested by user.")
        return {"stopping": True}
    return {"stopping": False}


# ============================ STUDY DATA & Q&A ===============================
@app.get("/api/study-data")
def api_study_data(request: Request):
    u = require_user(request)
    conn = db.connect()
    try:
        subs = conn.execute("SELECT * FROM subjects WHERE user_id=? ORDER BY id", (u["id"],)).fetchall()
        # newest plan, for merging study dates into each chapter
        plan = conn.execute("SELECT id FROM plans WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        plan_by_chapter = {}
        if plan:
            for pi in conn.execute(
                "SELECT chapter_id, day, task, done FROM plan_items WHERE plan_id=? AND chapter_id IS NOT NULL "
                "ORDER BY day, order_index", (plan["id"],)
            ).fetchall():
                plan_by_chapter.setdefault(pi["chapter_id"], []).append(
                    {"day": pi["day"], "task": pi["task"], "done": bool(pi["done"])})
        out = []
        for s in subs:
            chapters = conn.execute(
                "SELECT * FROM chapters WHERE subject_id=? ORDER BY order_index, id", (s["id"],)
            ).fetchall()
            ch_out = []
            for c in chapters:
                summ = conn.execute(
                    "SELECT content_html, is_ai_generated, studied FROM summaries WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
                    (c["id"],)
                ).fetchone()
                if summ:
                    ch_out.append({
                        "id": c["id"], "name": c["name"],
                        "html": summ["content_html"],
                        "ai_generated": bool(summ["is_ai_generated"]),
                        "studied": bool(summ["studied"]),
                        "plan": plan_by_chapter.get(c["id"], []),
                    })
            if ch_out:
                out.append({"subject": s["name"], "subject_id": s["id"],
                            "exam_date": s["exam_date"] or "", "level": s["level"] or "",
                            "chapters": ch_out})
        return {"subjects": out}
    finally:
        conn.close()


# ============================ MOCK / PRACTICE EXAM ==========================
def _subject_scope_notes(conn, subject_id):
    """Gather content for a practice exam: prefer compressed notes, else chapter sheets,
    else raw material. Returns (chapter_names, notes_blob)."""
    chapters = conn.execute(
        "SELECT id, name FROM chapters WHERE subject_id=? ORDER BY order_index, id", (subject_id,)
    ).fetchall()
    names, blobs = [], []
    for c in chapters:
        names.append(c["name"])
        piece = ""
        # 1) compressed notes for any material in this chapter
        mats = conn.execute(
            "SELECT raw_text FROM materials WHERE chapter_id=? AND confirmed=1", (c["id"],)
        ).fetchall()
        joined = "\n".join((m["raw_text"] or "") for m in mats).strip()
        if joined:
            sh = ai.material_hash(joined)
            nc = conn.execute("SELECT notes_md FROM note_cache WHERE source_hash=?", (sh,)).fetchone()
            piece = nc["notes_md"] if nc else joined[:1500]
        if not piece:
            # 2) fall back to the generated sheet text
            summ = conn.execute(
                "SELECT content_md FROM summaries WHERE chapter_id=? ORDER BY id DESC LIMIT 1", (c["id"],)
            ).fetchone()
            if summ:
                piece = (summ["content_md"] or "")[:1500]
        if piece:
            blobs.append(f"## {c['name']}\n{piece}")
    return names, "\n\n".join(blobs)


@app.post("/api/mock-exam/generate")
async def api_mock_exam_generate(request: Request):
    u = require_user(request)
    body = await request.json()
    subject_id = int(body["subject_id"])
    conn = db.connect()
    try:
        s = conn.execute("SELECT * FROM subjects WHERE id=? AND user_id=?",
                         (subject_id, u["id"])).fetchone()
        if not s:
            return {"ok": False, "error": "Subject not found."}
        names, notes = _subject_scope_notes(conn, subject_id)
        if not names:
            return {"ok": False, "error": "This subject has no chapters yet."}
        try:
            md_text = ai.generate_mock_exam(s["name"], u["grade"], names, notes)
        except ai.AIError as e:
            return {"ok": False, "error": f"{e.reason}"}
        html = md_to_html(md_text)
        n = conn.execute("SELECT COUNT(*) c FROM mock_exams WHERE subject_id=?",
                         (subject_id,)).fetchone()["c"]
        title = f"{s['name']} Practice Exam #{n + 1}"
        eid = conn.execute(
            "INSERT INTO mock_exams (user_id, subject_id, title, content_md, content_html) "
            "VALUES (?,?,?,?,?)",
            (u["id"], subject_id, title, md_text, html),
        ).lastrowid
        conn.commit()
        log.info("Mock exam generated: %s (id=%d)", title, eid)
        return {"ok": True, "id": eid, "title": title}
    finally:
        conn.close()


@app.get("/api/mock-exam/list")
def api_mock_exam_list(request: Request):
    u = require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT e.id, e.subject_id, e.title, e.created_at, s.name AS subject "
            "FROM mock_exams e JOIN subjects s ON s.id=e.subject_id "
            "WHERE e.user_id=? ORDER BY e.id DESC", (u["id"],)
        ).fetchall()
        return {"exams": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/mock-exam/{exam_id}")
def api_mock_exam_get(request: Request, exam_id: int):
    u = require_user(request)
    conn = db.connect()
    try:
        r = conn.execute("SELECT * FROM mock_exams WHERE id=? AND user_id=?",
                         (exam_id, u["id"])).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="not found")
        return {"id": r["id"], "title": r["title"], "html": r["content_html"],
                "subject_id": r["subject_id"]}
    finally:
        conn.close()


@app.delete("/api/mock-exam/{exam_id}")
def api_mock_exam_delete(request: Request, exam_id: int):
    u = require_user(request)
    conn = db.connect()
    try:
        conn.execute("DELETE FROM mock_exams WHERE id=? AND user_id=?", (exam_id, u["id"]))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/qa/{chapter_id}")
def api_qa_history(request: Request, chapter_id: int):
    require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT role, content, ts FROM qa_history WHERE chapter_id=? ORDER BY id", (chapter_id,)
        ).fetchall()
        return {"history": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/qa")
async def api_qa(request: Request):
    require_user(request)
    body = await request.json()
    chapter_id = int(body["chapter_id"])
    question = body["question"].strip()
    conn = db.connect()
    try:
        ch = conn.execute("SELECT name FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        summ = conn.execute(
            "SELECT content_md FROM summaries WHERE chapter_id=? ORDER BY id DESC LIMIT 1", (chapter_id,)
        ).fetchone()
        hist = conn.execute(
            "SELECT role, content FROM qa_history WHERE chapter_id=? ORDER BY id", (chapter_id,)
        ).fetchall()
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        answer = ai.answer_question(
            ch["name"] if ch else "", summ["content_md"] if summ else "",
            [dict(h) for h in hist], question,
        )
        conn.execute("INSERT INTO qa_history (chapter_id, role, content, ts) VALUES (?,?,?,?)",
                     (chapter_id, "user", question, now))
        conn.execute("INSERT INTO qa_history (chapter_id, role, content, ts) VALUES (?,?,?,?)",
                     (chapter_id, "ai", answer, now))
        conn.commit()
        return {"answer": answer}
    finally:
        conn.close()


# ============================ PLAN ===========================================
@app.get("/api/plan")
def api_plan(request: Request):
    u = require_user(request)
    conn = db.connect()
    try:
        plan = conn.execute(
            "SELECT * FROM plans WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],)
        ).fetchone()
        if not plan:
            return {"items": [], "generated_at": None}
        items = conn.execute(
            "SELECT * FROM plan_items WHERE plan_id=? ORDER BY day, order_index", (plan["id"],)
        ).fetchall()
        exams = conn.execute(
            "SELECT name, exam_date FROM subjects WHERE user_id=? "
            "AND exam_date IS NOT NULL AND exam_date<>'' ORDER BY exam_date", (u["id"],)
        ).fetchall()
        return {"items": [dict(i) for i in items], "generated_at": plan["generated_at"],
                "exams": [{"subject": e["name"], "date": e["exam_date"]} for e in exams]}
    finally:
        conn.close()


@app.post("/api/chapter/weight")
async def api_chapter_weight(request: Request):
    """Set a chapter's exam importance (1=low, 2=medium, 3=high). Free, no AI."""
    u = require_user(request)
    body = await request.json()
    cid = int(body["chapter_id"])
    w = int(body.get("weight", 2))
    w = 1 if w < 1 else 3 if w > 3 else w
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT c.id, c.subject_id FROM chapters c JOIN subjects s ON s.id=c.subject_id "
            "WHERE c.id=? AND s.user_id=?", (cid, u["id"])).fetchone()
        if not row:
            return {"ok": False, "error": "Chapter not found."}
        conn.execute("UPDATE chapters SET weight=? WHERE id=?", (w, cid))
        # importance changes the ideal plan -> mark the subject as needing a rebuild
        conn.execute("UPDATE subjects SET dirty=1 WHERE id=?", (row["subject_id"],))
        conn.commit()
        return {"ok": True, "weight": w}
    finally:
        conn.close()


@app.post("/api/plan/reschedule")
async def api_plan_reschedule(request: Request):
    """Dynamic reallocation (pure Python, NO AI, NO cost).

    Takes every UNFINISHED Pass-1/Pass-2 task (overdue or upcoming) and spreads it across
    the days still left before each exam, giving higher-importance chapters earlier/denser
    slots. Finished (checked) tasks are frozen and never moved. The practice-exam day
    (exam-2) and final-review day (exam-1) are re-placed for the new schedule."""
    from datetime import timedelta as _td
    u = require_user(request)
    uid = u["id"]
    conn = db.connect()
    try:
        plan = conn.execute(
            "SELECT id FROM plans WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not plan:
            return {"ok": False, "error": "No plan yet — generate one first."}
        pid = plan["id"]
        today = date.today()

        subs = conn.execute("SELECT * FROM subjects WHERE user_id=?", (uid,)).fetchall()
        exam_by = {}
        for s in subs:
            try:
                exam_by[s["name"].strip().lower()] = date.fromisoformat((s["exam_date"] or "").strip())
            except Exception:
                pass
        weight_by_cid = {r["id"]: (r["weight"] or 2) for r in conn.execute(
            "SELECT c.id, c.weight FROM chapters c JOIN subjects s ON s.id=c.subject_id "
            "WHERE s.user_id=?", (uid,)).fetchall()}

        items = conn.execute(
            "SELECT * FROM plan_items WHERE plan_id=? ORDER BY day, order_index", (pid,)).fetchall()

        def _is_final_phase(t):
            t = t or ""
            return t.startswith("[Practice]") or t.startswith("[Final review]")

        # movable = unfinished, non-final-phase tasks, grouped by subject
        movable_by_subj = {}
        for it in items:
            if it["done"] or _is_final_phase(it["task"]):
                continue
            movable_by_subj.setdefault((it["subject"] or "").strip().lower(), []).append(it)

        import math

        def _daterange(a, b):
            out, d = [], a
            while d <= b:
                out.append(d)
                d += _td(days=1)
            return out

        # drop unfinished practice/final rows -> we re-inject them with fresh dates
        conn.execute(
            "DELETE FROM plan_items WHERE plan_id=? AND done=0 AND "
            "(task LIKE '[Practice]%' OR task LIKE '[Final review]%')", (pid,))

        order = (conn.execute(
            "SELECT COALESCE(MAX(order_index),0) FROM plan_items WHERE plan_id=?",
            (pid,)).fetchone()[0]) + 1
        moved_total = 0
        summary = []

        for subj_key, subj_items in movable_by_subj.items():
            exam = exam_by.get(subj_key)
            subj_name = subj_items[0]["subject"]
            if not exam or exam <= today:
                continue  # no exam date or already passed -> leave those tasks as-is

            # study window: today .. exam-3 (reserve exam-2 practice, exam-1 review, exam day)
            study_days = _daterange(today, exam - _td(days=3))
            tight = False
            if not study_days:                       # tight: fold into today .. exam-2
                study_days = _daterange(today, exam - _td(days=2)); tight = True
            if not study_days:                       # very tight: cram today
                study_days = [today]; tight = True

            # heavier chapters first (earlier days), then original order
            subj_items.sort(key=lambda it: (-int(weight_by_cid.get(it["chapter_id"], 2)),
                                            it["order_index"]))
            per_day = math.ceil(len(subj_items) / len(study_days))
            if per_day > 3:
                tight = True
            for idx, it in enumerate(subj_items):
                d = study_days[min(idx // per_day, len(study_days) - 1)]
                conn.execute("UPDATE plan_items SET day=?, order_index=? WHERE id=?",
                             (d.isoformat(), order, it["id"]))
                order += 1
                moved_total += 1

            practice_d = max(exam - _td(days=2), today)
            review_d = max(exam - _td(days=1), today)
            if practice_d >= exam:
                practice_d = exam - _td(days=1)
            if review_d >= exam:
                review_d = exam - _td(days=1)
            conn.execute(
                "INSERT INTO plan_items (plan_id, day, subject, chapter, task, order_index, chapter_id) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (pid, practice_d.isoformat(), subj_name, "All chapters",
                 "[Practice] Take the practice exam for this subject (Generate practice exam), then review "
                 "every question you missed.", order))
            order += 1
            conn.execute(
                "INSERT INTO plan_items (plan_id, day, subject, chapter, task, order_index, chapter_id) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (pid, review_d.isoformat(), subj_name, "All chapters",
                 "[Final review] Review ALL chapters in scope: must-memorize points, formulas, and the questions "
                 "you missed on the practice exam. No new material today.", order))
            order += 1

            summary.append({"subject": subj_name, "tasks_moved": len(subj_items),
                            "days_left": len(study_days), "tight": tight})

        conn.commit()
        log.info("Plan rescheduled | user=%s | %d tasks across %d subjects",
                 uid, moved_total, len(summary))
        return {"ok": True, "moved": moved_total, "subjects": summary,
                "message": "Rescheduled unfinished tasks across the days you have left."}
    finally:
        conn.close()


@app.post("/api/plan/item")
async def api_plan_item(request: Request):
    require_user(request)
    body = await request.json()
    iid = body.get("id")
    conn = db.connect()
    try:
        if body.get("delete"):
            conn.execute("DELETE FROM plan_items WHERE id=?", (iid,))
        elif iid:
            conn.execute(
                "UPDATE plan_items SET day=?, subject=?, chapter=?, task=?, done=? WHERE id=?",
                (body.get("day", ""), body.get("subject", ""), body.get("chapter", ""),
                 body.get("task", ""), 1 if body.get("done") else 0, iid),
            )
        else:
            plan = conn.execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()
            if plan:
                conn.execute(
                    "INSERT INTO plan_items (plan_id, day, subject, chapter, task) VALUES (?,?,?,?,?)",
                    (plan["id"], body.get("day", ""), body.get("subject", ""),
                     body.get("chapter", ""), body.get("task", "")),
                )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ============================ DB BACKUP (export / import) ====================
# Data-only backup as JSON. This survives schema changes: on import we insert
# only the columns that still exist in the current database, so adding/removing
# columns in a new version of the program does not break the restore.
import json as _json
from datetime import datetime as _dt


def _all_tables(conn):
    return [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]


def _table_columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


@app.get("/api/db/export")
def api_db_export(request: Request):
    """Download every table as one JSON backup file."""
    require_user(request)
    conn = db.connect()
    try:
        dump = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
                for t in _all_tables(conn)}
        payload = {"app": "StudyBridge",
                   "exported_at": _dt.now().isoformat(timespec="seconds"),
                   "tables": dump}
        body = _json.dumps(payload, ensure_ascii=False, indent=2)
        fname = f"studybridge_backup_{date.today().isoformat()}.json"
        log.info("DB export: %d tables, %d bytes", len(dump), len(body))
        return Response(content=body, media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    finally:
        conn.close()


@app.get("/api/book/{subject_id}")
def api_book(subject_id: int, request: Request):
    """Study Book PDF for one subject: cover, contents, about page, every chapter with its
    practice problems, answer key, ending page. Built only from existing sheets: no AI calls, no cost."""
    u = require_user(request)
    from . import book
    conn = db.connect()
    t0 = time.time()
    try:
        pdf, fname = book.build_book(conn, u["id"], subject_id)
    except book.BookError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.error("Study book failed | subject=%s | %s\n%s", subject_id, e, traceback.format_exc())
        return JSONResponse({"error": "Could not build the book. Check the log for details."}, status_code=500)
    finally:
        conn.close()
    log.info("Study book built | subject=%s | %d bytes | %.1fs", subject_id, len(pdf), time.time() - t0)
    return _pdf_response(pdf, fname)


def _pdf_response(pdf, fname):
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/problem-set/options")
def api_problem_set_options(request: Request, subject_id: int):
    """Counts the Problem Set panel shows: per chapter practice / quiz / missed, plus saved exams."""
    u = require_user(request)
    from . import problemset, book
    conn = db.connect()
    try:
        return problemset.options(conn, u["id"], subject_id)
    except book.BookError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()


@app.post("/api/problem-set")
async def api_problem_set(request: Request):
    """Problem Set PDF from stored problems only (sheet practice, past quizzes, missed questions).
    No AI calls, no cost."""
    u = require_user(request)
    body = await request.json()
    from . import problemset, book
    conn = db.connect()
    t0 = time.time()
    try:
        pdf, fname = problemset.build_problem_set(
            conn, u["id"], int(body["subject_id"]),
            chapter_ids=body.get("chapter_ids") or None,
            sources=body.get("sources") or ["practice", "quiz", "missed"],
            answer_key=bool(body.get("answer_key", True)),
            shuffle=bool(body.get("shuffle", False)))
    except book.BookError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.error("Problem set failed | %s\n%s", e, traceback.format_exc())
        return JSONResponse({"error": "Could not build the problem set. Check the log for details."}, status_code=500)
    finally:
        conn.close()
    log.info("Problem set built | subject=%s | %d bytes | %.1fs", body.get("subject_id"), len(pdf), time.time() - t0)
    return _pdf_response(pdf, fname)


@app.get("/api/exam-pdf/{exam_id}")
def api_exam_pdf(request: Request, exam_id: int, answers: int = 1):
    """A saved practice exam as a printable paper. No AI calls."""
    u = require_user(request)
    from . import problemset, book
    conn = db.connect()
    try:
        pdf, fname = problemset.build_exam_pdf(conn, u["id"], exam_id, answer_key=bool(answers))
    except book.BookError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.error("Exam PDF failed | exam=%s | %s\n%s", exam_id, e, traceback.format_exc())
        return JSONResponse({"error": "Could not build the exam PDF. Check the log for details."}, status_code=500)
    finally:
        conn.close()
    return _pdf_response(pdf, fname)


@app.post("/api/db/import")
async def api_db_import(request: Request, file: UploadFile = File(...)):
    """Restore from a JSON backup. Replaces current data, keeping only columns
    that still exist in this version's schema."""
    require_user(request)
    raw = await file.read()
    payload = None
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr", "latin-1"):
        try:
            payload = _json.loads(raw.decode(enc))
            tables = payload["tables"]
            log.info("DB import decoded as %s", enc)
            break
        except Exception:
            payload = None
            continue
    if payload is None:
        return {"ok": False, "error": "Not a valid StudyBridge backup file (could not read its text)."}
    try:
        tables = payload["tables"]
    except Exception as e:
        return {"ok": False, "error": f"Not a valid StudyBridge backup file: {e}"}

    conn = db.connect()
    summary = {}
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        existing = set(_all_tables(conn))
        for t, rows in tables.items():
            if t not in existing:
                log.info("Import: skipping table '%s' (not in this version)", t)
                continue
            cols_now = set(_table_columns(conn, t))
            conn.execute(f"DELETE FROM {t}")
            inserted = 0
            for row in rows:
                use = {k: v for k, v in row.items() if k in cols_now}
                if not use:
                    continue
                cols = ",".join(use.keys())
                ph = ",".join("?" for _ in use)
                conn.execute(f"INSERT INTO {t} ({cols}) VALUES ({ph})", list(use.values()))
                inserted += 1
            summary[t] = inserted
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        log.info("DB import done: %s", summary)
        return {"ok": True, "summary": summary}
    except Exception as e:
        conn.rollback()
        log.exception("DB import failed")
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


# ============================ v2: PRESETS / ONBOARDING =======================
@app.get("/api/presets")
def api_presets(request: Request, grade: str = ""):
    """Grouped course catalog for the picker. grade: 0(K)..12."""
    require_user(request)
    g = grade if grade != "" else "9"
    school_level, groups = presets.courses_for_grade(g)
    return {"school_level": school_level, "groups": groups}


@app.post("/api/onboard/preset")
async def api_onboard_preset(request: Request):
    """Create subjects from picked preset courses, then AI-generate their chapter lists.
    body: {grade, courses:[{name, level, exam_date}]}"""
    u = require_user(request)
    body = await request.json()
    grade = str(body.get("grade", u["grade"] or ""))
    courses = body.get("courses", [])
    if not courses:
        return {"error": "Pick at least one course."}
    conn = db.connect()
    created = []
    try:
        for c in courses:
            base = (c.get("name") or "").strip()
            if not base:
                continue
            chosen = (c.get("level") or "").strip()
            exam_date = (c.get("exam_date") or "").strip()
            # Resolve a clean display name + tier from the chosen level option.
            if chosen in ("Standard", "Honors"):
                name, tier = base, chosen
            elif chosen == "AP":
                name, tier = f"AP {base}", "AP"
            elif chosen in ("AP Language", "AP Literature"):
                name, tier = f"AP English {chosen.split()[-1]}", "AP"
            elif chosen:
                name = chosen
                tier = "AP" if chosen.startswith("AP") else "Standard"
            else:
                name, tier = base, ""
            row = conn.execute("SELECT id FROM subjects WHERE user_id=? AND lower(name)=lower(?)",
                               (u["id"], name)).fetchone()
            if row:
                continue
            try:
                imp = int(c.get("importance", 2))
            except (TypeError, ValueError):
                imp = 2
            imp = 1 if imp < 1 else 3 if imp > 3 else imp
            cur = conn.execute(
                "INSERT INTO subjects (user_id, name, exam_date, exam_scope, level, importance, syllabus_source, dirty) "
                "VALUES (?,?,?,?,?,?, 'ai', 1)",
                (u["id"], name, exam_date, "ALL", tier, imp))
            sid = cur.lastrowid
            try:
                chs = ai.generate_syllabus(name, grade, "ALL", tier)
            except Exception:
                log.exception("preset syllabus gen failed for %s", name)
                chs = []
            for i, nm in enumerate(chs):
                conn.execute("INSERT INTO chapters (subject_id, name, order_index) VALUES (?,?,?)",
                             (sid, nm, i))
            created.append({"subject": name, "chapters": len(chs)})
        conn.commit()
        log.info("Onboard preset: %d subjects created", len(created))
        return {"created": created}
    finally:
        conn.close()


# ============================ v2: RECENT (resume) ============================
@app.post("/api/recent/touch")
async def api_recent_touch(request: Request):
    u = require_user(request)
    body = await request.json()
    cid = int(body["chapter_id"])
    conn = db.connect()
    try:
        from datetime import datetime as _dtt
        conn.execute(
            "INSERT INTO recent_views (user_id, chapter_id, viewed_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id, chapter_id) DO UPDATE SET viewed_at=excluded.viewed_at",
            (u["id"], cid, _dtt.now().isoformat(timespec="seconds")))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/recent")
def api_recent(request: Request):
    u = require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT r.chapter_id AS cid, c.name AS chapter, s.id AS sid, s.name AS subject "
            "FROM recent_views r JOIN chapters c ON c.id=r.chapter_id "
            "JOIN subjects s ON s.id=c.subject_id "
            "WHERE r.user_id=? ORDER BY r.viewed_at DESC LIMIT 8", (u["id"],)
        ).fetchall()
        return {"recent": [dict(r) for r in rows]}
    finally:
        conn.close()


# ============================ v2: chapter source helper =====================
def _chapter_source_text(conn, chapter_id):
    """Best available study text for a chapter: compressed notes -> sheet md -> raw."""
    mats = conn.execute("SELECT raw_text FROM materials WHERE chapter_id=? AND confirmed=1",
                        (chapter_id,)).fetchall()
    joined = "\n".join((m["raw_text"] or "") for m in mats).strip()
    if joined:
        sh = ai.material_hash(joined)
        nc = conn.execute("SELECT notes_md FROM note_cache WHERE source_hash=?", (sh,)).fetchone()
        if nc:
            return nc["notes_md"]
    summ = conn.execute("SELECT content_md FROM summaries WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
                       (chapter_id,)).fetchone()
    if summ and summ["content_md"]:
        return summ["content_md"]
    return joined


def _chapter_ctx(conn, chapter_id):
    return conn.execute(
        "SELECT c.id AS cid, c.name AS cname, s.name AS sname FROM chapters c "
        "JOIN subjects s ON s.id=c.subject_id WHERE c.id=?", (chapter_id,)).fetchone()


# ============================ v3: SPACED REPETITION / STREAK ================
# Leitner boxes: box 0 -> review in 1 day, then 3, 7, 14, 30. Simple, no AI, no cost.
SRS_INTERVALS = [1, 3, 7, 14, 30]


def _mark_study_day(conn, user_id):
    """Record that the student did real study work TODAY (drives the streak)."""
    conn.execute("INSERT OR IGNORE INTO study_days (user_id, day) VALUES (?,?)",
                 (user_id, date.today().isoformat()))


def _streak(conn, user_id):
    """Current consecutive-day streak ending today (or yesterday), and whether today counts."""
    from datetime import timedelta as _td
    days = set(r["day"] for r in conn.execute(
        "SELECT day FROM study_days WHERE user_id=?", (user_id,)).fetchall())
    today = date.today()
    studied_today = today.isoformat() in days
    # start from today if done, else from yesterday, so a not-yet-studied today doesn't zero it
    cur = today if studied_today else today - _td(days=1)
    n = 0
    while cur.isoformat() in days:
        n += 1
        cur -= _td(days=1)
    return {"current": n, "studied_today": studied_today}


def _srs_add(conn, user_id, kind, ref_id, chapter_id):
    """Schedule a new review item, due today (box 0). Ignores duplicates."""
    conn.execute(
        "INSERT OR IGNORE INTO srs_items (user_id, kind, ref_id, chapter_id, box, next_review) "
        "VALUES (?,?,?,?,0,?)",
        (user_id, kind, ref_id, chapter_id, date.today().isoformat()))


@app.get("/api/streak")
def api_streak(request: Request):
    u = require_user(request)
    conn = db.connect()
    try:
        st = _streak(conn, u["id"])
        due = conn.execute(
            "SELECT COUNT(*) c FROM srs_items WHERE user_id=? AND next_review<=?",
            (u["id"], date.today().isoformat())).fetchone()["c"]
        st["review_due"] = due
        return st
    finally:
        conn.close()


@app.get("/api/review/due")
def api_review_due(request: Request, limit: int = 40):
    """All review items due today, as recall cards. NO AI, NO cost.
    'weak' items pull their question/answer from weak_spots; 'flashcard' items pull
    front/back from the chapter's stored cards by index."""
    import json as _j
    u = require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT i.id AS item_id, i.kind, i.ref_id, i.chapter_id, i.box, c.name AS chapter "
            "FROM srs_items i JOIN chapters c ON c.id=i.chapter_id "
            "WHERE i.user_id=? AND i.next_review<=? ORDER BY i.next_review, i.id",
            (u["id"], date.today().isoformat())).fetchall()
        # cache each chapter's flashcards once
        fc_cache = {}
        out = []
        for r in rows:
            front = back = ""
            if r["kind"] == "weak":
                w = conn.execute("SELECT question, note, resolved FROM weak_spots WHERE id=?",
                                 (r["ref_id"],)).fetchone()
                if not w or w["resolved"]:
                    continue
                front, back = (w["question"] or ""), (w["note"] or "")
            else:  # flashcard
                cid = r["chapter_id"]
                if cid not in fc_cache:
                    fr = conn.execute(
                        "SELECT cards_json FROM flashcards WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
                        (cid,)).fetchone()
                    try:
                        fc_cache[cid] = _j.loads(fr["cards_json"]) if fr else []
                    except Exception:
                        fc_cache[cid] = []
                cards = fc_cache[cid]
                if r["ref_id"] >= len(cards):
                    continue  # stale (cards were regenerated) -> skip
                front = cards[r["ref_id"]].get("front", "")
                back = cards[r["ref_id"]].get("back", "")
            out.append({"item_id": r["item_id"], "kind": r["kind"], "chapter": r["chapter"],
                        "front": front, "back": back})
            if len(out) >= max(1, min(100, limit)):
                break
        return {"ok": True, "count": len(out), "cards": out}
    finally:
        conn.close()


@app.post("/api/review/grade")
async def api_review_grade(request: Request):
    """Grade one review item: remembered -> next box (longer interval); forgot -> box 0.
    Also counts as study activity for today's streak."""
    from datetime import timedelta as _td
    u = require_user(request)
    body = await request.json()
    iid = int(body["item_id"])
    remembered = bool(body.get("remembered"))
    conn = db.connect()
    try:
        it = conn.execute("SELECT box FROM srs_items WHERE id=? AND user_id=?",
                          (iid, u["id"])).fetchone()
        if not it:
            return {"ok": False, "error": "not found"}
        box = (it["box"] + 1) if remembered else 0
        box = max(0, min(box, len(SRS_INTERVALS) - 1))
        nxt = (date.today() + _td(days=SRS_INTERVALS[box])).isoformat()
        conn.execute("UPDATE srs_items SET box=?, next_review=? WHERE id=?", (box, nxt, iid))
        _mark_study_day(conn, u["id"])
        conn.commit()
        return {"ok": True, "next_review": nxt, "box": box}
    finally:
        conn.close()


# ============================ v2: QUICK QUIZ ================================
@app.post("/api/quiz/generate")
async def api_quiz_generate(request: Request):
    u = require_user(request)
    body = await request.json()
    cid = int(body["chapter_id"])
    conn = db.connect()
    try:
        ctx = _chapter_ctx(conn, cid)
        if not ctx:
            return {"ok": False, "error": "Chapter not found."}
        src = _chapter_source_text(conn, cid)
        # adaptive difficulty: read this chapter's current level, and feed the open
        # weak-spot questions so the quiz drills what the student keeps missing.
        lvl_row = conn.execute("SELECT quiz_level FROM chapters WHERE id=?", (cid,)).fetchone()
        difficulty = (lvl_row["quiz_level"] if lvl_row and lvl_row["quiz_level"] else 1)
        weak = [w["question"] for w in conn.execute(
            "SELECT question FROM weak_spots WHERE user_id=? AND chapter_id=? AND resolved=0 "
            "ORDER BY id DESC LIMIT 8", (u["id"], cid)).fetchall() if w["question"]]
        try:
            qs = ai.generate_quiz(ctx["cname"], ctx["sname"], u["grade"], src,
                                  n=int(body.get("n", 8)), difficulty=difficulty, weak_questions=weak)
        except ai.AIError as e:
            return {"ok": False, "error": e.reason}
        if not qs:
            return {"ok": False, "error": "Couldn't build a quiz for this chapter."}
        import json as _j
        conn.execute("INSERT INTO quizzes (chapter_id, questions_json) VALUES (?,?)",
                     (cid, _j.dumps(qs, ensure_ascii=False)))
        conn.commit()
        # send questions WITHOUT answers to the client
        public = [{"q": q["q"], "choices": q["choices"]} for q in qs]
        return {"ok": True, "questions": public, "answers": [q["answer_index"] for q in qs],
                "explanations": [q["explanation"] for q in qs], "difficulty": difficulty}
    finally:
        conn.close()


@app.post("/api/quiz/grade")
async def api_quiz_grade(request: Request):
    """Record wrong answers as weak spots. body: {chapter_id, wrong:[{question, note}]}"""
    u = require_user(request)
    body = await request.json()
    cid = int(body["chapter_id"])
    wrong = body.get("wrong", [])
    total = int(body.get("total", 0) or 0)
    correct = int(body.get("correct", 0) or 0)
    conn = db.connect()
    try:
        for w in wrong:
            cur = conn.execute(
                "INSERT INTO weak_spots (user_id, chapter_id, question, note) VALUES (?,?,?,?)",
                (u["id"], cid, (w.get("question") or "")[:500], (w.get("note") or "")[:500]))
            # schedule each missed question for spaced review
            _srs_add(conn, u["id"], "weak", cur.lastrowid, cid)
        # ADAPTIVE DIFFICULTY: move this chapter's quiz level from the score.
        lvl_row = conn.execute("SELECT quiz_level FROM chapters WHERE id=?", (cid,)).fetchone()
        cur_lvl = (lvl_row["quiz_level"] if lvl_row and lvl_row["quiz_level"] else 1)
        new_lvl = cur_lvl
        if total > 0:
            acc = correct / total
            if acc >= 0.8:
                new_lvl = min(3, cur_lvl + 1)
            elif acc <= 0.4:
                new_lvl = max(1, cur_lvl - 1)
            conn.execute(
                "INSERT INTO quiz_results (user_id, chapter_id, difficulty, correct, total) VALUES (?,?,?,?,?)",
                (u["id"], cid, cur_lvl, correct, total))
            conn.execute("UPDATE chapters SET quiz_level=? WHERE id=?", (new_lvl, cid))
        _mark_study_day(conn, u["id"])
        conn.commit()
        log.info("Quiz graded | chapter=%s | %d weak | acc=%s/%s | lvl %s->%s",
                 cid, len(wrong), correct, total, cur_lvl, new_lvl)
        return {"ok": True, "next_difficulty": new_lvl}
    finally:
        conn.close()


@app.get("/api/weak-spots/retest")
def api_weak_retest(request: Request, subject_id: int = 0, limit: int = 20):
    """Free adaptive review (NO AI, NO cost): re-serve the questions you got wrong
    (stored weak spots) as shuffled recall cards — see the question, recall the answer,
    then reveal the stored correct answer. Mark mastered ones with /api/weak-spots/resolve."""
    u = require_user(request)
    conn = db.connect()
    try:
        sql = ("SELECT w.id, w.chapter_id, w.question, w.note, c.name AS chapter "
               "FROM weak_spots w JOIN chapters c ON c.id=w.chapter_id "
               "WHERE w.user_id=? AND w.resolved=0 ")
        args = [u["id"]]
        if subject_id:
            sql += "AND c.subject_id=? "
            args.append(subject_id)
        sql += "ORDER BY w.id DESC LIMIT 200"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        import random as _r
        _r.shuffle(rows)
        lim = 1 if limit < 1 else 100 if limit > 100 else limit
        return {"ok": True, "count": min(len(rows), lim), "cards": rows[:lim]}
    finally:
        conn.close()


@app.get("/api/weak-spots")
def api_weak_spots(request: Request, subject_id: int = 0):
    """Open weak spots, optionally filtered to one subject."""
    u = require_user(request)
    conn = db.connect()
    try:
        if subject_id:
            rows = conn.execute(
                "SELECT w.id, w.chapter_id, w.question, w.note, c.name AS chapter "
                "FROM weak_spots w JOIN chapters c ON c.id=w.chapter_id "
                "WHERE w.user_id=? AND w.resolved=0 AND c.subject_id=? ORDER BY w.id DESC",
                (u["id"], subject_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT w.id, w.chapter_id, w.question, w.note, c.name AS chapter, s.name AS subject "
                "FROM weak_spots w JOIN chapters c ON c.id=w.chapter_id "
                "JOIN subjects s ON s.id=c.subject_id "
                "WHERE w.user_id=? AND w.resolved=0 ORDER BY w.id DESC LIMIT 100", (u["id"],)).fetchall()
        return {"weak_spots": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/weak-spots/resolve")
async def api_weak_resolve(request: Request):
    u = require_user(request)
    body = await request.json()
    wid = int(body["id"])
    conn = db.connect()
    try:
        conn.execute("UPDATE weak_spots SET resolved=1 WHERE id=? AND user_id=?", (wid, u["id"]))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ============================ v2: FLASHCARDS ================================
@app.post("/api/flashcards/generate")
async def api_flashcards_generate(request: Request):
    u = require_user(request)
    body = await request.json()
    cid = int(body["chapter_id"])
    conn = db.connect()
    try:
        ctx = _chapter_ctx(conn, cid)
        if not ctx:
            return {"ok": False, "error": "Chapter not found."}
        src = _chapter_source_text(conn, cid)
        try:
            cards = ai.generate_flashcards(ctx["cname"], ctx["sname"], u["grade"], src,
                                           n=int(body.get("n", 12)))
        except ai.AIError as e:
            return {"ok": False, "error": e.reason}
        if not cards:
            return {"ok": False, "error": "Couldn't build flashcards for this chapter."}
        import json as _j
        conn.execute("DELETE FROM flashcards WHERE chapter_id=?", (cid,))
        conn.execute("INSERT INTO flashcards (chapter_id, cards_json) VALUES (?,?)",
                     (cid, _j.dumps(cards, ensure_ascii=False)))
        # reset this chapter's flashcard review schedule to match the new cards, then
        # schedule every new card for spaced review (due today, box 0).
        conn.execute("DELETE FROM srs_items WHERE user_id=? AND kind='flashcard' AND chapter_id=?",
                     (u["id"], cid))
        for i in range(len(cards)):
            _srs_add(conn, u["id"], "flashcard", i, cid)
        conn.commit()
        return {"ok": True, "cards": cards}
    finally:
        conn.close()


@app.get("/api/flashcards/{chapter_id}")
def api_flashcards_get(request: Request, chapter_id: int):
    require_user(request)
    conn = db.connect()
    try:
        row = conn.execute("SELECT cards_json FROM flashcards WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
                          (chapter_id,)).fetchone()
        import json as _j
        return {"cards": _j.loads(row["cards_json"]) if row else []}
    finally:
        conn.close()


# ============================ v2: mark a chapter studied ====================
@app.post("/api/chapter/studied")
async def api_chapter_studied(request: Request):
    u = require_user(request)
    body = await request.json()
    cid = int(body["chapter_id"])
    val = 1 if body.get("studied", True) else 0
    conn = db.connect()
    try:
        conn.execute("UPDATE summaries SET studied=? WHERE chapter_id=?", (val, cid))
        if val:
            _mark_study_day(conn, u["id"])   # marking a chapter studied counts toward the streak
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ============================ v2: profile (grade) ===========================
@app.post("/api/user/grade")
async def api_user_grade(request: Request):
    u = require_user(request)
    body = await request.json()
    grade = str(body.get("grade", "")).strip()
    conn = db.connect()
    try:
        conn.execute("UPDATE users SET grade=? WHERE id=?", (grade, u["id"]))
        conn.commit()
        log.info("User grade updated -> %s", grade)
        return {"ok": True, "grade": grade}
    finally:
        conn.close()
