import io
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, time

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]

# present = Bor, absent = Yo'q (sabab hali noma'lum), excused = Sababli, unexcused = Sababsiz
STATUSES = {"present", "absent", "excused", "unexcused"}
LABELS = {"present": "Bor", "absent": "Yo'q", "excused": "Sababli", "unexcused": "Sababsiz"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # statement_cache_size=0 — Supabase pooler (pgbouncer) bilan ishlashi uchun
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
    )
    yield
    await app.state.pool.close()


app = FastAPI(title="Guruh sayti", lifespan=lifespan)


def admin(x_admin_token: str = Header(default="")):
    if not secrets.compare_digest(x_admin_token.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(401, "Parol noto'g'ri")


def month_range(month: str) -> tuple[date, date]:
    try:
        y, m = map(int, month.split("-"))
        start = date(y, m, 1)
        end = date(y + (m == 12), m % 12 + 1, 1)
    except ValueError:
        raise HTTPException(400, "Oy formati YYYY-MM bo'lishi kerak")
    return start, end


class StudentsIn(BaseModel):
    names: list[str]


class LessonIn(BaseModel):
    lesson_date: date
    subject: str
    pair_no: int = 1


class Record(BaseModel):
    student_id: int
    status: str


class AttendanceIn(BaseModel):
    records: list[Record]


# ---------- Talabalar ----------
@app.get("/api/students")
async def list_students():
    rows = await app.state.pool.fetch(
        "SELECT id, full_name FROM students WHERE active ORDER BY sort_order, full_name"
    )
    return [dict(r) for r in rows]


@app.post("/api/students", dependencies=[Depends(admin)])
async def add_students(body: StudentsIn):
    names = [n.strip() for n in body.names if n.strip()]
    async with app.state.pool.acquire() as con:
        start = await con.fetchval("SELECT COALESCE(MAX(sort_order), 0) FROM students")
        await con.executemany(
            "INSERT INTO students (full_name, sort_order) VALUES ($1, $2)",
            [(n, start + i + 1) for i, n in enumerate(names)],
        )
    return {"added": len(names)}


class NameIn(BaseModel):
    full_name: str


@app.put("/api/students/{student_id}", dependencies=[Depends(admin)])
async def rename_student(student_id: int, body: NameIn):
    name = body.full_name.strip()
    if not name:
        raise HTTPException(400, "Ism bo'sh bo'lmasligi kerak")
    res = await app.state.pool.execute(
        "UPDATE students SET full_name = $1 WHERE id = $2", name, student_id
    )
    if res == "UPDATE 0":
        raise HTTPException(404, "Talaba topilmadi")
    return {"ok": True}


@app.delete("/api/students/{student_id}", dependencies=[Depends(admin)])
async def remove_student(student_id: int):
    # Davomat tarixi saqlanib qolishi uchun o'chirmaymiz, faqat ro'yxatdan yashiramiz
    await app.state.pool.execute("UPDATE students SET active = FALSE WHERE id = $1", student_id)
    return {"ok": True}


# ---------- Darslar ----------
@app.get("/api/lessons")
async def list_lessons(lesson_date: date):
    rows = await app.state.pool.fetch(
        "SELECT id, lesson_date, pair_no, subject FROM lessons "
        "WHERE lesson_date = $1 ORDER BY pair_no",
        lesson_date,
    )
    return [dict(r) for r in rows]


@app.post("/api/lessons", dependencies=[Depends(admin)])
async def create_lesson(body: LessonIn):
    row = await app.state.pool.fetchrow(
        "INSERT INTO lessons (lesson_date, pair_no, subject) VALUES ($1, $2, $3) "
        "ON CONFLICT (lesson_date, pair_no) DO UPDATE SET subject = EXCLUDED.subject "
        "RETURNING id, lesson_date, pair_no, subject",
        body.lesson_date, body.pair_no, body.subject.strip(),
    )
    return dict(row)


class LessonEdit(BaseModel):
    subject: str
    pair_no: int


@app.put("/api/lessons/{lesson_id}", dependencies=[Depends(admin)])
async def edit_lesson(lesson_id: int, body: LessonEdit):
    if not body.subject.strip():
        raise HTTPException(400, "Fan nomini yozing")
    try:
        res = await app.state.pool.execute(
            "UPDATE lessons SET subject = $1, pair_no = $2 WHERE id = $3",
            body.subject.strip(), body.pair_no, lesson_id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Bu juftlikda boshqa dars bor")
    if res == "UPDATE 0":
        raise HTTPException(404, "Dars topilmadi")
    return {"ok": True}


@app.delete("/api/lessons/{lesson_id}", dependencies=[Depends(admin)])
async def delete_lesson(lesson_id: int):
    # attendance yozuvlari ON DELETE CASCADE bilan birga o'chadi
    await app.state.pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)
    return {"ok": True}


# ---------- Davomat ----------
@app.get("/api/lessons/{lesson_id}/attendance")
async def get_attendance(lesson_id: int):
    rows = await app.state.pool.fetch(
        "SELECT s.id AS student_id, s.full_name, a.status "
        "FROM students s LEFT JOIN attendance a "
        "ON a.student_id = s.id AND a.lesson_id = $1 "
        "WHERE s.active ORDER BY s.sort_order, s.full_name",
        lesson_id,
    )
    return [dict(r) for r in rows]


@app.put("/api/lessons/{lesson_id}/attendance", dependencies=[Depends(admin)])
async def save_attendance(lesson_id: int, body: AttendanceIn):
    for r in body.records:
        if r.status not in STATUSES:
            raise HTTPException(400, f"Noto'g'ri status: {r.status}")
    async with app.state.pool.acquire() as con:
        async with con.transaction():
            await con.executemany(
                "INSERT INTO attendance (lesson_id, student_id, status) VALUES ($1, $2, $3) "
                "ON CONFLICT (lesson_id, student_id) DO UPDATE SET status = EXCLUDED.status",
                [(lesson_id, r.student_id, r.status) for r in body.records],
            )
    return {"saved": len(body.records)}


# ---------- Statistika ----------
async def month_stats(month: str):
    start, end = month_range(month)
    rows = await app.state.pool.fetch(
        """
        SELECT s.id, s.full_name,
               COUNT(*) FILTER (WHERE a.status = 'present')   AS present,
               COUNT(*) FILTER (WHERE a.status = 'absent')    AS absent,
               COUNT(*) FILTER (WHERE a.status = 'excused')   AS excused,
               COUNT(*) FILTER (WHERE a.status = 'unexcused') AS unexcused
        FROM students s
        LEFT JOIN (
            SELECT a.* FROM attendance a
            JOIN lessons l ON l.id = a.lesson_id
            WHERE l.lesson_date >= $1 AND l.lesson_date < $2
        ) a ON a.student_id = s.id
        WHERE s.active
        GROUP BY s.id, s.full_name, s.sort_order
        ORDER BY s.sort_order, s.full_name
        """,
        start, end,
    )
    out = []
    for r in rows:
        d = dict(r)
        total = d["present"] + d["absent"] + d["excused"] + d["unexcused"]
        d["percent"] = round(d["present"] * 100 / total) if total else None
        out.append(d)
    return out


@app.get("/api/stats")
async def stats(month: str):
    return await month_stats(month)


# ---------- Excel ----------
@app.get("/api/export")
async def export_xlsx(month: str):
    start, end = month_range(month)
    pool = app.state.pool
    lessons = await pool.fetch(
        "SELECT id, lesson_date, pair_no, subject FROM lessons "
        "WHERE lesson_date >= $1 AND lesson_date < $2 ORDER BY lesson_date, pair_no",
        start, end,
    )
    students = await pool.fetch(
        "SELECT id, full_name FROM students WHERE active ORDER BY sort_order, full_name"
    )
    att = await pool.fetch(
        "SELECT a.lesson_id, a.student_id, a.status FROM attendance a "
        "JOIN lessons l ON l.id = a.lesson_id "
        "WHERE l.lesson_date >= $1 AND l.lesson_date < $2",
        start, end,
    )
    cell = {(r["lesson_id"], r["student_id"]): r["status"] for r in att}

    wb = Workbook()
    ws = wb.active
    ws.title = "Davomat"
    head_fill = PatternFill("solid", fgColor="14212B")
    head_font = Font(bold=True, color="FFFFFF")
    fills = {
        "absent": "F6D5D2", "excused": "F6E7C3", "unexcused": "E6D2EC",
    }

    ws.append(
        ["Talaba"]
        + [f"{l['lesson_date']:%d.%m} ({l['pair_no']}) {l['subject']}" for l in lessons]
        + ["Bor", "Yo'q", "Sababli", "Sababsiz", "%"]
    )
    for c in ws[1]:
        c.fill, c.font = head_fill, head_font
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[1].height = 60

    for s in students:
        counts = {k: 0 for k in STATUSES}
        row = [s["full_name"]]
        for l in lessons:
            st = cell.get((l["id"], s["id"]))
            if st:
                counts[st] += 1
            row.append(LABELS.get(st, ""))
        total = sum(counts.values())
        pct = round(counts["present"] * 100 / total) if total else ""
        row += [counts["present"], counts["absent"], counts["excused"], counts["unexcused"], pct]
        ws.append(row)
        r = ws.max_row
        for i, l in enumerate(lessons, start=2):
            st = cell.get((l["id"], s["id"]))
            if st in fills:
                ws.cell(row=r, column=i).fill = PatternFill("solid", fgColor=fills[st])
            ws.cell(row=r, column=i).alignment = Alignment(horizontal="center")

    ws.column_dimensions["A"].width = 30
    for i in range(2, len(lessons) + 7):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 13
    ws.freeze_panes = "B2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="davomat-{month}.xlsx"'},
    )


# ---------- Dars jadvali ----------
class ScheduleIn(BaseModel):
    weekday: int            # 1 = Dushanba ... 7 = Yakshanba
    pair_no: int = 1
    start_time: time | None = None
    end_time: time | None = None
    subject: str
    room: str = ""
    teacher: str = ""


def check_schedule(body: ScheduleIn):
    if not 1 <= body.weekday <= 7:
        raise HTTPException(400, "Hafta kuni 1 dan 7 gacha bo'lishi kerak")
    if not body.subject.strip():
        raise HTTPException(400, "Fan nomini yozing")


@app.get("/api/schedule")
async def get_schedule():
    rows = await app.state.pool.fetch(
        "SELECT id, weekday, pair_no, "
        "to_char(start_time, 'HH24:MI') AS start_time, "
        "to_char(end_time, 'HH24:MI') AS end_time, "
        "subject, room, teacher "
        "FROM schedule ORDER BY weekday, pair_no, start_time"
    )
    return [dict(r) for r in rows]


@app.post("/api/schedule", dependencies=[Depends(admin)])
async def add_schedule(body: ScheduleIn):
    check_schedule(body)
    row = await app.state.pool.fetchrow(
        "INSERT INTO schedule (weekday, pair_no, start_time, end_time, subject, room, teacher) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        body.weekday, body.pair_no, body.start_time, body.end_time,
        body.subject.strip(), body.room.strip(), body.teacher.strip(),
    )
    return {"id": row["id"]}


@app.put("/api/schedule/{item_id}", dependencies=[Depends(admin)])
async def update_schedule(item_id: int, body: ScheduleIn):
    check_schedule(body)
    res = await app.state.pool.execute(
        "UPDATE schedule SET weekday=$1, pair_no=$2, start_time=$3, end_time=$4, "
        "subject=$5, room=$6, teacher=$7 WHERE id=$8",
        body.weekday, body.pair_no, body.start_time, body.end_time,
        body.subject.strip(), body.room.strip(), body.teacher.strip(), item_id,
    )
    if res == "UPDATE 0":
        raise HTTPException(404, "Topilmadi")
    return {"ok": True}


@app.delete("/api/schedule/{item_id}", dependencies=[Depends(admin)])
async def delete_schedule(item_id: int):
    await app.state.pool.execute("DELETE FROM schedule WHERE id=$1", item_id)
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
