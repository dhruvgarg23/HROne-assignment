"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (real env wins over .env)
"""
import calendar
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False so real env wins

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

PRESENT_SET = ("PRESENT", "WFH", "ON_DUTY")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_millis(ms: int) -> int:
    return (ms // 1000) * 1000


def dt_from_millis(ms: int) -> datetime:
    ms = truncate_millis(int(ms))
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def millis_from_dt(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    dt = _ensure_utc(dt)
    return int(dt.timestamp() * 1000)


def round_half_up(value: float | int | Decimal, decimals: int) -> float:
    q = Decimal(1).scaleb(-decimals)  # 0.01 / 0.0001
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _shift_minutes(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _shift_minutes(shift_end) <= _shift_minutes(shift_start)


def attendance_date_for(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    punch_utc = _ensure_utc(punch_utc)
    punch_ist = punch_utc.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        # strictly earlier than shift_end -> previous day
        if (punch_ist.hour, punch_ist.minute, punch_ist.second, punch_ist.microsecond) < (eh, em, 0, 0):
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    h, m = map(int, shift_start.split(":"))
    d = datetime.fromisoformat(date_str)
    return d.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = map(int, shift_end.split(":"))
    d = datetime.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    return d.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    punch_in_utc = _ensure_utc(punch_in_utc)
    start = shift_start_dt(date_str, shift_start)
    diff = (punch_in_utc - start).total_seconds()
    if diff <= 600:  # strictly more than 10:00
        return 0
    return int(diff // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    punch_in_utc = _ensure_utc(punch_in_utc)
    punch_out_utc = _ensure_utc(punch_out_utc)
    secs = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(Decimal(str(secs)) / Decimal("3600"), 2)


def compute_overtime(punch_out_utc: datetime, shift_end: datetime) -> int:
    punch_out_utc = _ensure_utc(punch_out_utc)
    shift_end = _ensure_utc(shift_end)
    mins = int((punch_out_utc - shift_end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def working_days_in_month(month: str, joined_on: str) -> int:
    start_s, end_s = month_bounds(month)
    start = max(start_s, joined_on)
    if start > end_s:
        return 0
    d0 = datetime.fromisoformat(start).date()
    d1 = datetime.fromisoformat(end_s).date()
    n = 0
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def parse_date_or_422(s: str) -> str:
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        raise HTTPException(422, f"invalid date: {s}")
    return s


def parse_month_or_422(s: str) -> str:
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", s or ""):
        raise HTTPException(422, f"invalid month: {s}")
    return s


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@app.on_event("startup")
def _startup() -> None:
    try:
        ensure_indexes()
    except Exception:
        pass


try:
    ensure_indexes()
except Exception:
    pass


def serialize_employee(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    ca = d.get("created_at")
    if isinstance(ca, datetime):
        d["created_at"] = millis_from_dt(ca)
    return d


def _conv_dt(v: Any) -> Any:
    if isinstance(v, datetime):
        return millis_from_dt(v)
    return v


def serialize_attendance(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    d["punch_in"] = _conv_dt(d.get("punch_in"))
    d["punch_out"] = _conv_dt(d.get("punch_out"))
    if "work_hours" not in d:
        d["work_hours"] = None
    if "late_minutes" not in d or d["late_minutes"] is None:
        d["late_minutes"] = 0
    if "overtime_minutes" not in d or d["overtime_minutes"] is None:
        d["overtime_minutes"] = 0
    if "half_day" not in d or d["half_day"] is None:
        d["half_day"] = False
    if "history" not in d or d["history"] is None:
        d["history"] = []
        return d
    out_hist = []
    for h in d["history"]:
        h = dict(h)
        h["at"] = _conv_dt(h.get("at"))
        ch = dict(h.get("changes", {}))
        for k, v in list(ch.items()):
            if isinstance(v, dict):
                nv = dict(v)
                if k in ("punch_in", "punch_out"):
                    nv["from"] = _conv_dt(nv.get("from"))
                    nv["to"] = _conv_dt(nv.get("to"))
                ch[k] = nv
        h["changes"] = ch
        out_hist.append(h)
    d["history"] = out_hist
    return d


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except Exception:
            raise ValueError("invalid joined_on")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "mongo unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    now_ms = truncate_millis(int(datetime.now(timezone.utc).timestamp() * 1000))
    doc = body.model_dump()
    doc["created_at"] = dt_from_millis(now_ms)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_employee(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in / list
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    ms = truncate_millis(int(ms))
    ts = dt_from_millis(ms)
    date_str = attendance_date_for(ts, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    late = compute_late_minutes(ts, emp.get("shift_start", "09:30"), date_str)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        parse_date_or_422(date_from)
    if date_to is not None:
        parse_date_or_422(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from > date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def attendance_list_query(emp_code=None, date_from=None, date_to=None, status=None):
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Attendance: punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    ms = truncate_millis(int(ms))
    out_dt = dt_from_millis(ms)
    cur = (
        db.attendance_logs.find(
            {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt, "$ne": None}}
        )
        .sort("punch_in", DESCENDING)
        .limit(1)
    )
    docs = list(cur)
    if not docs:
        raise HTTPException(404, "no punch-in found")
    rec = docs[0]
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_dt = _ensure_utc(rec["punch_in"])
    if not (out_dt > punch_in_dt):
        raise HTTPException(422, "punched_at must be after punch_in")
    if (out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24h after punch_in")
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    send = shift_end_dt(rec["date"], shift_start, shift_end)
    work = compute_work_hours(punch_in_dt, out_dt)
    ot = compute_overtime(out_dt, send)
    half = bool(work < 4.50)
    res = db.attendance_logs.update_one(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None},
        {"$set": {"punch_out": out_dt, "work_hours": work, "overtime_minutes": ot, "half_day": half}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    updated = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": rec["date"]})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Attendance: regularize (PATCH)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date_or_422(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_status = rec.get("status")
    old_in_dt = _ensure_utc(rec.get("punch_in")) if rec.get("punch_in") is not None else None
    old_out_dt = _ensure_utc(rec.get("punch_out")) if rec.get("punch_out") is not None else None
    old_work = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0) if rec.get("late_minutes") is not None else 0
    old_ot = rec.get("overtime_minutes", 0) if rec.get("overtime_minutes") is not None else 0
    old_half = rec.get("half_day", False) if rec.get("half_day") is not None else False
    old_hist = rec.get("history", []) or []
    old_len = len(old_hist)

    new_status = body.status if body.status is not None else old_status
    new_in_dt = dt_from_millis(int(body.punch_in)) if body.punch_in is not None else old_in_dt
    new_out_dt = dt_from_millis(int(body.punch_out)) if body.punch_out is not None else old_out_dt

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT/LEAVE")
        new_in_dt = None
        new_out_dt = None
    else:
        if new_in_dt is None:
            raise HTTPException(422, "presence status requires punch_in")

    if new_in_dt is not None:
        want = attendance_date_for(new_in_dt, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
        if want != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
    if new_out_dt is not None:
        if new_in_dt is None:
            raise HTTPException(422, "punch_out without punch_in")
        if not (new_out_dt > new_in_dt):
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_out_dt - new_in_dt).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out more than 24h after punch_in")

    def _ms(dt):
        return millis_from_dt(dt) if dt is not None else None

    if (
        new_status == old_status
        and _ms(new_in_dt) == _ms(old_in_dt)
        and _ms(new_out_dt) == _ms(old_out_dt)
    ):
        raise HTTPException(422, "request changes nothing")

    if new_status in ("ABSENT", "LEAVE"):
        new_late, new_work, new_ot, new_half = 0, None, 0, False
    else:
        new_late = compute_late_minutes(new_in_dt, emp.get("shift_start", "09:30"), date)
        if new_out_dt is not None:
            new_work = compute_work_hours(new_in_dt, new_out_dt)
            new_ot = compute_overtime(new_out_dt, shift_end_dt(date, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")))
            new_half = bool(new_work < 4.50)
        else:
            new_work, new_ot, new_half = None, 0, False

    changes_store: dict = {}
    changes_api: dict = {}
    pairs = [
        ("status", old_status, new_status, False),
        ("punch_in", old_in_dt, new_in_dt, True),
        ("punch_out", old_out_dt, new_out_dt, True),
        ("work_hours", old_work, new_work, False),
        ("late_minutes", old_late, new_late, False),
        ("overtime_minutes", old_ot, new_ot, False),
        ("half_day", old_half, new_half, False),
    ]
    for k, o, n, is_dt in pairs:
        ov = _ms(o) if is_dt else o
        nv = _ms(n) if is_dt else n
        if ov != nv:
            if is_dt:
                changes_store[k] = {"from": o, "to": n}
            else:
                changes_store[k] = {"from": o, "to": n}
            changes_api[k] = {"from": ov, "to": nv}
    if not changes_store:
        raise HTTPException(422, "request changes nothing")

    now_ms = truncate_millis(int(datetime.now(timezone.utc).timestamp() * 1000))
    entry_store = {"at": dt_from_millis(now_ms), "by": body.regularized_by, "reason": body.reason, "changes": changes_store}

    filt: dict = {"emp_code": emp_code, "date": date}
    # guard against concurrent history appends: require same history length
    filt["$expr"] = {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, old_len]}
    res = db.attendance_logs.update_one(
        filt,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in_dt,
                "punch_out": new_out_dt,
                "work_hours": new_work,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_half,
            },
            "$push": {"history": entry_store},
        },
    )
    if res.matched_count == 0:
        # distinguish lost race vs deleted (record existed a moment ago)
        if db.attendance_logs.find_one({"emp_code": emp_code, "date": date}) is None:
            raise HTTPException(404, "no record for that date")
        raise HTTPException(409, "concurrent modification")
    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers (pipelines built once, reused by endpoints + explain)
# --------------------------------------------------------------------------- #
DOW_EXPR = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
DOW_LOG_EXPR = {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}


def employee_monthly_pipeline(emp_code: str, month: str):
    ms, me = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": ms, "$lte": me}}},
        {"$addFields": {"_dow": DOW_EXPR}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_SET)]},
                                    {"$in": ["$_dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None):
    ms, me = month_bounds(month)
    match_emp: dict = {"joined_on": {"$lte": me}}
    if department is not None:
        match_emp["department"] = department
    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": ms, "$lte": me},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "_dow": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emps": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT_SET)]},
                                    {"$in": ["$_dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT_SET)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_cnt": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT_SET)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, department: Optional[str] = None):
    ms, me = month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": ms, "$lte": me}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})
    pipe += [
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipe


def department_trend_pipeline(department: str, from_s: str, to_s: str, dept_codes: list):
    from_dt = datetime.fromisoformat(from_s)
    to_dt = datetime.fromisoformat(to_s)
    from_bson = datetime(from_dt.year, from_dt.month, from_dt.day, tzinfo=timezone.utc)
    to_bson = datetime(to_dt.year, to_dt.month, to_dt.day, tzinfo=timezone.utc)
    to_excl = to_bson + timedelta(days=1)
    seed_match = {"department": department}
    return [
        {"$match": {"emp_code": {"$in": dept_codes}, "date": {"$gte": from_s, "$lte": to_s}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENT_SET)]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$project": {"_id": 0, "date": "$_id", "dateObj": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": seed_match},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date": from_s,
                            "dateObj": from_bson,
                            "present_count": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "dateObj": {"$first": "$dateObj"},
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {"$sort": {"dateObj": 1}},
        {"$densify": {"field": "dateObj", "range": {"step": 1, "unit": "day", "bounds": [from_bson, to_excl]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dateObj"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
                    {"$count": "n"},
                ],
                "as": "_hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$_hc.n"}, 0]},
                "_dow": {"$dayOfWeek": "$dateObj"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$_dow", [2, 3, 4, 5, 6]]},
                "attendance_rate": {
                    "$cond": [
                        {"$and": [{"$in": ["$_dow", [2, 3, 4, 5, 6]]}, {"$gt": [{"$ifNull": [{"$first": "$_hc.n"}, 0]}, 0]}]},
                        {"$divide": [{"$ifNull": ["$present_count", 0]}, {"$ifNull": [{"$first": "$_hc.n"}, 1]}]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dateObj": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"dateObj": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    parse_month_or_422(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    pipe = employee_monthly_pipeline(emp_code, month)
    rows = list(db.attendance_logs.aggregate(pipe))
    r = rows[0] if rows else {}
    present = r.get("present_days", 0)
    # normalize 2.0 -> 2 etc. keep numeric
    if isinstance(present, float) and present.is_integer():
        present = int(present)
    wd = working_days_in_month(month, emp.get("joined_on", month + "-01"))
    pct = None if wd == 0 else round_half_up(Decimal(str(present)) / Decimal(str(wd)) * Decimal("100"), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    parse_month_or_422(month)
    pipe = department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipe))
    items = []
    for r in rows:
        cnt = int(r.get("work_cnt", 0))
        avg = None if cnt == 0 else round_half_up(Decimal(str(r.get("work_sum", 0))) / Decimal(str(cnt)), 2)
        pres = r.get("present_days", 0)
        if isinstance(pres, float) and pres.is_integer():
            pres = int(pres)
        items.append(
            {
                "department": r["_id"],
                "headcount": len(r.get("emps", [])),
                "present_days": pres,
                "avg_work_hours": avg,
                "late_count": int(r.get("late_count", 0)),
                "total_late_minutes": int(r.get("total_late_minutes", 0)),
                "leave_count": int(r.get("leave_count", 0)),
                "on_duty_count": int(r.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    parse_month_or_422(month)
    pipe = late_leaderboard_pipeline(month, department)
    rows = list(db.attendance_logs.aggregate(pipe))
    items = []
    for r in rows:
        if int(r.get("rank", 0)) <= limit:
            items.append(
                {
                    "rank": int(r["rank"]),
                    "emp_code": r["emp_code"],
                    "name": r["name"],
                    "department": r["department"],
                    "total_late_minutes": int(r["total_late_minutes"]),
                    "late_count": int(r["late_count"]),
                }
            )
    items.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    parse_date_or_422(from_)
    parse_date_or_422(to)
    if to < from_:
        raise HTTPException(422, "to < from")
    d0 = datetime.fromisoformat(from_).date()
    d1 = datetime.fromisoformat(to).date()
    if (d1 - d0).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    # headcount per day from joined_on (python, small) — but pipeline also computes it;
    # we keep pipeline as source of truth for present/late + densify.
    pipe = department_trend_pipeline(department, from_, to, codes)
    rows = list(db.attendance_logs.aggregate(pipe))
    # index by date for safety, ensure full range
    by_date = {r["date"]: r for r in rows}
    items = []
    d = d0
    while d <= d1:
        ds = d.isoformat()
        r = by_date.get(ds)
        if r is None:
            # fallback (should not happen when densify works): compute headcount
            hc = sum(1 for e in emps if e.get("joined_on", "0000-00-00") <= ds)
            is_wd = d.weekday() < 5
            items.append({"date": ds, "is_working_day": is_wd, "headcount": hc, "present_count": 0, "late_count": 0, "attendance_rate": round_half_up(0, 4) if (is_wd and hc > 0) else None, "moving_avg_7d": None})
        else:
            hc = int(r.get("headcount", 0))
            pres = r.get("present_count", 0)
            if isinstance(pres, float) and pres.is_integer():
                pres = int(pres)
            rate = r.get("attendance_rate")
            mv = r.get("moving_avg_7d")
            rate = None if rate is None else round_half_up(rate, 4)
            mv = None if mv is None else round_half_up(mv, 4)
            items.append(
                {
                    "date": ds,
                    "is_working_day": bool(r.get("is_working_day", False)),
                    "headcount": hc,
                    "present_count": pres,
                    "late_count": int(r.get("late_count", 0)),
                    "attendance_rate": rate,
                    "moving_avg_7d": mv,
                }
            )
        d += timedelta(days=1)
    # moving average must be over non-null rates inside requested range with shorter
    # first windows. Mongo $avg already does this, but recompute in python over the
    # fetched (<=92) rows to guarantee half-up + null-window semantics regardless
    # of server version quirks. This loops over days, not over 100k logs.
    rates = [it["attendance_rate"] for it in items]
    for i, it in enumerate(items):
        window = [v for v in rates[max(0, i - 6): i + 1] if v is not None]
        it["moving_avg_7d"] = None if not window else round_half_up(sum(window) / len(window), 4)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from is not None:
            parse_date_or_422(date_from)
        if date_to is not None:
            parse_date_or_422(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from > date_to")
        q = attendance_list_query(emp_code, date_from, date_to, status)
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        out = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        parse_month_or_422(month)
        pipe = employee_monthly_pipeline(emp_code, month)
        out = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        parse_month_or_422(month)
        pipe = department_summary_pipeline(month, department)
        out = db.command({"explain": {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "employees", "explain": out}
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        parse_month_or_422(month)
        pipe = late_leaderboard_pipeline(month, department)
        out = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
    # department_trend
    dept = department
    if not dept or not from_ or not to:
        raise HTTPException(422, "department, from and to required")
    parse_date_or_422(from_)
    parse_date_or_422(to)
    if to < from_:
        raise HTTPException(422, "to < from")
    codes = [e["emp_code"] for e in db.employees.find({"department": dept}, {"emp_code": 1})]
    pipe = department_trend_pipeline(dept, from_, to, codes or ["__none__"])
    out = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}


# --------------------------------------------------------------------------- #
# Interactive UI (additive console; hidden from OpenAPI so the contract is untouched)
# --------------------------------------------------------------------------- #
UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attendance API console</title>
<style>
:root{
  color-scheme:light dark;
  --bg:#f8fafc; --surface:#ffffff; --border:#e2e8f0; --text:#0f172a; --muted:#64748b;
  --accent:#4f46e5; --accent-hover:#4338ca; --accent-ink:#ffffff;
  --ok-bg:#dcfce7; --ok-text:#15803d; --bad-bg:#fee2e2; --bad-text:#b91c1c; --idle-bg:#f1f5f9; --idle-text:#475569;
  --radius-card:12px; --radius-ctl:8px;
  --shadow:0 1px 2px rgb(0 0 0 / .05);
}
@media (prefers-color-scheme:dark){
  :root{--bg:#020617; --surface:#0f172a; --border:#1e293b; --text:#f1f5f9; --muted:#94a3b8;
    --idle-bg:#1e293b; --idle-text:#cbd5e1; --shadow:none}
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;background:var(--bg);color:var(--text);
  padding-block:0 32px}
.skip{position:absolute;inset-inline-start:12px;top:-48px;z-index:50;background:var(--accent);color:var(--accent-ink);
  padding:8px 12px;border-radius:var(--radius-ctl);transition:top 150ms ease-out}
.skip:focus{top:12px}
.topbar{position:sticky;top:0;z-index:30;background:var(--surface);border-block-end:1px solid var(--border);
  padding-block:12px;padding-inline:max(16px,env(safe-area-inset-left)) max(16px,env(safe-area-inset-right))}
.topbar-in{max-width:1200px;margin-inline:auto;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.topbar h1{font-size:18px;margin:0;text-wrap:balance}
.topbar .sub{color:var(--muted);font-size:13px;margin:0;text-wrap:pretty}
.topbar .spacer{flex:1}
.health{display:inline-flex;align-items:center;gap:8px;font-size:13px;border:1px solid var(--border);
  border-radius:999px;padding:4px 12px;background:var(--surface);font-variant-numeric:tabular-nums}
.health .dot{inline-size:8px;block-size:8px;border-radius:50%;background:var(--muted)}
.health.ok .dot{background:var(--ok-text)} .health.bad .dot{background:var(--bad-text)}
.docs-link{font-size:13px;color:var(--accent);text-decoration:none}
.docs-link:hover{text-decoration:underline}
.wrap{max-width:1200px;margin-inline:auto;padding-inline:max(16px,env(safe-area-inset-left)) max(16px,env(safe-area-inset-right))}
.helper{margin-block-start:24px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-card);
  box-shadow:var(--shadow);padding:16px}
.helper h2{font-size:15px;margin:0 0 4px;text-wrap:balance}
.helper p{font-size:13px;color:var(--muted);margin:0 0 8px;text-wrap:pretty}
.helper-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 12px}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(340px,400px);gap:24px;margin-block-start:32px;align-items:start}
@media (max-width:980px){.workspace{grid-template-columns:minmax(0,1fr)}}
section.group{display:flex;flex-direction:column;gap:16px;margin-block:0 32px}
section.group:last-child{margin-block-end:0}
.group-title{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;margin:0}
.group-title + .card{margin-block-start:0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-card);
  box-shadow:var(--shadow);padding:16px}
.card h2{font-size:15px;margin:0;text-wrap:balance;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.method{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums;background:var(--idle-bg);color:var(--idle-text);
  border-radius:6px;padding:2px 8px;white-space:nowrap}
.card .path{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
label{display:block;font-size:12px;font-weight:600;margin-block:12px 4px}
label:first-of-type{margin-block-start:8px}
input,select{inline-size:100%;padding:8px 10px;font-size:13px;border:1px solid var(--border);border-radius:var(--radius-ctl);
  background:var(--surface);color:var(--text);font-variant-numeric:tabular-nums}
input:focus-visible,select:focus-visible,button:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input[readonly]{background:var(--idle-bg)}
.row{display:flex;gap:12px;margin-block-start:12px;flex-wrap:wrap}
button{font-size:13px;font-weight:600;border-radius:var(--radius-ctl);padding:8px 14px;cursor:pointer;
  border:1px solid transparent;transition:opacity 150ms ease-out}
.btn-primary{background:var(--accent);color:var(--accent-ink)}
.btn-primary:hover{background:var(--accent-hover)}
.btn-ghost{background:transparent;color:var(--text);border-color:var(--border)}
.btn-ghost:hover{border-color:var(--muted)}
.card-status{font-size:12px;margin-block-start:8px;min-block-size:18px;color:var(--muted);font-variant-numeric:tabular-nums}
.card-status:empty::before{content:"Not run yet.";color:var(--muted)}
.card-status.ok{color:var(--ok-text)} .card-status.bad{color:var(--bad-text)}
.hint{font-size:12px;color:var(--muted);margin-block-start:8px;text-wrap:pretty}
code{font-size:12px;font-variant-numeric:tabular-nums}
.inspector{position:sticky;top:76px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-card);box-shadow:var(--shadow);padding:16px;max-block-size:calc(100dvh - 100px);
  display:flex;flex-direction:column}
@media (max-width:980px){.inspector{position:static;max-block-size:none}}
.inspector h2{font-size:15px;margin:0;text-wrap:balance}
.meta{display:flex;align-items:center;gap:8px;margin-block:8px;flex-wrap:wrap}
.pill{font-size:12px;font-weight:700;border-radius:999px;padding:2px 10px;background:var(--idle-bg);color:var(--idle-text);
  font-variant-numeric:tabular-nums}
.pill.ok{background:var(--ok-bg);color:var(--ok-text)} .pill.bad{background:var(--bad-bg);color:var(--bad-text)}
.timing{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.icon-btn{margin-inline-start:auto;background:transparent;border:1px solid var(--border);border-radius:var(--radius-ctl);
  padding:6px 10px;font-size:12px;color:var(--text)}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.5;margin:0;overflow:auto;
  border:1px solid var(--border);border-radius:var(--radius-ctl);padding:12px;background:var(--bg);color:var(--text);
  font-variant-numeric:tabular-nums;flex:1;min-block-size:120px}
.skel{display:flex;flex-direction:column;gap:8px;padding:12px}
.skel span{display:block;block-size:12px;border-radius:6px;background:var(--border);opacity:.7;animation:pulse 1.4s ease-out infinite alternate}
.skel span:nth-child(2){inline-size:82%} .skel span:nth-child(3){inline-size:64%}
@keyframes pulse{from{opacity:.35}to{opacity:.7}}
@media (prefers-reduced-motion:reduce){.skel span{animation:none}.skip,button{transition:none}html{scroll-behavior:auto}}
.empty{font-size:13px;color:var(--muted);text-wrap:pretty}
.empty strong{color:var(--text)}
</style>
</head>
<body>
<a class="skip" href="#inspector">Skip to response viewer</a>
<header class="topbar"><div class="topbar-in">
<div><h1>Attendance API console</h1>
<p class="sub">Same-origin calls only &middot; spec at <a class="docs-link" href="/docs"><code>/docs</code></a> &middot; times are epoch <b>ms</b>, IST rules R1 apply.</p></div>
<div class="spacer"></div>
<span class="health" id="healthPill" role="status"><span class="dot"></span><span id="healthText">checking&hellip;</span></span>
</div></header>

<div class="wrap">
<div class="helper">
<h2>Millis helper</h2>
<p>IST wall time &rarr; epoch ms. Parse assumes +05:30. Copy the number into any <code>punched_at</code> / <code>punch_in</code> field.</p>
<div class="helper-grid">
<div><label for="istIn">IST datetime (YYYY-MM-DDTHH:MM)</label>
<input id="istIn" type="datetime-local" value="2026-07-20T09:40"></div>
<div><label for="istOut">Epoch ms</label>
<input id="istOut" readonly tabular-nums></div>
</div>
<div class="row">
<button class="btn-ghost" onclick="istToMs()">Convert</button>
<button class="btn-ghost" onclick="setNow('istOut')">Now</button>
</div>
</div>

<div class="workspace"><div>
<section class="group" aria-label="System and people">
<p class="group-title">System &middot; People</p>
<div class="card" id="card-health"><h2><span class="method">GET</span> /health</h2>
<div class="row"><button class="btn-primary" onclick="sendCard('card-health','GET','/health')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-emp-create"><h2><span class="method">POST</span> <span class="path">/employees</span></h2>
<label>emp_code (EMP + 4-6 digits)</label><input id="eCode" value="EMP0100">
<label>name</label><input id="eName" value="Test User">
<label>email</label><input id="eMail" value="test@example.com">
<label>department</label><input id="eDept" value="Engineering">
<label>shift_start</label><input id="eS1" value="09:30">
<label>shift_end</label><input id="eS2" value="18:30">
<label for="eJoin">joined_on (YYYY-MM-DD)</label><input id="eJoin" value="2026-07-01">
<div class="row"><button class="btn-primary" onclick="empCreate('card-emp-create')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-emp-list"><h2><span class="method">GET</span> <span class="path">/employees</span></h2>
<label for="leDept">department (optional)</label><input id="leDept" placeholder="Engineering">
<label for="lePage">page</label><input id="lePage" type="number" value="1">
<label for="leSize">page_size</label><input id="leSize" type="number" value="20">
<div class="row"><button class="btn-primary" onclick="empList('card-emp-list')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>
</section>

<section class="group" aria-label="Attendance">
<p class="group-title">Attendance</p>
<div class="card" id="card-punch-in"><h2><span class="method">POST</span> <span class="path">/attendance/punch-in</span></h2>
<label for="piEmp">emp_code</label><input id="piEmp" value="EMP0100">
<label for="piAt">punched_at ms (empty = now)</label><input id="piAt" placeholder="e.g. 1784520600000">
<label for="piSt">status</label><select id="piSt"><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option></select>
<div class="row"><button class="btn-primary" onclick="punchIn('card-punch-in')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-punch-out"><h2><span class="method">POST</span> <span class="path">/attendance/punch-out</span></h2>
<label for="poEmp">emp_code</label><input id="poEmp" value="EMP0100">
<label for="poAt">punched_at ms (empty = now)</label><input id="poAt" placeholder="e.g. 1784554800000">
<div class="row"><button class="btn-primary" onclick="punchOut('card-punch-out')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-att-list"><h2><span class="method">GET</span> <span class="path">/attendance</span></h2>
<label for="laEmp">emp_code</label><input id="laEmp" placeholder="EMP0100">
<label for="laFrom">date_from</label><input id="laFrom" placeholder="2026-07-01">
<label for="laTo">date_to</label><input id="laTo" placeholder="2026-07-31">
<label for="laSt">status</label><select id="laSt"><option value="">(any)</option><option>PRESENT</option><option>ABSENT</option><option>LEAVE</option><option>WFH</option><option>ON_DUTY</option></select>
<label for="laPage">page</label><input id="laPage" type="number" value="1">
<label for="laSize">page_size</label><input id="laSize" type="number" value="20">
<div class="row"><button class="btn-primary" onclick="attList('card-att-list')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-patch"><h2><span class="method">PATCH</span> <span class="path">/attendance/{emp}/{date}</span></h2>
<label for="paEmp">emp_code</label><input id="paEmp" value="EMP0100">
<label for="paDate">date (record date)</label><input id="paDate" value="2026-07-20">
<label for="paSt">status (optional)</label><select id="paSt"><option value="">(unchanged)</option><option>PRESENT</option><option>ABSENT</option><option>LEAVE</option><option>WFH</option><option>ON_DUTY</option></select>
<label for="paIn">punch_in ms (optional)</label><input id="paIn" placeholder="epoch ms">
<label for="paOut">punch_out ms (optional)</label><input id="paOut" placeholder="epoch ms">
<label for="paReason">reason (5-200 chars)</label><input id="paReason" value="correct time entry">
<label for="paBy">regularized_by</label><input id="paBy" value="hr.admin">
<div class="row"><button class="btn-primary" onclick="regularize('card-patch')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>
</section>

<section class="group" aria-label="Analytics">
<p class="group-title">Analytics</p>
<div class="card" id="card-monthly"><h2><span class="method">GET</span> <span class="path">monthly</span></h2>
<label for="moEmp">emp_code</label><input id="moEmp" value="EMP0100">
<label for="moM">month (YYYY-MM)</label><input id="moM" value="2026-07">
<div class="row"><button class="btn-primary" onclick="monthly('card-monthly')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-dept"><h2><span class="method">GET</span> <span class="path">departments/summary</span></h2>
<label for="dsM">month</label><input id="dsM" value="2026-07">
<label for="dsD">department (optional)</label><input id="dsD" placeholder="Engineering">
<div class="row"><button class="btn-primary" onclick="deptSummary('card-dept')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-lead"><h2><span class="method">GET</span> <span class="path">leaderboard/late</span></h2>
<label for="lbM">month</label><input id="lbM" value="2026-07">
<label for="lbL">limit</label><input id="lbL" type="number" value="10">
<label for="lbD">department (optional)</label><input id="lbD" placeholder="Engineering">
<div class="row"><button class="btn-primary" onclick="leaderboard('card-lead')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>

<div class="card" id="card-trend"><h2><span class="method">GET</span> <span class="path">departments/{dept}/trend</span></h2>
<label for="trD">department</label><input id="trD" value="Engineering">
<label for="trF">from</label><input id="trF" value="2026-07-01">
<label for="trT">to</label><input id="trT" value="2026-07-07">
<div class="row"><button class="btn-primary" onclick="trend('card-trend')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div></div>
</section>

<section class="group" aria-label="Admin">
<p class="group-title">Admin</p>
<div class="card" id="card-explain"><h2><span class="method">GET</span> <span class="path">admin/explain</span></h2>
<label for="exE">endpoint</label><select id="exE"><option>attendance_list</option><option>employee_monthly</option><option>department_summary</option><option>late_leaderboard</option><option>department_trend</option></select>
<label for="exQ">extra query (e.g. month=2026-07&amp;emp_code=EMP0100)</label><input id="exQ" value="month=2026-07">
<div class="row"><button class="btn-primary" onclick="explain('card-explain')">Send</button></div>
<div class="card-status" data-status aria-live="polite"></div>
<div class="hint">Pass the same params the target endpoint needs.</div></div>
</section>
</div>

<aside class="inspector" id="inspector" aria-label="Response viewer">
<h2>Response</h2>
<div class="meta"><span class="pill" id="status">idle</span><span class="timing" id="timing"></span>
<button class="icon-btn" id="copyBtn" aria-label="Copy response JSON" onclick="copyResp()">Copy</button></div>
<pre id="body" aria-live="polite"><span class="empty"><strong>No response yet.</strong> Run GET /health to verify the server, then any card above.</span></pre></aside>
</div>
</div>

<script>
let lastRaw='';
function setPill(el, ok){
  el.classList.remove('ok','bad');
  if(ok===true)el.classList.add('ok'); else if(ok===false)el.classList.add('bad');
}
function cardStatus(id, text, ok){
  if(!id)return;
  const card=document.getElementById(id);
  if(!card)return;
  const s=card.querySelector('[data-status]');
  if(!s)return;
  s.textContent=text; s.classList.remove('ok','bad');
  if(ok===true)s.classList.add('ok'); else if(ok===false)s.classList.add('bad');
}
function skeleton(){
  const bd=document.getElementById('body');
  bd.innerHTML='<div class="skel" aria-hidden="true"><span></span><span></span><span></span></div>';
}
async function call(method, path, body, cardId){
  const pill=document.getElementById('status'), bd=document.getElementById('body'), tm=document.getElementById('timing');
  skeleton(); tm.textContent='';
  pill.textContent=method+' '+path+' …'; setPill(pill,null);
  if(cardId)cardStatus(cardId, method+' '+path+' …');
  const t0=performance.now();
  try{
    const r=await fetch(path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
    const txt=await r.text();
    const ms=Math.round(performance.now()-t0);
    let pretty=txt; try{pretty=JSON.stringify(JSON.parse(txt),null,1);}catch(e){}
    lastRaw=pretty;
    const ok=r.ok;
    pill.textContent=method+' '+path+' → '+r.status; setPill(pill,ok);
    tm.textContent=ms+' ms';
    bd.textContent=pretty;
    if(cardId)cardStatus(cardId, (ok?'✓ ':'✗ ')+r.status+' · '+ms+' ms', ok);
  }catch(e){
    pill.textContent='network error'; setPill(pill,false);
    bd.textContent=String(e); lastRaw=String(e);
    if(cardId)cardStatus(cardId,'✗ network error',false);
  }
}
async function copyResp(){
  try{await navigator.clipboard.writeText(lastRaw);const b=document.getElementById('copyBtn');b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1200);}catch(e){}
}
async function healthPing(){
  try{
    const r=await fetch('/health'); const p=document.getElementById('healthPill'), t=document.getElementById('healthText');
    p.classList.remove('ok','bad'); p.classList.add(r.ok?'ok':'bad');
    t.textContent=r.ok?'api up':'api error '+r.status;
  }catch(e){const p=document.getElementById('healthPill');p.classList.add('bad');document.getElementById('healthText').textContent='unreachable';}
}
document.addEventListener('DOMContentLoaded', healthPing);
const v=id=>document.getElementById(id).value.trim();
const num=id=>{const s=v(id);return s==='' ? undefined : Number(s);};
function qs(o){return Object.entries(o).filter(([k,x])=>x!==undefined&&x!==''&&x!==null).map(([k,x])=>encodeURIComponent(k)+'='+encodeURIComponent(x)).join('&');}
function istToMs(){const s=v('istIn');if(!s)return;const ms=Date.parse(s+':00+05:30');document.getElementById('istOut').value=isNaN(ms)?'invalid':ms;}
function setNow(id){document.getElementById(id).value=Math.floor(Date.now()/1000)*1000;}
function sendCard(id,m,p){call(m,p,undefined,id);}
function empCreate(id){call('POST','/employees',{emp_code:v('eCode'),name:v('eName'),email:v('eMail'),department:v('eDept'),shift_start:v('eS1'),shift_end:v('eS2'),joined_on:v('eJoin')},id);}
function empList(id){const q=qs({department:v('leDept'),page:v('lePage'),page_size:v('leSize')});call('GET','/employees?'+q,undefined,id);}
function punchIn(id){const b={emp_code:v('piEmp'),status:v('piSt')};const a=num('piAt');if(a!==undefined)b.punched_at=a;call('POST','/attendance/punch-in',b,id);}
function punchOut(id){const b={emp_code:v('poEmp')};const a=num('poAt');if(a!==undefined)b.punched_at=a;call('POST','/attendance/punch-out',b,id);}
function attList(id){const q=qs({emp_code:v('laEmp'),date_from:v('laFrom'),date_to:v('laTo'),status:v('laSt'),page:v('laPage'),page_size:v('laSize')});call('GET','/attendance?'+q,undefined,id);}
function regularize(id){
  if((v('paSt')==='ABSENT'||v('paSt')==='LEAVE')&&(v('paIn')!==''||v('paOut')!=='')){
    cardStatus(id,'✗ punch times cannot be sent with ABSENT/LEAVE',false); return;
  }
  if(v('paSt')==='ABSENT'||v('paSt')==='LEAVE'){
    if(!confirm('Set status to '+v('paSt')+'? This clears both punch times.'))return;
  }
  const b={reason:v('paReason'),regularized_by:v('paBy')};if(v('paSt'))b.status=v('paSt');const i=num('paIn'),o=num('paOut');if(i!==undefined)b.punch_in=i;if(o!==undefined)b.punch_out=o;call('PATCH','/attendance/'+encodeURIComponent(v('paEmp'))+'/'+encodeURIComponent(v('paDate')),b,id);
}
function monthly(id){call('GET','/analytics/employees/'+encodeURIComponent(v('moEmp'))+'/monthly?'+qs({month:v('moM')}),undefined,id);}
function deptSummary(id){call('GET','/analytics/departments/summary?'+qs({month:v('dsM'),department:v('dsD')}),undefined,id);}
function leaderboard(id){call('GET','/analytics/leaderboard/late?'+qs({month:v('lbM'),limit:v('lbL'),department:v('lbD')}),undefined,id);}
function trend(id){call('GET','/analytics/departments/'+encodeURIComponent(v('trD'))+'/trend?'+qs({from:v('trF'),to:v('trT')}),undefined,id);}
function explain(id){const e=v('exE');const q=v('exQ');call('GET','/admin/explain/'+e+(q?'?'+q:''),undefined,id);}
/* back-compat: old inline handlers without a card id */
function callLegacy(m,p,b){call(m,p,b,undefined);}
</script>
</body>
</html>"""


@app.get("/ui", include_in_schema=False)
def ui():
    return HTMLResponse(UI_HTML)
