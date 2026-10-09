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
    status      TEXT NOT NULL CHECK (status IN ('present', 'absent', 'excused', 'unexcused')),
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
