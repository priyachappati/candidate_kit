"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import os
from datetime import datetime, timedelta, timezone, date as date_cls
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Tuple, Dict, Any

from bson import ObjectId
from bson.timestamp import Timestamp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Config & client
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set in the environment")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Indexes at startup (idempotent)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", ASCENDING)], name="dept_idx")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="emp_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_idx"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_out", ASCENDING), ("punch_in", DESCENDING)],
        name="emp_open_punchin_idx",
    )
    db.attendance_logs.create_index([("date", ASCENDING)], name="date_idx")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def truncate_sec(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def parse_hhmm(s: str) -> Tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    return (eh, em) <= (sh, sm)


def compute_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in, with overnight adjustment."""
    local = to_ist(punch_in)
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if (local.hour, local.minute) < (eh, em):
            local = local - timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """R2: grace 10 min; late only if strictly > 10:00 after shift_start."""
    local = to_ist(punch_in)
    sh, sm = parse_hhmm(shift_start)
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    delta_sec = (local - start).total_seconds()
    if delta_sec <= 600:
        return 0
    return int(delta_sec // 60)


def shift_end_dt(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    """The IST instant at which the shift ends for this attendance date."""
    eh, em = parse_hhmm(shift_end)
    d = date_cls.fromisoformat(attendance_date)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end = end + timedelta(days=1)
    return end


def compute_overtime_minutes(punch_out: datetime,
                             attendance_date: str,
                             shift_start: str,
                             shift_end: str) -> int:
    """R3: floor(min from shift_end to punch_out); only if >= 30 else 0."""
    end = shift_end_dt(attendance_date, shift_start, shift_end)
    out_ist = to_ist(punch_out)
    mins = int((out_ist - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) seconds / 3600, 2dp half-up."""
    secs = (punch_out - punch_in).total_seconds()
    hours = Decimal(secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half day if rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_derived(status: str,
                    punch_in: Optional[datetime],
                    punch_out: Optional[datetime],
                    attendance_date: Optional[str],
                    shift_start: str,
                    shift_end: str) -> Dict[str, Any]:
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in, shift_start)
    if punch_out is None:
        return {
            "late_minutes": late,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    wh = compute_work_hours(punch_in, punch_out)
    ot = compute_overtime_minutes(punch_out, attendance_date, shift_start, shift_end)
    hd = compute_half_day(wh)
    return {
        "late_minutes": late,
        "work_hours": wh,
        "overtime_minutes": ot,
        "half_day": hd,
    }


def _jsonable(obj):
    """Recursively convert BSON / non-JSON types to JSON-safe values."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Timestamp):
        return {"time": obj.time, "inc": obj.inc}
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return to_ms(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.hex()
    if isinstance(obj, bytearray):
        try:
            return bytes(obj).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(obj).hex()
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


def parse_iso_date_strict(s: str) -> Optional[date_cls]:
    """Parse YYYY-MM-DD strictly; return None on any issue (whitespace, bad format, etc.)."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) != 10:
        return None
    try:
        d = date_cls.fromisoformat(s)
    except ValueError:
        return None
    return d


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc["created_at"]),
    }


def serialize_history_entry(h: dict) -> dict:
    changes_in = h.get("changes", {}) or {}
    changes_out = {}
    for k, v in changes_in.items():
        if k in ("punch_in", "punch_out"):
            changes_out[k] = {
                "from": to_ms(v.get("from")),
                "to": to_ms(v.get("to")),
            }
        else:
            changes_out[k] = {"from": v.get("from"), "to": v.get("to")}
    return {
        "at": to_ms(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes_out,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v):
        date_cls.fromisoformat(v)
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def _presence(cls, v):
        if v not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
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
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Punch-in
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = truncate_sec(from_ms(body.punched_at))
    else:
        ts = truncate_sec(datetime.now(UTC))

    att_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
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


# --------------------------------------------------------------------------- #
# Punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = truncate_sec(from_ms(body.punched_at))
    else:
        ts = truncate_sec(datetime.now(UTC))

    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no open punch-in found")

    pin = rec["punch_in"]
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=UTC)
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pin) > timedelta(hours=24):
        raise HTTPException(422, "punched_at more than 24h after punch_in")

    derived = compute_derived(
        status=rec["status"],
        punch_in=pin,
        punch_out=ts,
        attendance_date=rec["date"],
        shift_start=emp["shift_start"],
        shift_end=emp["shift_end"],
    )

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Attendance list
# --------------------------------------------------------------------------- #
def _attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng: Dict[str, str] = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        date_from = date_from.strip()
        try:
            date_cls.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(422, "invalid date_from")
    if date_to is not None:
        date_to = date_to.strip()
        try:
            date_cls.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(422, "invalid date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")

    q = _attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# PATCH regularize
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try:
        date_cls.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "invalid date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    new_status = body.status if body.status is not None else rec["status"]
    new_pin = rec.get("punch_in")
    new_pout = rec.get("punch_out")

    if body.punch_in is not None:
        new_pin = truncate_sec(from_ms(body.punch_in))
    if body.punch_out is not None:
        new_pout = truncate_sec(from_ms(body.punch_out))

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_pin = None
        new_pout = None
    else:
        if new_pin is None:
            raise HTTPException(422, "presence status requires punch_in")

    if new_pin is not None:
        pin_date = compute_attendance_date(new_pin, emp["shift_start"], emp["shift_end"])
        if pin_date != date:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")

    if new_pin is not None and new_pout is not None:
        if new_pout <= new_pin:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_pout - new_pin) > timedelta(hours=24):
            raise HTTPException(422, "punch_out more than 24h after punch_in")

    derived = compute_derived(
        status=new_status,
        punch_in=new_pin,
        punch_out=new_pout,
        attendance_date=date,
        shift_start=emp["shift_start"],
        shift_end=emp["shift_end"],
    )

    changes: Dict[str, Dict[str, Any]] = {}

    def _norm(dt):
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    old_status = rec["status"]
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}

    old_pin = _norm(rec.get("punch_in"))
    if _norm(new_pin) != old_pin:
        changes["punch_in"] = {"from": rec.get("punch_in"), "to": new_pin}

    old_pout = _norm(rec.get("punch_out"))
    if _norm(new_pout) != old_pout:
        changes["punch_out"] = {"from": rec.get("punch_out"), "to": new_pout}

    old_late = rec.get("late_minutes", 0)
    if derived["late_minutes"] != old_late:
        changes["late_minutes"] = {"from": old_late, "to": derived["late_minutes"]}

    old_wh = rec.get("work_hours")
    if derived["work_hours"] != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": derived["work_hours"]}

    old_ot = rec.get("overtime_minutes", 0)
    if derived["overtime_minutes"] != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": derived["overtime_minutes"]}

    old_hd = rec.get("half_day", False)
    if derived["half_day"] != old_hd:
        changes["half_day"] = {"from": old_hd, "to": derived["half_day"]}

    if not changes:
        raise HTTPException(422, "no changes")

    hist_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "history": rec.get("history", [])},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "late_minutes": derived["late_minutes"],
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": hist_entry},
        },
    )
    if res.modified_count == 0:
        raise HTTPException(409, "concurrent modification, retry")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> Tuple[str, str, int, int]:
    y, m = month.split("-")
    yi, mi = int(y), int(m)
    first = date_cls(yi, mi, 1)
    if mi == 12:
        nxt = date_cls(yi + 1, 1, 1)
    else:
        nxt = date_cls(yi, mi + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat(), yi, mi


def count_working_days(start: date_cls, end: date_cls) -> int:
    if end < start:
        return 0
    n = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# Employee monthly
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> Tuple[str, list]:
    first, last, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$addFields": {"_d": {"$dateFromString": {"dateString": "$date"}}}},
        {"$addFields": {"_dow": {"$dayOfWeek": "$_d"}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$lte": ["$_dow", 6]},
                ]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    return "attendance_logs", pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    first, last, _, _ = month_bounds(month)
    joined = date_cls.fromisoformat(emp["joined_on"])
    month_first = date_cls.fromisoformat(first)
    month_last = date_cls.fromisoformat(last)
    start = max(joined, month_first)
    working_days = count_working_days(start, month_last)

    coll, pipe = build_employee_monthly_pipeline(emp_code, month)
    rows = list(db[coll].aggregate(pipe))
    row = rows[0] if rows else {}

    present_days = float(row.get("present_days", 0))
    leave_days = int(row.get("leave_days", 0))
    late_count = int(row.get("late_count", 0))
    total_late = int(row.get("total_late_minutes", 0))
    total_ot = int(row.get("total_overtime_minutes", 0))

    pct = None if working_days == 0 else round_half_up(present_days / working_days * 100.0, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Department summary
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(month: str, department: Optional[str]) -> Tuple[str, list]:
    first, last, _, _ = month_bounds(month)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$and": [
                        {"$in": ["$emp_code", "$$codes"]},
                        {"$gte": ["$date", first]},
                        {"$lte": ["$date", last]},
                    ]},
                }},
                {"$addFields": {"_d": {"$dateFromString": {"dateString": "$date"}}}},
                {"$addFields": {"_dow": {"$dayOfWeek": "$_d"}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": ["$_dow", 6]},
                        ]},
                        {"$cond": ["$half_day", 0.5, 1]},
                        0,
                    ]}},
                    "wh_sum": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        "$work_hours", 0,
                    ]}},
                    "wh_count": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        1, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "agg",
        }},
        {"$addFields": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": {"$ifNull": ["$agg.present_days", 0]},
            "wh_sum": {"$ifNull": ["$agg.wh_sum", 0]},
            "wh_count": {"$ifNull": ["$agg.wh_count", 0]},
            "late_count": {"$ifNull": ["$agg.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$agg.total_late_minutes", 0]},
            "leave_count": {"$ifNull": ["$agg.leave_count", 0]},
            "on_duty_count": {"$ifNull": ["$agg.on_duty_count", 0]},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]
    return "employees", pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    coll, pipe = build_department_summary_pipeline(month, department)
    rows = list(db[coll].aggregate(pipe))
    items = []
    for r in rows:
        wh_sum = float(r.get("wh_sum", 0) or 0)
        wh_count = int(r.get("wh_count", 0) or 0)
        avg_wh = None if wh_count == 0 else round_half_up(wh_sum / wh_count, 2)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r.get("present_days", 0)),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Leaderboard
# --------------------------------------------------------------------------- #
def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> Tuple[str, list]:
    first, last, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return "attendance_logs", pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    coll, pipe = build_leaderboard_pipeline(month, limit, department)
    rows = list(db[coll].aggregate(pipe))
    items = [{
        "rank": int(r["rank"]),
        "emp_code": r["emp_code"],
        "name": r["name"],
        "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]),
        "late_count": int(r["late_count"]),
    } for r in rows]
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #
def build_trend_pipeline(department: str, date_from: str, date_to: str) -> Tuple[str, list]:
    date_from = date_from.strip()
    date_to = date_to.strip()
    start_dt = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(date_to).replace(tzinfo=UTC)

    pipeline = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$addFields": {"_d": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {
            "field": "_d",
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
        }},
        {"$addFields": {
            "_dateStr": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_d"}},
            "_dow": {"$dayOfWeek": "$_d"},
        }},
        {"$lookup": {
            "from": "employees",
            "pipeline": [
                {"$match": {"department": department}},
                {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
            ],
            "as": "dept_emps",
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {
                "input": "$dept_emps",
                "as": "e",
                "cond": {"$lte": ["$$e.joined_on", "$_dateStr"]},
            }}},
            "is_working_day": {"$lte": ["$_dow", 6]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"_d": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$sort": {"_d": 1}},
        {"$project": {
            "_id": 0,
            "date": "$_dateStr",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": "$moving_avg_7d_raw",
        }},
    ]
    return "attendance_logs", pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    d1 = parse_iso_date_strict(from_)
    d2 = parse_iso_date_strict(to)
    if d1 is None or d2 is None:
        raise HTTPException(422, "invalid date format; use YYYY-MM-DD")
    if d2 < d1:
        raise HTTPException(422, "to must be >= from")
    if (d2 - d1).days > 91:
        raise HTTPException(422, "range exceeds 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")

    coll, pipe = build_trend_pipeline(department, d1.isoformat(), d2.isoformat())
    rows = list(db[coll].aggregate(pipe))

    items = []
    for r in rows:
        ar = r.get("attendance_rate")
        ma = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if ar is None else round_half_up(ar, 4),
            "moving_avg_7d": None if ma is None else round_half_up(ma, 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = _attendance_query(emp_code, date_from, date_to, status)
        cur = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        plan = cur.explain()
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _jsonable(plan),
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        coll, pipe = build_employee_monthly_pipeline(emp_code, month)
        plan = db.command(
            "explain",
            {"aggregate": coll, "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": coll,
            "explain": _jsonable(plan),
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        coll, pipe = build_department_summary_pipeline(month, department)
        plan = db.command(
            "explain",
            {"aggregate": coll, "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": coll,
            "explain": _jsonable(plan),
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        coll, pipe = build_leaderboard_pipeline(month, limit, department)
        plan = db.command(
            "explain",
            {"aggregate": coll, "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": coll,
            "explain": _jsonable(plan),
        }

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, to required")
        d1 = parse_iso_date_strict(from_)
        d2 = parse_iso_date_strict(to)
        if d1 is None or d2 is None:
            raise HTTPException(422, "invalid date format; use YYYY-MM-DD")
        if d2 < d1:
            raise HTTPException(422, "to must be >= from")
        if (d2 - d1).days > 91:
            raise HTTPException(422, "range exceeds 92 days")
        coll, pipe = build_trend_pipeline(department, d1.isoformat(), d2.isoformat())
        plan = db.command(
            "explain",
            {"aggregate": coll, "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": coll,
            "explain": _jsonable(plan),
        }

    raise HTTPException(422, "unknown endpoint")