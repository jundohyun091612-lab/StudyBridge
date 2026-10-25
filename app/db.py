"""
db.py
- Sets up the local database (SQLite: a single file at data/studybridge.db).
- No install needed; it ships with Python.
- Creates all tables on first run and seeds the one demo user.

Tables (plain-language meaning):
  users        - the student profile (one demo user for now)
  subjects     - one row per subject the student is studying for (with exam date, scope, free time)
  chapters     - the "slots": the units inside a subject (from syllabus)
  materials    - each uploaded file, its extracted text, and which chapter it belongs to
  summaries    - the AI study sheet for one chapter (the 11-section content)
  plans        - one row each time a study plan is generated
  plan_items   - the day-by-day rows of a plan (editable by the student)
  qa_history   - follow-up questions and answers, tied to a chapter
"""
import sqlite3
from .config import DB_PATH
from .logger import get_logger

log = get_logger()


def connect():
    """Open a connection. Rows behave like dictionaries (row['name'])."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    dob         TEXT,
    grade       TEXT,
    school      TEXT,
    login_id    TEXT UNIQUE NOT NULL,
    password    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    exam_date       TEXT,            -- YYYY-MM-DD
    exam_scope      TEXT,            -- free text, e.g. "Unit 1 - Unit 8"
    available_time  TEXT,            -- free text, e.g. "weekdays 2h, weekends 5h"
    syllabus_source TEXT             -- 'user' | 'ai' | 'none'
);

CREATE TABLE IF NOT EXISTS chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id),
    name        TEXT NOT NULL,
    order_index INTEGER DEFAULT 0,
    in_scope    INTEGER DEFAULT 1    -- 1 if inside the exam scope
);

CREATE TABLE IF NOT EXISTS materials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    filename    TEXT NOT NULL,
    filetype    TEXT,
    raw_text    TEXT,                -- extracted text
    subject_id  INTEGER REFERENCES subjects(id),
    chapter_id  INTEGER REFERENCES chapters(id),
    confirmed   INTEGER DEFAULT 0,   -- 1 once the student confirms the match
    note        TEXT                 -- e.g. 'multi-chapter' or 'unmatched'
);

CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id      INTEGER NOT NULL REFERENCES chapters(id),
    content_md      TEXT,            -- raw markdown from the AI
    content_html    TEXT,            -- rendered HTML for the screen
    is_ai_generated INTEGER DEFAULT 0  -- 1 = built without student material ("AI-generated" stamp)
);

CREATE TABLE IF NOT EXISTS plans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    generated_at TEXT
);

CREATE TABLE IF NOT EXISTS plan_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL REFERENCES plans(id),
    day         TEXT,                -- YYYY-MM-DD
    subject     TEXT,
    chapter     TEXT,
    task        TEXT,
    done        INTEGER DEFAULT 0,
    order_index INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS qa_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id  INTEGER NOT NULL REFERENCES chapters(id),
    role        TEXT,                -- 'user' or 'ai'
    content     TEXT,
    ts          TEXT
);
"""

# The account created on a brand-new install (empty database). An existing database keeps
# its own account untouched; the login page pre-fills whichever account is already there.
SEED_USER = {
    "name": "Demo Student",
    "dob": "",
    "grade": "10",
    "school": "",
    "login_id": "demo@studybridge.local",
    "password": "demo",
}


def _migrate(conn):
    """Additive, idempotent migrations so older databases keep working."""
    def has_col(table, col):
        return col in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    # Link a plan row to the actual chapter it studies (was name-only before).
    if not has_col("plan_items", "chapter_id"):
        conn.execute("ALTER TABLE plan_items ADD COLUMN chapter_id INTEGER")
        log.info("migrate: plan_items.chapter_id added")
    # Cache bookkeeping on the study sheet: which material + code version produced it.
    if not has_col("summaries", "source_hash"):
        conn.execute("ALTER TABLE summaries ADD COLUMN source_hash TEXT")
        log.info("migrate: summaries.source_hash added")
    if not has_col("summaries", "prompt_version"):
        conn.execute("ALTER TABLE summaries ADD COLUMN prompt_version TEXT")
        log.info("migrate: summaries.prompt_version added")
    # Content hash of a material's extracted text, used to skip duplicate uploads.
    if not has_col("materials", "content_hash"):
        conn.execute("ALTER TABLE materials ADD COLUMN content_hash TEXT")
        log.info("migrate: materials.content_hash added")
    # Cache for STAGE-1 compressed notes, keyed by a hash of the chapter's material.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS note_cache ("
        " source_hash TEXT PRIMARY KEY,"
        " notes_md TEXT NOT NULL,"
        " n_chunks INTEGER DEFAULT 0,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    # Practice exams generated on demand (one row per generated exam).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mock_exams ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL,"
        " subject_id INTEGER NOT NULL,"
        " title TEXT,"
        " content_md TEXT,"
        " content_html TEXT,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    # ---- v2 additions ----
    # Difficulty tier of a subject (Standard / Honors / AP). Null on old rows.
    if not has_col("subjects", "level"):
        conn.execute("ALTER TABLE subjects ADD COLUMN level TEXT")
        log.info("migrate: subjects.level added")
    # 'dirty' = subject was edited / got new files since the last generate, so its
    # sheets+plan are out of date and the user should press Apply to rebuild.
    if not has_col("subjects", "dirty"):
        conn.execute("ALTER TABLE subjects ADD COLUMN dirty INTEGER DEFAULT 0")
        log.info("migrate: subjects.dirty added")
    # Mark a summary as 'read' so the study screen / cards can show progress.
    if not has_col("summaries", "studied"):
        conn.execute("ALTER TABLE summaries ADD COLUMN studied INTEGER DEFAULT 0")
        log.info("migrate: summaries.studied added")
    # Importance/weight of a chapter for the exam: 1=low, 2=medium (default), 3=high.
    # Drives how many study days a chapter gets in the plan and in reschedule.
    if not has_col("chapters", "weight"):
        conn.execute("ALTER TABLE chapters ADD COLUMN weight INTEGER DEFAULT 2")
        log.info("migrate: chapters.weight added")
    # Subject-level importance (1=low, 2=medium, 3=high). Used to split study time
    # ACROSS subjects (chapters.weight splits time within a subject).
    if not has_col("subjects", "importance"):
        conn.execute("ALTER TABLE subjects ADD COLUMN importance INTEGER DEFAULT 2")
        log.info("migrate: subjects.importance added")
    # Quick quizzes: auto-generated per chapter, stored so they can be retaken.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quizzes ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chapter_id INTEGER NOT NULL,"
        " questions_json TEXT NOT NULL,"      # [{q, choices[], answer_index, explanation}]
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    # One row per question the student got WRONG -> drives weak-spot review.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS weak_spots ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL,"
        " chapter_id INTEGER NOT NULL,"
        " question TEXT,"
        " note TEXT,"                          # correct answer / why
        " resolved INTEGER DEFAULT 0,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    # Flashcards auto-made from a chapter sheet (front/back), for active recall.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS flashcards ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chapter_id INTEGER NOT NULL,"
        " cards_json TEXT NOT NULL,"           # [{front, back}]
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    # 'Recent' / resume: last time the student opened a given chapter.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recent_views ("
        " user_id INTEGER NOT NULL,"
        " chapter_id INTEGER NOT NULL,"
        " viewed_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (user_id, chapter_id))"
    )
    # ---- v3 additions ----
    # SPACED REPETITION: one row per review item (a flashcard or a weak-spot question).
    # box = interval step (0->1d, 1->3d, 2->7d, 3->14d, 4->30d). next_review = when it's due.
    # Remembered -> box+1; forgot -> box back to 0. Pure scheduling, no AI.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS srs_items ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL,"
        " kind TEXT NOT NULL,"                 # 'weak' | 'flashcard'
        " ref_id INTEGER NOT NULL,"            # weak_spots.id, OR flashcard index in the chapter
        " chapter_id INTEGER NOT NULL,"
        " box INTEGER DEFAULT 0,"
        " next_review TEXT,"                   # YYYY-MM-DD
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS srs_unique "
        "ON srs_items (user_id, kind, ref_id, chapter_id)"
    )
    # STREAK: one row per calendar day the student did real study work.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS study_days ("
        " user_id INTEGER NOT NULL,"
        " day TEXT NOT NULL,"                  # YYYY-MM-DD
        " PRIMARY KEY (user_id, day))"
    )
    # ADAPTIVE QUIZ: history of quiz scores per chapter, used to move difficulty up/down.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quiz_results ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL,"
        " chapter_id INTEGER NOT NULL,"
        " difficulty INTEGER,"
        " correct INTEGER,"
        " total INTEGER,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    # Current quiz difficulty per chapter: 1=easy, 2=medium, 3=hard. Starts easy.
    if not has_col("chapters", "quiz_level"):
        conn.execute("ALTER TABLE chapters ADD COLUMN quiz_level INTEGER DEFAULT 1")
        log.info("migrate: chapters.quiz_level added")
    conn.commit()


def default_login():
    """The local account the login page pre-fills: the first account in this database."""
    conn = connect()
    try:
        row = conn.execute("SELECT login_id, password FROM users ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return {"login_id": SEED_USER["login_id"], "password": SEED_USER["password"]}
    return {"login_id": row["login_id"], "password": row["password"]}


def init_db():
    """Create tables if missing and create the demo user on a brand-new database."""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        has_user = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if has_user is None:
            conn.execute(
                "INSERT INTO users (name, dob, grade, school, login_id, password) "
                "VALUES (?,?,?,?,?,?)",
                (
                    SEED_USER["name"], SEED_USER["dob"], SEED_USER["grade"],
                    SEED_USER["school"], SEED_USER["login_id"], SEED_USER["password"],
                ),
            )
            log.info("Seeded demo user: %s", SEED_USER["login_id"])
        conn.commit()
        log.info("Database ready at %s", DB_PATH)
    except Exception:
        log.exception("init_db failed")
        raise
    finally:
        conn.close()
