CREATE TABLE IF NOT EXISTS students (
    id          SERIAL PRIMARY KEY,
    full_name   TEXT NOT NULL,
    sort_order  INT  NOT NULL DEFAULT 0,
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS lessons (
    id           SERIAL PRIMARY KEY,
    lesson_date  DATE NOT NULL,
    pair_no      INT  NOT NULL DEFAULT 1,
    subject      TEXT NOT NULL,
    UNIQUE (lesson_date, pair_no)
);

CREATE TABLE IF NOT EXISTS attendance (
    lesson_id   INT NOT NULL REFERENCES lessons(id)  ON DELETE CASCADE,
    student_id  INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    status      TEXT NOT NULL CHECK (status IN ('present', 'late', 'excused', 'unexcused')),
    PRIMARY KEY (lesson_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_lessons_date ON lessons (lesson_date);

-- Dars jadvali (haftalik, har hafta takrorlanadi)
CREATE TABLE IF NOT EXISTS schedule (
    id          SERIAL PRIMARY KEY,
    weekday     INT  NOT NULL CHECK (weekday BETWEEN 1 AND 7),  -- 1 = Dushanba
    pair_no     INT  NOT NULL DEFAULT 1,
    start_time  TIME,
    end_time    TIME,
    subject     TEXT NOT NULL,
    room        TEXT NOT NULL DEFAULT '',
    teacher     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_schedule_day ON schedule (weekday, pair_no);

-- Hujjatlar moduli
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INT  NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    id            SERIAL PRIMARY KEY,
    title         TEXT   NOT NULL,
    category_id   INT    REFERENCES categories(id) ON DELETE SET NULL,
    tags          TEXT[] NOT NULL DEFAULT '{}',
    note          TEXT   NOT NULL DEFAULT '',
    kind          TEXT   NOT NULL CHECK (kind IN ('file', 'link')),
    url           TEXT,
    storage_path  TEXT,
    file_name     TEXT,
    mime          TEXT,
    size          BIGINT,
    pinned        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_order ON documents (pinned DESC, created_at DESC);

-- Jadvallarga tashqaridan kirishni yopish (server postgres roli bilan ulanadi, unga ta'sir qilmaydi)
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents  ENABLE ROW LEVEL SECURITY;

-- Sababli kelmaganlik uchun sabab (faqat starosta ko'radi)
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';

-- Talabaning shaxsiy profili uchun kod
ALTER TABLE students ADD COLUMN IF NOT EXISTS access_code TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_students_access_code ON students (access_code) WHERE access_code IS NOT NULL;

-- Admin va muhim o'zgarishlar uchun audit jurnali
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip         TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL DEFAULT '',
    entity_id  INT,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log (created_at DESC);
