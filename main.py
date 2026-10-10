import io
import logging
import mimetypes
import os
import re
import secrets
import time as pytime
import uuid
from contextlib import asynccontextmanager
from copy import copy
from datetime import date, time, timedelta

import asyncpg
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.properties import PageSetupProperties
from pydantic import BaseModel

log = logging.getLogger("uvicorn.error")     # Render loglarida ko'rinadi
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]

# present = Bor, late = Kechikdi, excused = Sababli (kelmagan), unexcused = Sababsiz (kelmagan)
# Davomat foizi = (Bor + Kechikdi) / belgilangan darslar. "Jami yo'q" = Sababli + Sababsiz.
STATUSES = {"present", "late", "excused", "unexcused"}
LABELS = {"present": "Bor", "late": "Kechikdi", "excused": "Sababli", "unexcused": "Sababsiz"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # statement_cache_size=0 — Supabase pooler (pgbouncer) bilan ishlashi uchun
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
    )
    app.state.http = httpx.AsyncClient(timeout=60)
    await ensure_bucket(app.state.http)
    yield
    await app.state.http.aclose()
    await app.state.pool.close()


app = FastAPI(title="Guruh sayti", lifespan=lifespan)


def admin(x_admin_token: str = Header(default="")):
    if not secrets.compare_digest(x_admin_token.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(401, "Parol noto'g'ri")


def is_admin(x_admin_token: str = Header(default="")) -> bool:
    """Starosta ekanini tekshiradi, lekin xato bermaydi (ixtiyoriy maydonlarni ko'rsatish uchun)."""
    return bool(x_admin_token) and secrets.compare_digest(x_admin_token.encode(), ADMIN_TOKEN.encode())


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
    note: str = ""              # faqat "Sababli" uchun: sabab (masalan, kasal)


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
async def get_attendance(lesson_id: int, staff: bool = Depends(is_admin)):
    rows = await app.state.pool.fetch(
        "SELECT s.id AS student_id, s.full_name, a.status, a.note "
        "FROM students s LEFT JOIN attendance a "
        "ON a.student_id = s.id AND a.lesson_id = $1 "
        "WHERE s.active ORDER BY s.sort_order, s.full_name",
        lesson_id,
    )
    # sabab (masalan, kasallik) faqat starostaga ko'rinadi
    return [dict(r) if staff else {k: v for k, v in dict(r).items() if k != "note"} for r in rows]


@app.put("/api/lessons/{lesson_id}/attendance", dependencies=[Depends(admin)])
async def save_attendance(lesson_id: int, body: AttendanceIn):
    for r in body.records:
        if r.status not in STATUSES:
            raise HTTPException(400, f"Noto'g'ri status: {r.status}")
    rows = []
    for r in body.records:
        ex = r.status == "excused"      # sabab faqat "Sababli" bo'lsa saqlanadi
        rows.append((lesson_id, r.student_id, r.status, r.note.strip()[:300] if ex else ""))
    try:
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                await con.executemany(
                    "INSERT INTO attendance (lesson_id, student_id, status, note) "
                    "VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (lesson_id, student_id) DO UPDATE SET "
                    "status = EXCLUDED.status, note = EXCLUDED.note",
                    rows,
                )
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(400, "Talaba topilmadi")
    return {"saved": len(body.records)}


# ---------- Statistika ----------
async def month_stats(month: str):
    start, end = month_range(month)
    rows = await app.state.pool.fetch(
        """
        SELECT s.id, s.full_name,
               COUNT(*) FILTER (WHERE a.status = 'present')   AS present,
               COUNT(*) FILTER (WHERE a.status = 'late')      AS late,
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
        total = d["present"] + d["late"] + d["excused"] + d["unexcused"]
        d["percent"] = round((d["present"] + d["late"]) * 100 / total) if total else None
        out.append(d)
    return out


@app.get("/api/stats")
async def stats(month: str):
    return await month_stats(month)


@app.get("/api/stats/trend")
async def stats_trend(month: str):
    """Oy ichidagi har bir kun uchun jami davomat (grafik uchun)."""
    start, end = month_range(month)
    rows = await app.state.pool.fetch(
        """
        SELECT l.lesson_date AS d,
               COUNT(*) FILTER (WHERE a.status = 'present')   AS present,
               COUNT(*) FILTER (WHERE a.status = 'late')      AS late,
               COUNT(*) FILTER (WHERE a.status = 'excused')   AS excused,
               COUNT(*) FILTER (WHERE a.status = 'unexcused') AS unexcused
        FROM lessons l
        JOIN attendance a ON a.lesson_id = l.id
        JOIN students s ON s.id = a.student_id AND s.active
        WHERE l.lesson_date >= $1 AND l.lesson_date < $2
        GROUP BY l.lesson_date
        ORDER BY l.lesson_date
        """,
        start, end,
    )
    out = []
    for r in rows:
        total = r["present"] + r["late"] + r["excused"] + r["unexcused"]
        out.append({
            "date": r["d"].isoformat(),
            "present": r["present"], "late": r["late"],
            "excused": r["excused"], "unexcused": r["unexcused"],
            "total": total,
            "percent": round((r["present"] + r["late"]) * 100 / total) if total else 0,
        })
    return out


@app.get("/api/stats/day")
async def stats_day(lesson_date: date, staff: bool = Depends(is_admin)):
    """Bitta kun: har bir dars bo'yicha sonlar va kelmaganlar ro'yxati."""
    pool = app.state.pool
    lessons = await pool.fetch(
        "SELECT id, pair_no, subject FROM lessons WHERE lesson_date = $1 ORDER BY pair_no",
        lesson_date,
    )
    ids = [l["id"] for l in lessons]
    recs = await pool.fetch(
        "SELECT a.lesson_id, a.status, a.note, s.full_name "
        "FROM attendance a JOIN students s ON s.id = a.student_id AND s.active "
        "WHERE a.lesson_id = ANY($1::int[]) ORDER BY s.sort_order, s.full_name",
        ids,
    )
    out = []
    for l in lessons:
        mine = [r for r in recs if r["lesson_id"] == l["id"]]
        item = {"id": l["id"], "pair_no": l["pair_no"], "subject": l["subject"],
                "total": len(mine),
                "absentees": [
                    {"full_name": r["full_name"], "status": r["status"],
                     **({"note": r["note"]} if staff else {})}
                    for r in mine if r["status"] != "present"]}
        for k in STATUSES:
            item[k] = sum(1 for r in mine if r["status"] == k)
        out.append(item)
    return out


# ---------- Excel ----------
# Hisobot template_week.xlsx shablonidagi chop etiladigan haftalik jadval ko'rinishida chiqadi.
# Shablonni o'z faylingiz bilan almashtirsangiz bo'ladi (tuzilmasini saqlang: 4-qatordan sarlavha,
# 7-qatordan talabalar, C:V ustunlari Du-Ju x 4 juftlik, W = "Jami yo'q", X = "Izoh").
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template_week.xlsx")
SYMBOL = {"present": "+", "late": "K", "excused": "S", "unexcused": "X"}
DAY_COL0 = [3, 7, 11, 15, 19]   # Dushanba..Juma bloklarining birinchi ustuni
TEMPLATE_ROWS = 21              # shablondagi talabalar qatorlari (7..27)
THIN = Side(style="thin")


def note_text(entries: list) -> str:
    """[(sana, izoh), ...] -> '05.10 kasal; 07.10 oilaviy' (bir kunda bir xil izoh bir marta)."""
    by_day: dict = {}
    for d, n in entries:
        if n not in by_day.setdefault(d, []):
            by_day[d].append(n)
    return "; ".join(f"{d:%d.%m} {', '.join(ns)}" for d, ns in sorted(by_day.items()))


def fill_week(ws, monday: date, students: list, lesson_ids: dict, att: dict, notes: list | None = None):
    """Bitta haftalik varaqni to'ldiradi.
    lesson_ids: {(sana, juftlik): dars_id}, att: {(dars_id, talaba_id): status}
    notes: [(talaba_id, sana, izoh)] — "Sababli" izohlari; None bo'lsa (starosta emas) chiqarilmaydi"""
    days = [monday + timedelta(days=i) for i in range(5)]
    friday = days[-1]
    ws.title = f"{monday:%d.%m}-{friday:%d.%m}"
    ws["A2"] = (f"Hafta:  {monday:%d} . {monday:%m} . {monday:%Y}  dan  "
                f"{friday:%d} . {friday:%m} . {friday:%Y}  gacha")
    for i, d in enumerate(days):
        ws.cell(5, DAY_COL0[i]).value = f"sana:  {d:%d} . {d:%m}"

    week_notes: dict = {}
    for sid, d, text in notes or []:
        if days[0] <= d <= days[-1]:
            week_notes.setdefault(sid, []).append((d, text))

    n = len(students)
    last = 6 + max(n, 1)         # oxirgi talaba qatori
    legend = last + 1
    stop = max(28, legend)

    mid = {c: copy(ws.cell(7, c)._style) for c in range(1, 25)}
    end = {c: copy(ws.cell(27, c)._style) for c in range(1, 25)}
    blank = {c: copy(ws.cell(28, c)._style) for c in range(1, 25)}

    for r in range(7, stop + 1):
        style = mid if r < last else end if r == last else blank
        for c in range(1, 25):
            cell = ws.cell(r, c)
            cell.value = None
            cell._style = copy(style[c])
        ws.row_dimensions[r].height = 26.1 if r <= last else (20 if r == legend else 15)

    for idx, s in enumerate(students):
        r = 7 + idx
        ws.cell(r, 1).value = idx + 1
        ws.cell(r, 2).value = s["full_name"]
        missed = 0
        for di, d in enumerate(days):
            for pair in range(1, 5):
                lid = lesson_ids.get((d, pair))
                st = att.get((lid, s["id"])) if lid else None
                if st:
                    ws.cell(r, DAY_COL0[di] + pair - 1).value = SYMBOL[st]
                    if st in ("excused", "unexcused"):   # kelmaganlar; Kechikdi hisobga kirmaydi
                        missed += 1
        ws.cell(r, 23).value = missed or None
        text = note_text(week_notes.get(s["id"], []))
        if text:                 # "Izoh" ustuni
            cell = ws.cell(r, 24)
            cell.value = text
            cell.font = Font(name=cell.font.name or "Arial", size=8)
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left")
            ws.row_dimensions[r].height = max(26.1, 10.5 * -(-len(text) // 17) + 4)

    note = ws.cell(legend, 2)
    note.value = "Belgilar:   +  Bor     K  Kechikdi     S  Sababli     X  Sababsiz"
    note.font = Font(name="Arial", size=9, italic=True)
    note.alignment = Alignment(horizontal="left", vertical="center")
    ws.print_area = f"A1:X{legend}"


def add_summary(wb, rows: list, month: str, notes: list | None = None):
    ws = wb.create_sheet("Oy jami")
    head_fill = PatternFill("solid", fgColor="E7E6E6")
    box = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    ws["A1"] = f"OY JAMI — {month}"
    ws["A1"].font = Font(name="Arial", size=14, bold=True)
    ws.merge_cells("A1:I1" if notes is not None else "A1:H1")
    ws["A1"].alignment = Alignment(horizontal="center")
    heads = ["№", "Talaba F.I.Sh.", "Bor", "Kechikdi", "Sababli", "Sababsiz", "Jami", "Davomat %"]
    if notes is not None:
        heads.append("Sababli izohlari")
    by_student: dict = {}
    for sid, d, text in notes or []:
        by_student.setdefault(sid, []).append((d, text))
    for c, h in enumerate(heads, start=1):
        cell = ws.cell(3, c, h)
        cell.font = Font(name="Arial", size=11, bold=True)
        cell.fill, cell.border = head_fill, box
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, r in enumerate(rows, start=1):
        total = r["present"] + r["late"] + r["excused"] + r["unexcused"]
        vals = [i, r["full_name"], r["present"], r["late"], r["excused"], r["unexcused"],
                total, r["percent"] if r["percent"] is not None else ""]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(3 + i, c, v)
            cell.font = Font(name="Arial", size=11)
            cell.border = box
            cell.alignment = Alignment(horizontal="left" if c == 2 else "center", vertical="center")
        ws.row_dimensions[3 + i].height = 22
        if notes is not None:
            text = note_text(by_student.get(r["id"], []))
            cell = ws.cell(3 + i, 9, text)
            cell.font = Font(name="Arial", size=10)
            cell.border = box
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left")
            if text:
                ws.row_dimensions[3 + i].height = max(22, 13.5 * -(-len(text) // 46) + 4)
    for col, w in zip("ABCDEFGHI", [5, 42, 8, 8, 10, 10, 8, 12, 48]):
        ws.column_dimensions[col].width = w
    ws.row_dimensions[3].height = 24
    ws.freeze_panes = "C4"
    ws.page_setup.orientation = "portrait"
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0


def build_month_workbook(students, mondays, lesson_ids, att, stats_rows, month, notes=None):
    wb = load_workbook(TEMPLATE)
    base = wb.active
    if mondays:
        sheets = [base] + [wb.copy_worksheet(base) for _ in mondays[1:]]
        for ws, mon in zip(sheets, mondays):
            fill_week(ws, mon, students, lesson_ids, att, notes)
    else:
        wb.remove(base)
    start, end = month_range(month)
    add_summary(wb, stats_rows, month,
                None if notes is None else [e for e in notes if start <= e[1] < end])
    return wb


def build_week_workbook(students, monday, lesson_ids, att, notes=None):
    wb = load_workbook(TEMPLATE)
    fill_week(wb.active, monday, students, lesson_ids, att, notes)
    return wb


async def load_span(first: date, until: date):
    pool = app.state.pool
    lrows = await pool.fetch(
        "SELECT id, lesson_date, pair_no FROM lessons WHERE lesson_date >= $1 AND lesson_date < $2",
        first, until,
    )
    arows = await pool.fetch(
        "SELECT a.lesson_id, a.student_id, a.status FROM attendance a "
        "JOIN lessons l ON l.id = a.lesson_id WHERE l.lesson_date >= $1 AND l.lesson_date < $2",
        first, until,
    )
    lesson_ids = {(r["lesson_date"], r["pair_no"]): r["id"] for r in lrows}
    att = {(r["lesson_id"], r["student_id"]): r["status"] for r in arows}
    return lesson_ids, att


async def load_notes(first: date, until: date) -> list:
    """"Sababli" izohlari: [(talaba_id, sana, izoh)]."""
    rows = await app.state.pool.fetch(
        "SELECT a.student_id, l.lesson_date, a.note FROM attendance a "
        "JOIN lessons l ON l.id = a.lesson_id "
        "WHERE a.status = 'excused' AND a.note <> '' AND l.lesson_date >= $1 AND l.lesson_date < $2 "
        "ORDER BY l.lesson_date, l.pair_no",
        first, until,
    )
    return [(r["student_id"], r["lesson_date"], r["note"]) for r in rows]


async def active_students():
    rows = await app.state.pool.fetch(
        "SELECT id, full_name FROM students WHERE active ORDER BY sort_order, full_name"
    )
    return [dict(r) for r in rows]


def xlsx_response(wb, filename: str):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export")
async def export_month(month: str, staff: bool = Depends(is_admin)):
    """Oylik hisobot: har hafta uchun alohida varaq (shablon ko'rinishida) + 'Oy jami'."""
    start, end = month_range(month)
    days = await app.state.pool.fetch(
        "SELECT DISTINCT lesson_date FROM lessons WHERE lesson_date >= $1 AND lesson_date < $2",
        start, end,
    )
    mondays = sorted({
        r["lesson_date"] - timedelta(days=r["lesson_date"].weekday())
        for r in days if r["lesson_date"].weekday() < 5
    })
    lesson_ids, att = ({}, {})
    if mondays:
        lesson_ids, att = await load_span(mondays[0], mondays[-1] + timedelta(days=7))
    notes = None        # izohlar faqat starostaga (parol bilan yuklaganda) chiqadi
    if staff:
        first = min(start, mondays[0]) if mondays else start
        until = max(end, mondays[-1] + timedelta(days=7)) if mondays else end
        notes = await load_notes(first, until)
    wb = build_month_workbook(
        await active_students(), mondays, lesson_ids, att, await month_stats(month), month, notes
    )
    return xlsx_response(wb, f"davomat-{month}.xlsx")


@app.get("/api/export/week")
async def export_week(day: date, staff: bool = Depends(is_admin)):
    """Haftalik hisobot: berilgan sana qaysi haftaga tushsa, o'sha haftaning varag'i."""
    monday = day - timedelta(days=day.weekday())
    lesson_ids, att = await load_span(monday, monday + timedelta(days=7))
    notes = await load_notes(monday, monday + timedelta(days=7)) if staff else None
    wb = build_week_workbook(await active_students(), monday, lesson_ids, att, notes)
    return xlsx_response(wb, f"davomat-{monday:%Y-%m-%d}.xlsx")


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


# ---------- Hujjatlar ----------
GROUP_CODE = os.environ.get("GROUP_CODE", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = os.environ.get("STORAGE_BUCKET", "documents")
MAX_UPLOAD = 25 * 1024 * 1024   # 25 MB

_fails: dict[str, list[float]] = {}   # guruh kodini taxmin qilishdan himoya (IP bo'yicha)


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() or (request.client.host if request.client else "?")


def member(
    request: Request,
    x_group_code: str = Header(default=""),
    x_admin_token: str = Header(default=""),
):
    """Starosta (admin paroli) yoki guruh kodi bilan kirgan talaba."""
    if x_admin_token and secrets.compare_digest(x_admin_token.encode(), ADMIN_TOKEN.encode()):
        return "admin"
    ip, now = client_ip(request), pytime.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 10:
        raise HTTPException(429, "Juda ko'p urinish. 5 daqiqadan keyin qayta urinib ko'ring")
    if GROUP_CODE and x_group_code and secrets.compare_digest(x_group_code.encode(), GROUP_CODE.encode()):
        return "member"
    _fails[ip] = recent + [now]
    raise HTTPException(401, "Guruh kodi noto'g'ri")


def sb_headers(extra: dict | None = None) -> dict:
    h = {"apikey": SUPABASE_KEY}
    if not SUPABASE_KEY.startswith("sb_"):      # eski (JWT) kalit uchun
        h["Authorization"] = f"Bearer {SUPABASE_KEY}"
    if extra:
        h.update(extra)
    return h


def need_storage():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Fayl saqlash sozlanmagan (SUPABASE_URL va SUPABASE_SERVICE_KEY kerak)")


def sb_error(r: httpx.Response) -> str:
    """Supabase javobidan o'qiladigan xatolik matni (kalitni o'z ichiga olmaydi)."""
    try:
        j = r.json()
        msg = j.get("message") or j.get("error") or str(j)
    except Exception:
        msg = (r.text or "")[:200]
    return f"{r.status_code}: {msg}"


async def sb_call(method: str, path: str, **kw) -> httpx.Response:
    """Supabase Storage so'rovi. Yangi (sb_) kalit bilan avval faqat apikey yuboriladi;
    rad etilsa, Authorization: Bearer bilan ham bir marta sinab ko'riladi."""
    need_storage()
    url = f"{SUPABASE_URL}/storage/v1/{path}"
    extra = kw.pop("headers", {})
    variants = [sb_headers(extra)]
    if SUPABASE_KEY.startswith("sb_"):
        variants.append({**sb_headers(extra), "Authorization": f"Bearer {SUPABASE_KEY}"})
    r = None
    for h in variants:
        try:
            r = await app.state.http.request(method, url, headers=h, **kw)
        except httpx.HTTPError as e:
            log.error("Supabase bilan aloqa xatosi: %s", type(e).__name__)
            raise HTTPException(502, "Supabase bilan bog'lanib bo'lmadi. SUPABASE_URL to'g'riligini tekshiring "
                                     "(faqat https://xxxx.supabase.co ko'rinishida bo'lsin)")
        if r.status_code not in (401, 403) and not (r.status_code == 400 and "jw" in r.text.lower()):
            return r
    return r


async def ensure_bucket(http: httpx.AsyncClient | None = None):
    """Yopiq (private) bucket yo'q bo'lsa yaratadi; bor bo'lsa jim o'tadi."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    try:
        r = await sb_call("POST", "bucket", json={"id": BUCKET, "name": BUCKET, "public": False})
        if r.status_code < 300:
            log.info("Supabase bucket '%s' yaratildi", BUCKET)
        elif r.status_code == 409 or "already exists" in r.text.lower():
            log.info("Supabase bucket '%s' mavjud", BUCKET)
        else:
            log.warning("Supabase bucket '%s' yaratilmadi: %s", BUCKET, sb_error(r))
    except Exception as e:
        log.warning("Supabase bucket tekshirib bo'lmadi: %s", e)


async def storage_upload(path: str, data: bytes, mime: str):
    for attempt in (1, 2):
        r = await sb_call("POST", f"object/{BUCKET}/{path}", headers={"Content-Type": mime}, content=data)
        if r.status_code < 300:
            return
        if attempt == 1 and "bucket" in r.text.lower() and "not found" in r.text.lower():
            await ensure_bucket()       # bucket o'chirilgan yoki hali yaratilmagan bo'lsa, yaratib qayta urinamiz
            continue
        break
    log.error("Supabase faylni saqlamadi: %s", sb_error(r))
    raise HTTPException(502, f"Supabase javobi — {sb_error(r)}")


async def storage_sign(path: str, seconds: int = 300) -> str:
    r = await sb_call("POST", f"object/sign/{BUCKET}/{path}", json={"expiresIn": seconds})
    if r.status_code >= 300:
        log.error("Supabase havola yaratmadi: %s", sb_error(r))
        raise HTTPException(502, f"Fayl havolasini yaratib bo'lmadi — {sb_error(r)}")
    return f"{SUPABASE_URL}/storage/v1{r.json()['signedURL']}"


async def storage_delete(path: str):
    try:
        await sb_call("DELETE", f"object/{BUCKET}/{path}")
    except Exception:
        pass


async def read_upload(file: UploadFile):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Fayl 25 MB dan katta bo'lmasligi kerak")
    name = os.path.basename(file.filename or "fayl")[:200]
    ext = re.sub(r"[^a-z0-9]", "", os.path.splitext(name)[1].lower())[:8]
    path = uuid.uuid4().hex + (f".{ext}" if ext else "")      # saqlash nomi har doim xavfsiz (ASCII)
    mime = file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return data, name, mime, path


def clean_tags(items: list[str]) -> list[str]:
    out: list[str] = []
    for t in items:
        t = t.strip().lstrip("#")[:30]
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out[:10]


def check_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://\S+$", url):
        raise HTTPException(400, "Havola http:// yoki https:// bilan boshlanishi kerak")
    return url[:2000]


def to_category(raw: str):
    return int(raw) if raw.strip().isdigit() else None


@app.get("/api/access")
async def access(role: str = Depends(member)):
    return {"role": role}


# --- kategoriyalar ---
class CategoryIn(BaseModel):
    name: str


@app.get("/api/categories", dependencies=[Depends(member)])
async def list_categories():
    rows = await app.state.pool.fetch("SELECT id, name FROM categories ORDER BY sort_order, name")
    return [dict(r) for r in rows]


@app.post("/api/categories", dependencies=[Depends(admin)])
async def add_category(body: CategoryIn):
    name = body.name.strip()[:60]
    if not name:
        raise HTTPException(400, "Kategoriya nomini yozing")
    try:
        row = await app.state.pool.fetchrow(
            "INSERT INTO categories (name, sort_order) "
            "VALUES ($1, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories)) RETURNING id, name",
            name,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Bunday kategoriya allaqachon bor")
    return dict(row)


@app.delete("/api/categories/{cid}", dependencies=[Depends(admin)])
async def delete_category(cid: int):
    # hujjatlar o'chmaydi, faqat "kategoriyasiz" bo'lib qoladi (FK: ON DELETE SET NULL)
    await app.state.pool.execute("DELETE FROM categories WHERE id = $1", cid)
    return {"ok": True}


# --- hujjatlar ---
@app.get("/api/documents", dependencies=[Depends(member)])
async def list_documents():
    rows = await app.state.pool.fetch(
        "SELECT d.id, d.title, d.category_id, c.name AS category, d.tags, d.note, d.kind, "
        "CASE WHEN d.kind = 'link' THEN d.url END AS url, "
        "d.file_name, d.mime, d.size, d.pinned, d.created_at "
        "FROM documents d LEFT JOIN categories c ON c.id = d.category_id "
        "ORDER BY d.pinned DESC, d.created_at DESC"
    )
    return [dict(r) for r in rows]


@app.get("/api/documents/{doc_id}/open", dependencies=[Depends(member)])
async def open_document(doc_id: int):
    row = await app.state.pool.fetchrow("SELECT kind, url, storage_path FROM documents WHERE id = $1", doc_id)
    if not row:
        raise HTTPException(404, "Hujjat topilmadi")
    if row["kind"] == "link":
        return {"url": row["url"]}
    return {"url": await storage_sign(row["storage_path"])}


@app.post("/api/documents", dependencies=[Depends(admin)])
async def create_document(
    title: str = Form(...),
    category_id: str = Form(""),
    tags: str = Form(""),
    note: str = Form(""),
    link: str = Form(""),
    file: UploadFile | None = File(None),
):
    title = title.strip()[:200]
    if not title:
        raise HTTPException(400, "Hujjat nomini yozing")
    has_file = file is not None and bool(file.filename)
    link = link.strip()
    if has_file == bool(link):
        raise HTTPException(400, "Fayl yoki havoladan faqat bittasini kiriting")

    kind, url, path, fname, mime, size = "link", None, None, None, None, None
    if link:
        url = check_url(link)
    else:
        data, fname, mime, path = await read_upload(file)
        size, kind = len(data), "file"
        await storage_upload(path, data, mime)
    try:
        row = await app.state.pool.fetchrow(
            "INSERT INTO documents (title, category_id, tags, note, kind, url, storage_path, file_name, mime, size) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
            title, to_category(category_id), clean_tags(tags.split(",")), note.strip()[:1000],
            kind, url, path, fname, mime, size,
        )
    except asyncpg.ForeignKeyViolationError:
        if path:
            await storage_delete(path)
        raise HTTPException(400, "Kategoriya topilmadi")
    return {"id": row["id"]}


class DocUpdate(BaseModel):
    title: str
    category_id: int | None = None
    tags: list[str] = []
    note: str = ""
    pinned: bool = False
    url: str | None = None


@app.put("/api/documents/{doc_id}", dependencies=[Depends(admin)])
async def update_document(doc_id: int, body: DocUpdate):
    title = body.title.strip()[:200]
    if not title:
        raise HTTPException(400, "Hujjat nomini yozing")
    row = await app.state.pool.fetchrow("SELECT kind, url FROM documents WHERE id = $1", doc_id)
    if not row:
        raise HTTPException(404, "Hujjat topilmadi")
    url = check_url(body.url) if row["kind"] == "link" and body.url else row["url"]
    try:
        await app.state.pool.execute(
            "UPDATE documents SET title=$1, category_id=$2, tags=$3, note=$4, pinned=$5, url=$6 WHERE id=$7",
            title, body.category_id, clean_tags(body.tags), body.note.strip()[:1000], body.pinned, url, doc_id,
        )
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(400, "Kategoriya topilmadi")
    return {"ok": True}


@app.post("/api/documents/{doc_id}/file", dependencies=[Depends(admin)])
async def replace_file(doc_id: int, file: UploadFile = File(...)):
    row = await app.state.pool.fetchrow("SELECT kind, storage_path FROM documents WHERE id = $1", doc_id)
    if not row:
        raise HTTPException(404, "Hujjat topilmadi")
    if row["kind"] != "file":
        raise HTTPException(400, "Bu hujjat havola, faylni almashtirib bo'lmaydi")
    data, fname, mime, path = await read_upload(file)
    await storage_upload(path, data, mime)
    await app.state.pool.execute(
        "UPDATE documents SET storage_path=$1, file_name=$2, mime=$3, size=$4 WHERE id=$5",
        path, fname, mime, len(data), doc_id,
    )
    await storage_delete(row["storage_path"])
    return {"ok": True}


@app.delete("/api/documents/{doc_id}", dependencies=[Depends(admin)])
async def delete_document(doc_id: int):
    row = await app.state.pool.fetchrow("DELETE FROM documents WHERE id = $1 RETURNING storage_path", doc_id)
    if row and row["storage_path"]:
        await storage_delete(row["storage_path"])
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
