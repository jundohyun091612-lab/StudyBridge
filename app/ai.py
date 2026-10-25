"""
ai.py
- The single place that talks to OpenAI.
- Every function logs what it did (model, time taken, success/failure) so problems are
  easy to trace from the log file. It never logs full document text (only sizes).

Design rules we agreed on:
  * No caching, but also no duplicate calls: each function is called once per real need.
  * Summaries must NOT invent facts -> we instruct the model to use ONLY the given material.
  * Syllabus generation and empty-chapter generation rely on the model's own knowledge
    (no student material exists), so those are clearly marked elsewhere as "AI generated".
"""
import base64
import hashlib
import inspect
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

from . import config
from .logger import get_logger

log = get_logger()


class GenerationCancelled(Exception):
    """Raised when the user presses Stop during background generation.

    It is NOT an error: chapters already finished are committed + cached, so a
    later run resumes from where it left off. The worker catches this and reports
    a 'cancelled' state rather than an 'error' state.
    """
    pass

_client = None
_key_cached = None


def prompt_version() -> str:
    """Auto-derived version tag for the cache key.

    It is a short hash of the SOURCE CODE of the generation functions below. If you edit
    the prompt or the generation logic, this hash changes automatically, which changes the
    cache key, which invalidates old cached sheets. That's the whole point: you can never get
    a stale result from changed code, and you never have to remember to bump a version number.
    """
    try:
        src = "".join(
            inspect.getsource(fn)
            for fn in (compress_material, _compress_chunk, summarize_chapter)
        )
    except Exception:
        src = "v0"
    return "p" + hashlib.sha256(src.encode("utf-8")).hexdigest()[:10]


def material_hash(material_text: str) -> str:
    """Stable hash of a chapter's material content (order-independent at the call site)."""
    return hashlib.sha256((material_text or "").encode("utf-8")).hexdigest()[:16]


def cache_key(source_hash: str, model: str) -> str:
    """The full cache key: material + prompt/code version + model. Any change busts it."""
    raw = f"{source_hash}|{prompt_version()}|{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def get_client():
    """Build the OpenAI client from the key file. Raises a clear error if the key is missing."""
    global _client, _key_cached
    key = config.load_openai_key()
    if not key:
        raise RuntimeError(
            "OpenAI key not found. Put your key inside OpenAI_Key.txt "
            "in the same folder as run.ps1."
        )
    if _client is None or key != _key_cached:
        _client = OpenAI(api_key=key)
        _key_cached = key
        masked = (key[:7] + "…" + key[-4:]) if len(key) > 14 else "set"
        log.info("OpenAI client ready | key=%s | models: match=%s summary=%s plan=%s vision=%s",
                 masked, config.MODEL_MATCH, config.MODEL_SUMMARY, config.MODEL_PLAN, config.MODEL_VISION)
    return _client


class AIError(Exception):
    """Raised when an AI call ultimately fails. Carries a short human-readable reason
    (for the UI) and the full technical detail (already written to the log)."""
    def __init__(self, reason, detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _err_detail(e) -> dict:
    """Pull every useful field out of an OpenAI/SDK exception for logging."""
    d = {"type": type(e).__name__, "status": getattr(e, "status_code", None),
         "code": getattr(e, "code", None), "request_id": getattr(e, "request_id", None),
         "message": str(e)}
    # The real reason (insufficient_quota / rate_limit_exceeded / etc.) is usually in .body
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            d["code"] = err.get("code") or d["code"]
            d["err_type"] = err.get("type")
            d["message"] = err.get("message") or d["message"]
    return d


def _human_reason(d: dict) -> str:
    code = (d.get("code") or "") or (d.get("err_type") or "")
    code = str(code).lower()
    status = d.get("status")
    if "insufficient_quota" in code or "billing" in code:
        return "Your OpenAI account is out of credit (insufficient_quota). Add credit at platform.openai.com → Billing."
    if "rate_limit" in code or status == 429:
        return "Hit OpenAI’s per-minute rate limit. Wait a moment and retry."
    if "invalid_api_key" in code or "authentication" in code or status == 401:
        return "Your OpenAI API key looks invalid. Check OpenAI_Key.txt."
    if "model_not_found" in code or status == 404:
        return "The configured model isn’t available to your key."
    if "context_length" in code or "string_above_max_length" in code:
        return "The input was too long for the model."
    if status and status >= 500:
        return "OpenAI had a server error. Retry shortly."
    return "The AI service couldn’t complete the request."


def _retry_wait(exc, attempt):
    """Seconds to wait before a retry. Parse the server's hint if present, else backoff."""
    import re
    msg = str(exc)
    m = re.search(r"try again in ([\d.]+)(ms|s)", msg)
    hinted = 0.0
    if m:
        val = float(m.group(1))
        hinted = val / 1000.0 if m.group(2) == "ms" else val
    # token-per-minute limits need real spacing; grow with each attempt, cap at 30s
    return min(max(hinted, 2.0 * (attempt + 1)), 30.0)


def _approx_chars(messages):
    n = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    n += len(part["text"])
    return n


def _stream_completion(client, kwargs, on_progress, expected_tokens):
    """Run ONE streaming chat completion.

    Returns (full_text, completion_tokens|None).

    Streaming means the model hands us the reply token-by-token instead of all at once.
    That does NOT change cost — it's still a single call billed on the same input/output
    tokens — it only lets us watch the reply being written, which is what powers the live
    progress bar + ETA on the study-sheet step.

    As tokens arrive we call on_progress(got_tokens, expected_tokens) (throttled to a few
    times per second) so the caller can update progress. If the stream breaks part-way we
    raise, so _chat's retry loop discards this partial text and retries the whole call
    (we never save a half-written sheet).
    """
    # stream_options.include_usage gives us a final usage chunk for the token log. Older
    # SDKs may not accept that kwarg, so fall back to a plain stream if so.
    try:
        stream = client.chat.completions.create(
            **kwargs, stream=True, stream_options={"include_usage": True})
    except TypeError:
        stream = client.chat.completions.create(**kwargs, stream=True)

    parts = []
    got = 0                 # content deltas received ~= completion tokens so far
    comp_tokens = None
    last_emit = 0.0
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if choices:
            delta = getattr(choices[0].delta, "content", None) or ""
            if delta:
                parts.append(delta)
                got += 1
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            comp_tokens = getattr(usage, "completion_tokens", None)
        if on_progress is not None:
            now = time.time()
            if now - last_emit >= 0.2:      # ~5 updates/sec, cheap
                last_emit = now
                try:
                    on_progress(got, expected_tokens)
                except Exception:
                    pass
    if on_progress is not None:             # final tick
        try:
            on_progress(got, expected_tokens)
        except Exception:
            pass
    return "".join(parts), (comp_tokens if comp_tokens is not None else got)


def _chat(model, messages, want_json=False, max_tokens=4000,
          stream=False, on_progress=None, expected_tokens=None):
    """One chat call with timing, logging, and automatic retry on rate-limit/transient errors.
    On final failure raises AIError(reason, detail) after logging the full detail.

    stream=True receives the reply incrementally and (if given) calls on_progress(got, expected)
    as tokens arrive — used to drive a live progress bar + ETA on long generations (study sheet,
    mock exam). Streaming is same cost / same single call; it only makes progress visible.
    JSON replies (want_json=True) are NEVER streamed: the caller needs the whole object at once."""
    client = get_client()
    kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if want_json:
        kwargs["response_format"] = {"type": "json_object"}
        stream = False  # safety: a structured JSON reply must arrive whole, not in pieces

    in_chars = _approx_chars(messages)
    log.info("AI call -> model=%s | max_tokens=%d | input~%d chars (~%d tokens) | json=%s | stream=%s",
             model, max_tokens, in_chars, in_chars // 4, want_json, stream)

    last_detail = None
    for attempt in range(6):
        started = time.time()
        try:
            if stream:
                text, comp_tokens = _stream_completion(client, kwargs, on_progress, expected_tokens)
                elapsed = time.time() - started
                log.info("AI ok | model=%s | %.1fs (stream) | completion~%s tokens | reply=%d chars",
                         model, elapsed, comp_tokens, len(text))
                return text
            resp = client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            elapsed = time.time() - started
            usage = getattr(resp, "usage", None)
            if usage:
                log.info("AI ok | model=%s | %.1fs | prompt=%s completion=%s total=%s tokens | reply=%d chars",
                         model, elapsed, getattr(usage, "prompt_tokens", "?"),
                         getattr(usage, "completion_tokens", "?"),
                         getattr(usage, "total_tokens", "?"), len(text))
            else:
                log.info("AI ok | model=%s | %.1fs | tokens n/a | reply=%d chars", model, elapsed, len(text))
            return text
        except Exception as e:
            d = _err_detail(e)
            last_detail = d
            status = d.get("status")
            name = d.get("type")
            is_rate = name == "RateLimitError" or status == 429 or "rate_limit" in str(d.get("code", "")).lower()
            is_quota = "insufficient_quota" in str(d.get("code", "")).lower()
            is_transient = name in ("APITimeoutError", "APIConnectionError", "InternalServerError") or (status or 0) in (500, 502, 503, 529)
            # Log the FULL detail every time something goes wrong.
            log.warning("AI error | model=%s | attempt=%d/6 | status=%s | code=%s | err_type=%s | request_id=%s | msg=%s",
                        model, attempt + 1, status, d.get("code"), d.get("err_type"),
                        d.get("request_id"), (d.get("message") or "")[:500])
            # Out of credit will NOT recover by retrying — fail fast.
            if is_quota:
                log.error("AI giving up (insufficient_quota) | model=%s", model)
                raise AIError(_human_reason(d), d)
            if (is_rate or is_transient) and attempt < 5:
                wait = _retry_wait(e, attempt)
                log.warning("AI retrying in %.1fs | model=%s", wait, model)
                time.sleep(wait)
                continue
            log.error("AI giving up | model=%s | reason=%s", model, _human_reason(d))
            raise AIError(_human_reason(d), d)
    raise AIError(_human_reason(last_detail or {}), last_detail or {})


# ---------------------------------------------------------------------------
# 1) Read text out of an image (or a scanned PDF page) using vision.
# ---------------------------------------------------------------------------
def read_text_from_image(image_bytes: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text":
                    "Extract ALL readable text from this image exactly as written. "
                    "Keep math, formulas, and structure. If there is no text, reply with an empty string. "
                    "Do not add explanations."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    log.info("AI vision | reading text from image (%d bytes, %s)", len(image_bytes), mime)
    return _chat(config.MODEL_VISION, messages, max_tokens=4000).strip()


# ---------------------------------------------------------------------------
# 2) Generate a syllabus (chapter list) when the student has none.
#    Relies on the model's general knowledge -> marked as AI generated by the caller.
# ---------------------------------------------------------------------------
def generate_syllabus(subject_name: str, grade: str, exam_scope: str, level: str = "") -> list:
    lvl = f"Course level: {level}\n" if level else ""
    prompt = (
        f"You are building a chapter (unit) list for a high-school study app.\n"
        f"Subject: {subject_name}\nGrade level: {grade}\n{lvl}"
        f"Exam scope (as the student described it): {exam_scope}\n\n"
        f"Produce a realistic, standard chapter list for this subject and grade that fits the exam scope. "
        f"Match the depth to the course level (AP = college-level units, Honors = deeper, Standard = core). "
        f"Use clear, specific chapter names (e.g. 'Chemical Bonding', not just 'Unit 5'). "
        f'Reply ONLY as JSON: {{"chapters": ["name1", "name2", ...]}} in study order.'
    )
    text = _chat(config.MODEL_SYLLABUS, [{"role": "user", "content": prompt}], want_json=True)
    data = json.loads(text)
    chapters = [c.strip() for c in data.get("chapters", []) if c.strip()]
    log.info("AI syllabus | subject=%s | level=%s | %d chapters", subject_name, level, len(chapters))
    return chapters


def analyze_syllabus_bundle(files: list) -> dict:
    """ONE call to group MANY uploaded files into the few real school subjects.
    files: [{"name": str, "text": str}]. Returns {"subjects":[{"name","chapters":[..]}]}.
    Crucially: things like 'Poetry', 'The Odyssey', 'Grammar' are UNITS of one English
    course, NOT separate subjects — so we don't end up with 8 fake subjects."""
    blocks = []
    for i, f in enumerate(files[:60]):
        snippet = (f.get("text") or "")[:500].replace("\n", " ")
        blocks.append(f'FILE {i+1} ("{f.get("name","")}"): {snippet}')
    joined = "\n\n".join(blocks)
    prompt = (
        "Below are several uploaded documents (syllabi, outlines, or class notes).\n"
        "Group them into the ACTUAL school SUBJECTS a student takes. A student usually has "
        "only a handful of subjects (e.g. 'English 9', 'Algebra 1', 'Biology').\n"
        "IMPORTANT: topics like 'Poetry', 'The Odyssey', 'Grammar', 'Romeo and Juliet' are "
        "UNITS/CHAPTERS of ONE subject (English), NOT separate subjects. Do not create a "
        "separate subject per file or per topic. Merge aggressively into real courses.\n"
        "For each subject, give a clean subject name and its list of chapter/unit names in a "
        "sensible order, with duplicates removed.\n\n"
        f"DOCUMENTS:\n{joined}\n\n"
        'Reply ONLY as JSON: {"subjects":[{"name":"English 9","chapters":["Grammar","Poetry","The Odyssey"]}, ...]}'
    )
    text = _chat(config.MODEL_SYLLABUS, [{"role": "user", "content": prompt}], want_json=True)
    data = json.loads(text)
    subjects = []
    for s in data.get("subjects", []):
        name = (s.get("name") or "").strip()
        chapters = [c.strip() for c in s.get("chapters", []) if c and c.strip()]
        if name and chapters:
            subjects.append({"name": name, "chapters": chapters})
    log.info("AI bundle analyze | %d files -> %d subjects", len(files), len(subjects))
    return {"subjects": subjects}


def extract_syllabus_from_text(hint_name: str, raw_text: str) -> dict:
    """Read a real syllabus document and pull out the SUBJECT NAME and the chapter list.
    Returns {"subject": str, "chapters": [..]}. hint_name is an optional fallback."""
    prompt = (
        f"The text below is a class syllabus / course outline.\n"
        f"1) Identify the SUBJECT/COURSE name (e.g. 'Biology', 'Algebra 1', 'World History').\n"
        f"2) Extract ONLY the chapter or unit names, in the order they appear. Drop dates, page "
        f"numbers, teacher names, grading policies, and anything that is not a chapter/unit title.\n\n"
        f'Reply ONLY as JSON: {{"subject": "...", "chapters": ["name1", "name2", ...]}}\n\n'
        f"SYLLABUS TEXT:\n{raw_text[:8000]}"
    )
    text = _chat(config.MODEL_SYLLABUS, [{"role": "user", "content": prompt}], want_json=True)
    data = json.loads(text)
    subject = (data.get("subject") or hint_name or "").strip()
    chapters = [c.strip() for c in data.get("chapters", []) if c.strip()]
    log.info("AI syllabus-from-file | subject=%s | %d chapters", subject, len(chapters))
    return {"subject": subject, "chapters": chapters}


# ---------------------------------------------------------------------------
# 3) Match uploaded materials to chapters (one call for a whole subject).
#    Returns: list of {material_id, chapter_index or -1, multi:[indexes]}
# ---------------------------------------------------------------------------
def _match_batch(subject_name, chapters, materials):
    chapter_lines = "\n".join(f"{i}: {c}" for i, c in enumerate(chapters))
    mat_lines = "\n".join(
        f'- material_id {m["id"]} (file "{m["filename"]}"): {m["preview"][:600]}'
        for m in materials
    )
    prompt = (
        f"Subject: {subject_name}\n\n"
        f"CHAPTERS (index: name):\n{chapter_lines}\n\n"
        f"MATERIALS (a short preview of each):\n{mat_lines}\n\n"
        "For each material, decide which chapter index it belongs to.\n"
        "- If it clearly fits ONE chapter, give that index.\n"
        "- If it spans MORE THAN ONE chapter, list all indexes in 'multi'.\n"
        "- If it fits NONE of the chapters, use chapter -1.\n"
        'Reply ONLY as JSON: {"matches":[{"material_id":1,"chapter":0,"multi":[]}, ...]}'
    )
    text = _chat(config.MODEL_MATCH, [{"role": "user", "content": prompt}], want_json=True)
    return json.loads(text).get("matches", [])


def match_materials(subject_name, chapters: list, materials: list, batch_size: int = 10) -> dict:
    """
    chapters: list of chapter names (index = position).
    materials: list of {"id": int, "filename": str, "preview": str}

    Materials are matched in small BATCHES so a big upload doesn't hit the token /
    rate limit in one giant call. A batch that fails after retries is skipped (those
    files simply stay unsorted and can be re-sorted later) rather than failing the
    whole upload.
    """
    matches, failed_batches, last_err = [], 0, None
    total = len(materials)
    for start in range(0, total, batch_size):
        chunk = materials[start:start + batch_size]
        try:
            matches.extend(_match_batch(subject_name, chapters, chunk))
        except AIError as e:
            failed_batches += 1
            last_err = e
            log.warning("match batch %d-%d failed: %s", start, start + len(chunk), e.reason)
            # No credit / bad key won't fix itself across batches — stop now.
            if "insufficient_quota" in str(e.detail.get("code", "")).lower() or e.detail.get("status") in (401, 404):
                break
        except Exception as e:
            failed_batches += 1
            last_err = e
            log.exception("match batch %d-%d failed (unexpected)", start, start + len(chunk))
        if start + batch_size < total:
            time.sleep(0.4)  # be gentle on per-minute limits
    log.info("AI match | %d materials | %d matched | %d batch(es) failed",
             total, len(matches), failed_batches)
    if not matches and failed_batches:
        if isinstance(last_err, AIError):
            raise last_err
        raise AIError("Sorting failed. Try again shortly.", {"message": str(last_err)})
    return {"matches": matches}


# ---------------------------------------------------------------------------
# 4) Summarize ONE chapter into the 11-section study sheet.
#    has_material=True  -> use ONLY the given text (no hallucination).
#    has_material=False -> build from the model's own knowledge (AI generated).
# ---------------------------------------------------------------------------
ELEVEN_SECTIONS = """# (chapter name here)

## 1. Unit Overview
## 2. Key Concepts
## 3. Main Content Summary
## 4. How the Concepts Connect
## 5. Examples & Applications
## 6. Common Misconceptions
## 7. Must-Memorize Points
## 8. Exam Focus Points
## 9. Practice Problems
## 10. Answers & Explanations
## 11. One-Minute Review"""


def _compress_chunk(chunk_text, chapter_name, subject_name):
    """STAGE 1 (map) for one chunk: pull the study-worthy facts into compact notes."""
    prompt = (
        "Extract the key study facts from this chunk of class material into compact NOTES.\n"
        f"Subject: {subject_name} | Chapter: {chapter_name}\n"
        "Rules: keep definitions, formulas, dates, names, cause-effect, worked steps, and any "
        "exam-worthy detail. Drop fluff, repetition, figure captions, and page furniture. Use short "
        "bullet lines. Do NOT add facts that are not in the text. Keep it under "
        f"{config.COMPRESS_NOTES_PER_CHUNK} characters.\n\n"
        f"MATERIAL CHUNK:\n{chunk_text}"
    )
    notes = _chat(config.MODEL_COMPRESS, [{"role": "user", "content": prompt}],
                  max_tokens=700).strip()
    return notes[: config.COMPRESS_NOTES_PER_CHUNK + 400]


def compress_material(chapter_name, subject_name, material_text,
                      progress=None, should_cancel=None):
    """STAGE 1 (map): turn arbitrarily large chapter material into fixed-size 'key notes'.

    This is what keeps cost flat: no matter how many pages a student uploads for a chapter,
    the notes we hand to the sheet generator are capped at COMPRESS_MAX_NOTES_CHARS. Because we
    summarize EVERY chunk, the whole material is reflected — not just the first few pages.
    Returns (notes_text, n_chunks). Cheap model (MODEL_COMPRESS) does the bulk work.

    progress(done, total): optional callback fired as each chunk finishes, so the UI can
        show "Reading 3/7 sections" instead of looking frozen on a big chapter.
    should_cancel(): optional callable; if it returns True we stop ASAP and raise
        GenerationCancelled. Chunks already finished are kept by the caller's cache logic.
    """
    text = material_text or ""
    if len(text) <= config.COMPRESS_TRIGGER_CHARS:
        # Short enough to use directly; no compression needed.
        if progress:
            try: progress(1, 1)
            except Exception: pass
        return text, 0

    chunk = config.COMPRESS_CHUNK_CHARS
    chunks = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    log.info("compress: chapter=%s | %d chars -> %d chunks (model=%s, parallel=%d)",
             chapter_name, len(text), len(chunks), config.MODEL_COMPRESS, config.COMPRESS_PARALLEL)

    # Compress chunks in parallel (big speedup on large textbook chapters). Every chunk is
    # still summarized, so no content is dropped — only the wall-clock time goes down.
    parts = [""] * len(chunks)
    total = len(chunks)

    def _do(ci_ck):
        ci, ck = ci_ck
        try:
            return ci, _compress_chunk(ck, chapter_name, subject_name)
        except AIError:
            raise
        except Exception as e:
            log.warning("compress chunk %d failed: %s", ci + 1, e)
            return ci, ""

    def _tick(n):
        if progress:
            try: progress(n, total)
            except Exception: pass

    done_n = 0
    workers = max(1, min(config.COMPRESS_PARALLEL, len(chunks)))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_do, pair): pair[0] for pair in enumerate(chunks)}
            try:
                # as_completed -> report the moment each chunk lands, in any order
                for fut in as_completed(futures):
                    ci, result = fut.result()
                    parts[ci] = result
                    done_n += 1
                    _tick(done_n)
                    if should_cancel and should_cancel():
                        raise GenerationCancelled()
            except GenerationCancelled:
                for f in futures:          # best-effort: drop chunks not yet started
                    f.cancel()
                raise
    else:
        for pair in enumerate(chunks):
            if should_cancel and should_cancel():
                raise GenerationCancelled()
            ci, result = _do(pair)
            parts[ci] = result
            done_n += 1
            _tick(done_n)
    notes = "\n".join(p for p in parts if p)

    # If the combined notes are still too long (very large material), do one reduce pass.
    if len(notes) > config.COMPRESS_MAX_NOTES_CHARS:
        reduce_prompt = (
            "Condense these notes into a single tight set of study notes, keeping every distinct "
            "fact, formula, date, and definition but removing duplication. Keep under "
            f"{config.COMPRESS_MAX_NOTES_CHARS} characters.\n\n{notes}"
        )
        try:
            notes = _chat(config.MODEL_COMPRESS, [{"role": "user", "content": reduce_prompt}],
                          max_tokens=2000).strip()
        except Exception as e:
            log.warning("compress reduce-pass failed (%s); truncating notes", e)
            notes = notes[: config.COMPRESS_MAX_NOTES_CHARS]
    return notes[: config.COMPRESS_MAX_NOTES_CHARS], len(chunks)


# Rough target length (in tokens) of a full 11-section sheet. Used ONLY to turn live
# token count into a 0-100% progress fraction + ETA while streaming. If the real reply
# runs longer the fraction just clamps at 99% until it finishes — overshooting a bit is
# fine (the bar/ETA stay honest), and the cap below keeps it from ever showing "done early".
SHEET_EXPECTED_TOKENS = 5000


def summarize_chapter(chapter_name, subject_name, grade, material_text, has_material,
                      precompressed_notes=None, on_progress=None):
    if has_material:
        source_policy = (
            "SOURCE POLICY:\n"
            "- Use the student's uploaded MATERIAL below as the backbone of the guide.\n"
            "- Where the material is thin or missing details a section needs, FILL IN with accurate, "
            "standard high-school curriculum knowledge so that EVERY section is complete and genuinely "
            "useful to study from. Never leave a section sparse and never write 'Not covered' — expand it "
            "yourself instead.\n"
        )
        # STAGE 2 input: prefer the compressed key-notes (fixed size, reflects ALL material).
        # Fall back to a capped slice only if no compression was done (short material).
        if precompressed_notes:
            body = f"STUDENT MATERIAL (compressed key notes covering the whole chapter):\n{precompressed_notes}"
        else:
            body = f"STUDENT MATERIAL:\n{material_text[:config.COMPRESS_MAX_NOTES_CHARS]}"
    else:
        source_policy = (
            "SOURCE POLICY:\n"
            "- There is no student material for this chapter. Write the entire guide from your own accurate "
            "knowledge of the standard high-school curriculum for this subject and grade.\n"
        )
        body = "(no student material provided)"

    prompt = (
        "You are writing a complete, exam-ready STUDY GUIDE page for a high-school student — like a page "
        "from a high-quality commercial study workbook you'd buy at a bookstore. This is NOT a brief "
        "summary: it must be thorough, well-structured, and ready to study and memorize from.\n\n"
        f"Subject: {subject_name} | Grade: {grade} | Chapter: {chapter_name}\n\n"
        f"{source_policy}\n"
        "PROVENANCE TAGGING (required): right after each section heading line '## N. Title', append exactly "
        "one tag in curly braces:\n"
        "  {src:material} = this section's content comes only from the student's material\n"
        "  {src:ai} = this section comes only from your own knowledge (not in their material)\n"
        "  {src:mixed} = it combines both\n"
        "(If there is no student material, every section is {src:ai}.)\n\n"
        "REQUIRED DEPTH — these are MINIMUMS. A thin sheet is a failure. Write like a full chapter from a "
        "paid exam-prep workbook a student studies from for days — thorough enough to score top marks, not a recap.\n"
        "1. Unit Overview: 4-6 full sentences — what the unit covers, why it matters, and how it connects to the course.\n"
        "2. Key Concepts: AT LEAST 20 key terms, each a bullet with the **term in bold**, a clear 2-3 sentence "
        "definition, and a short concrete example where it helps.\n"
        "3. Main Content Summary: the heart of the sheet — organize under 6-8 **bold sub-headings**, each with a full "
        "teaching paragraph (or grouped bullets) that actually explains the idea, not just names it. Include tables and "
        "diagrams generously here.\n"
        "4. How the Concepts Connect: a real paragraph PLUS a Mermaid diagram showing how the ideas relate.\n"
        "5. Examples & Applications: AT LEAST 10 fully worked examples, each step by step with the reasoning explained.\n"
        "6. Common Misconceptions: AT LEAST 10 mistakes, each with the correction and why it's wrong.\n"
        "7. Must-Memorize Points: AT LEAST 15 exact facts/formulas/dates as a tight bulleted list.\n"
        "8. Exam Focus Points: AT LEAST 10 bullets on what exams emphasize and the exact question types to expect.\n"
        "9. Practice Problems: AT LEAST 10 numbered problems spanning easy to hard (this is a quick self-check; the "
        "full exam practice lives in a separate mock exam).\n"
        "10. Answers & Explanations: a FULL worked solution for EVERY practice problem (show the steps, not just "
        "the answer).\n"
        "11. One-Minute Review: 8-12 bullets that rapidly recap the whole unit.\n\n"
        "VISUALS — use diagrams and tables GENEROUSLY wherever they make an idea easier to see. Do not ration them; "
        "a good study sheet is highly visual. Aim for SEVERAL diagrams/tables across the sheet (typically 4-8):\n"
        "- Any process, cycle, sequence, or cause->effect -> a Mermaid `flowchart LR` diagram.\n"
        "- Any classification/hierarchy/structure -> a Mermaid `flowchart TD` (top-down tree).\n"
        "- Any comparison of two+ things, or a set of properties/values -> a Markdown table.\n"
        "- Keep Mermaid node labels short and ASCII-only (no parentheses, %, or special characters that break it). "
        "Example:\n"
        "  ```mermaid\n  flowchart LR\n    A[Light plus H2O plus CO2] --> B[Chloroplast] --> C[Glucose plus O2]\n  ```\n"
        "- Put visuals in the sections where they help most (especially 2, 3, 4, 5), not just one place.\n\n"
        "LENGTH: this is a long, complete chapter — when printed it should run many pages. Do NOT stop early or "
        "summarize to save space. Keep writing until EVERY section meets its minimum above.\n"
        "Write in English, in Markdown, following EXACTLY this 11-section template (keep the headings, replace "
        "the first line with the real chapter name, and add the {src:...} tag on each heading). Use bullet "
        "points, bold for key terms, and tables where useful.\n\n"
        f"TEMPLATE:\n{ELEVEN_SECTIONS}\n\n"
        f"{body}"
    )
    text = _chat(config.MODEL_SUMMARY, [{"role": "user", "content": prompt}],
                 max_tokens=16000, stream=True, on_progress=on_progress,
                 expected_tokens=SHEET_EXPECTED_TOKENS)
    log.info("AI study-guide | chapter=%s | has_material=%s | %d chars",
             chapter_name, has_material, len(text))
    return text


def generate_mock_exam(subject_name, grade, chapter_names, notes_blob):
    """Generate a full practice exam (40 questions) covering the whole subject scope.
    notes_blob = concatenated compressed notes / key content from the chapters in scope.
    Returns Markdown: exam questions first, then an Answer Key with worked solutions."""
    chs = ", ".join(chapter_names) if chapter_names else "the whole course"
    prompt = (
        "You are writing a realistic PRACTICE EXAM for a high-school student to take before the real test. "
        "It must feel like an actual exam paper and cover the WHOLE scope below, weighted across all chapters.\n\n"
        f"Subject: {subject_name} | Grade: {grade}\nChapters in scope: {chs}\n\n"
        "STRUCTURE — exactly these parts, 40 questions total:\n"
        "Part A — Multiple Choice (16 questions): each with options A-D, one correct.\n"
        "Part B — Short Answer (16 questions): one or two sentences / a value with work.\n"
        "Part C — Extended Response (8 questions): require explanation, multi-step work, or a worked problem.\n\n"
        "RULES:\n"
        "- Spread questions across ALL chapters in scope, not just the first ones. Mix difficulty (easy -> hard).\n"
        "- Number questions continuously 1-40. Keep questions clear and self-contained.\n"
        "- Base questions on the content notes provided; fill gaps with standard curriculum knowledge if needed.\n"
        "- After ALL questions, add a section titled exactly '## Answer Key' with the correct answer AND a brief "
        "worked explanation for EVERY question 1-40.\n"
        "- Use diagrams/tables only if a question needs one. Write in English, in Markdown.\n\n"
        f"CONTENT NOTES (the material in scope):\n{notes_blob[:9000]}"
    )
    text = _chat(config.MODEL_SUMMARY, [{"role": "user", "content": prompt}], max_tokens=16000)
    log.info("AI mock-exam | subject=%s | chapters=%d | %d chars",
             subject_name, len(chapter_names), len(text))
    return text


# ---------------------------------------------------------------------------
# 5) Build the day-by-day study plan (one call total).
# ---------------------------------------------------------------------------
def generate_plan(subjects_payload: list, today: str) -> list:
    """
    subjects_payload: list of
        {"name", "exam_date", "available_time", "chapters":[names...]}
    Returns list of {"day","subject","chapter","task"}.
    """
    _wlabel = {1: "low", 2: "medium", 3: "high"}
    blocks = []
    for s in subjects_payload:
        cl = s["chapters"]
        simp = _wlabel.get(int(s.get("importance", 2)), "medium")
        if cl and isinstance(cl[0], dict):
            # chapters carry an importance weight (1/2/3) -> tell the planner to
            # spend more days on high-importance chapters, fewer on low.
            chs = ", ".join(
                f'{c["name"]} (importance: {_wlabel.get(int(c.get("weight", 2)), "medium")})'
                for c in cl
            ) if cl else "(no chapters)"
        else:
            chs = ", ".join(cl) if cl else "(no chapters)"
        blocks.append(
            f'Subject: {s["name"]} (subject importance: {simp})\n  Exam date: {s["exam_date"]}\n'
            f'  Available time: {s["available_time"]}\n  Chapters in scope: {chs}'
        )
    joined = "\n\n".join(blocks)
    prompt = (
        f"Today is {today}. Build a STRATEGIC, phase-based day-by-day study plan for a high-school student "
        f"preparing for these exams. Do not just spread chapters evenly — follow a real revision strategy.\n\n"
        f"{joined}\n\n"
        "STRATEGY (work backward from each exam date and split the available days into phases):\n"
        "- Across subjects, give ones marked higher 'subject importance' more total study time than lower ones.\n"
        "- Phase 1 — First pass (learn): go through EVERY chapter in scope once to learn the concepts. Spend more "
        "days on harder/larger chapters AND on chapters marked 'importance: high'; spend fewer on 'importance: low'. "
        "Every chapter MUST appear at least once. This phase finishes early.\n"
        "- Phase 2 — Second pass (deepen + fix weak spots): re-study, focus on harder chapters, review the "
        "must-memorize points.\n"
        "- IMPORTANT: Schedule ALL chapter learning and deepening to FINISH at least 2 days before each exam date. "
        "Do NOT put any chapter study, practice-exam day, or final-review day in the last 2 days before an exam — "
        "the app adds a dedicated practice-exam day and a full final-review day automatically, plus the exam day "
        "itself. Your job is only Phase 1 + Phase 2, ending >= 2 days before the exam.\n\n"
        "Make each day's task specific and actionable, and start the task text with its phase label in brackets, e.g. "
        "'[Pass 1] Read and take notes on ...', '[Pass 2] Re-study and quiz yourself on ...'.\n"
        'Reply ONLY as JSON: {"plan":[{"day":"YYYY-MM-DD","subject":"...","chapter":"...","task":"..."}, ...]} '
        "sorted by day. Write the task text in English."
    )
    text = _chat(config.MODEL_PLAN, [{"role": "user", "content": prompt}], want_json=True, max_tokens=4000)
    data = json.loads(text)
    items = data.get("plan", [])
    log.info("AI plan | %d items", len(items))
    return items


# ---------------------------------------------------------------------------
# 6) Answer a follow-up question about a chapter (uses the study sheet as context).
# ---------------------------------------------------------------------------
def answer_question(chapter_name, study_sheet_md, history, question):
    msgs = [{
        "role": "system",
        "content": (
            f"You are a friendly tutor helping a high-school student study the chapter "
            f'"{chapter_name}". Use this study sheet as your main reference:\n\n{study_sheet_md}\n\n'
            "Answer clearly in English. If the sheet does not cover it, say so and give a brief correct answer."
        ),
    }]
    for h in history:
        msgs.append({"role": "assistant" if h["role"] == "ai" else "user", "content": h["content"]})
    msgs.append({"role": "user", "content": question})
    text = _chat(config.MODEL_QA, msgs, max_tokens=1500)
    log.info("AI qa | chapter=%s | q=%d chars | a=%d chars",
             chapter_name, len(question), len(text))
    return text


# ---------------------------------------------------------------------------
# 7) Quick quiz for ONE chapter (active recall, auto-graded in the UI).
# ---------------------------------------------------------------------------
def generate_quiz(chapter_name, subject_name, grade, source_text, n=8,
                  difficulty=1, weak_questions=None):
    """Return a list of {q, choices:[4], answer_index, explanation}.
    source_text = the chapter's compressed notes or sheet markdown.
    difficulty: 1=easy (recall), 2=medium (apply), 3=hard (multi-step) - adapts to the
    student's past scores. weak_questions: topics the student keeps missing, drilled harder."""
    diff_label = {1: "EASY", 2: "MEDIUM", 3: "HARD"}.get(int(difficulty or 1), "EASY")
    diff_rule = {
        1: "EASY level: direct recall of key facts and definitions.",
        2: "MEDIUM level: apply the concept to a simple example or scenario.",
        3: "HARD level: multi-step reasoning, combine ideas, or spot a subtle mistake.",
    }.get(int(difficulty or 1))
    weak_block = ""
    if weak_questions:
        joined = "\n".join(f"- {q}" for q in weak_questions[:8])
        weak_block = ("\nThe student keeps missing these topics, so weight the quiz toward them "
                      "(ask about the same ideas in a fresh way, do not copy verbatim):\n" + joined + "\n")
    prompt = (
        f"Write a {n}-question multiple-choice QUICK QUIZ for active recall on this chapter.\n"
        f"Subject: {subject_name} | Grade: {grade} | Chapter: {chapter_name}\n"
        f"Difficulty: {diff_label}. {diff_rule}\n"
        f"{weak_block}\n"
        "Rules: each question has exactly 4 options. Keep every question at the difficulty above. "
        "Base it on the content below; fill gaps with standard curriculum knowledge.\n"
        'Reply ONLY as JSON: {"questions":[{"q":"...","choices":["A","B","C","D"],'
        '"answer_index":0,"explanation":"why the answer is correct (one sentence)"}, ...]}\n\n'
        f"CONTENT:\n{(source_text or '')[:6000]}"
    )
    text = _chat(config.MODEL_SUMMARY, [{"role": "user", "content": prompt}], want_json=True, max_tokens=4000)
    data = json.loads(text)
    out = []
    for q in data.get("questions", []):
        choices = [str(c) for c in (q.get("choices") or [])][:4]
        if q.get("q") and len(choices) == 4:
            ai_idx = q.get("answer_index", 0)
            try:
                ai_idx = int(ai_idx)
            except (TypeError, ValueError):
                ai_idx = 0
            out.append({"q": str(q["q"]), "choices": choices,
                        "answer_index": max(0, min(3, ai_idx)),
                        "explanation": str(q.get("explanation", ""))})
    log.info("AI quiz | chapter=%s | %d questions", chapter_name, len(out))
    return out


# ---------------------------------------------------------------------------
# 8) Flashcards for ONE chapter (front/back) for spaced-recall review.
# ---------------------------------------------------------------------------
def generate_flashcards(chapter_name, subject_name, grade, source_text, n=12):
    """Return a list of {front, back}."""
    prompt = (
        f"Create {n} study FLASHCARDS for this chapter for active recall.\n"
        f"Subject: {subject_name} | Grade: {grade} | Chapter: {chapter_name}\n\n"
        "Each card: 'front' = a term/question/prompt, 'back' = a concise answer/definition. "
        "Cover the most important concepts, terms, formulas, and facts. Keep backs short.\n"
        'Reply ONLY as JSON: {"cards":[{"front":"...","back":"..."}, ...]}\n\n'
        f"CONTENT:\n{(source_text or '')[:6000]}"
    )
    text = _chat(config.MODEL_SUMMARY, [{"role": "user", "content": prompt}], want_json=True, max_tokens=4000)
    data = json.loads(text)
    out = [{"front": str(c.get("front", "")), "back": str(c.get("back", ""))}
           for c in data.get("cards", []) if c.get("front") and c.get("back")]
    log.info("AI flashcards | chapter=%s | %d cards", chapter_name, len(out))
    return out
