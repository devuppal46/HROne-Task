"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file
  1. Configuration and database connection
  2. Time, rounding and business-rule helpers (R1 - R5)
  3. Serialisation (BSON datetimes -> epoch milliseconds, never _id)
  4. Request models and validation
  5. Startup: indexes
  6. Endpoints: system, employees, attendance
  7. Analytics pipeline builders (shared by the endpoints and /admin/explain)
  8. Endpoints: analytics, admin
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # real environment variables take priority over .env

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
NON_PRESENCE = ["ABSENT", "LEAVE"]
ALL_STATUSES = PRESENCE + NON_PRESENCE

GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = StrictInt  # bounds are checked by the Field(ge, le) on each use


# --------------------------------------------------------------------------- #
# 2. Helpers: time, rounding, business rules
# --------------------------------------------------------------------------- #
def unprocessable(msg: str, loc: Optional[list] = None) -> HTTPException:
    """A 422 whose body has the same shape as FastAPI's own validation errors."""
    return HTTPException(422, detail=[{"loc": loc or ["body"], "msg": msg, "type": "value_error"}])


def half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def parse_date(value: str, loc: list) -> date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise unprocessable("must be a date in YYYY-MM-DD format", loc)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable("must be a valid calendar date", loc)


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of the punch; an overnight-shift punch before shift_end belongs to the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start_at(att_date: date, emp: dict) -> datetime:
    return datetime.combine(att_date, parse_hhmm(emp["shift_start"]), IST)


def shift_end_at(att_date: date, emp: dict) -> datetime:
    end_day = att_date + timedelta(days=1) if is_overnight(emp) else att_date
    return datetime.combine(end_day, parse_hhmm(emp["shift_end"]), IST)


def compute_late_minutes(punch_in: datetime, emp: dict, att_date: date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((as_utc(punch_in) - shift_start_at(att_date, emp)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (Decimal, not round(), which is banker's rounding)."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float(half_up(Decimal(seconds) / Decimal(3600), 2))


def compute_overtime(punch_out: datetime, emp: dict, att_date: date) -> int:
    """R3: floor minutes after shift end, counted only when >= 30. Overnight shifts end the next day."""
    seconds = int((as_utc(punch_out) - shift_end_at(att_date, emp)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: based on the rounded work_hours."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_HOURS


def derive_fields(status: str, punch_in, punch_out, emp: dict, att_date: date) -> dict:
    """The four derived fields for a record, per the table in DATA_MODEL.md section 3."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp, att_date)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, emp, att_date),
        "half_day": compute_half_day(hours),
    }


# --------------------------------------------------------------------------- #
# 3. Serialisation: every instant leaves the API as epoch ms; _id never leaves
# --------------------------------------------------------------------------- #
INSTANT_CHANGE_KEYS = {"punch_in", "punch_out"}


def ser_change_value(key: str, value: Any) -> Any:
    if key in INSTANT_CHANGE_KEYS and isinstance(value, datetime):
        return dt_to_ms(value)
    return value


def ser_history(entry: dict) -> dict:
    changes = entry.get("changes") or {}
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            k: {"from": ser_change_value(k, v.get("from")), "to": ser_change_value(k, v.get("to"))}
            for k, v in changes.items()
        },
    }


def ser_record(doc: dict) -> dict:
    """Legacy records may miss half_day / history / derived fields: missing -> defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [ser_history(h) for h in (doc.get("history") or [])],
    }


def ser_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def dec_to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return float(value)


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # created_at or anything else from a client is ignored

    emp_code: StrictStr = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: StrictStr = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: StrictStr

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError on e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # derived fields sent by a client are ignored

    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: PresenceLiteral = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # work_hours etc. in a body are ignored

    status: Optional[StatusLiteral] = None
    punch_in: Optional[EpochMillis] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[EpochMillis] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # The contract types these fields as non-nullable: "omitted" means unchanged, null is invalid.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} may be omitted but not null")
        return self


# --------------------------------------------------------------------------- #
# 5. Startup: indexes (idempotent - create_index is a no-op when it already exists)
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        ([("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "department_emp_code"}),
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "department_joined_on"}),
        ([("joined_on", ASCENDING)], {"name": "joined_on"}),
    ],
    "attendance_logs": [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_code_date_unique"}),
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp_code"}),
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp_code"}),
    ],
}


def ensure_indexes() -> None:
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            try:
                db[coll].create_index(keys, **opts)
            except OperationFailure as exc:  # never block startup on one index
                print(f"[startup] could not create index {opts.get('name')} on {coll}: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # DB down at boot: /health reports 503 until it is up
        print(f"[startup] index creation skipped, MongoDB unavailable: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [ser_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp, att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): concurrent duplicates -> exactly one wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    # date order == punch_in order because date is derived from punch_in (R1); served by emp_code_date_unique.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("date", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    p_in = as_utc(rec["punch_in"])
    if ts <= p_in:
        raise unprocessable("punched_at must be after punch_in", ["body", "punched_at"])
    if ts - p_in > MAX_SHIFT:
        raise unprocessable("punched_at is more than 24 hours after punch_in", ["body", "punched_at"])

    att_date = date.fromisoformat(rec["date"])
    derived = derive_fields(rec["status"], p_in, ts, emp, att_date)
    updated = db.attendance_logs.find_one_and_update(
        # punch_out: None in the filter makes this a compare-and-set: only one concurrent request matches
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return ser_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusLiteral] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort = build_attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q, {"_id": 0}).sort(sort).skip((page - 1) * page_size).limit(page_size)
    return {"items": [ser_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def build_attendance_list_query(emp_code, date_from, date_to, status):
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            parse_date(date_from, ["query", "date_from"])
            q["date"]["$gte"] = date_from
        if date_to is not None:
            parse_date(date_to, ["query", "date_to"])
            q["date"]["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise unprocessable("date_from must not be after date_to", ["query", "date_from"])
    if status is not None:
        q["status"] = status
    return q, [("date", DESCENDING), ("emp_code", ASCENDING)]


TRACKED_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


def current_values(rec: dict) -> dict:
    return {
        "status": rec.get("status"),
        "punch_in": as_utc(rec.get("punch_in")),
        "punch_out": as_utc(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, record_date: str = Path(..., alias="date")):
    att_date = parse_date(record_date, ["path", "date"])
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = current_values(rec)
    sent = body.model_fields_set
    new_status = body.status if "status" in sent else old["status"]

    if new_status in NON_PRESENCE:
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable(f"{new_status} clears punch times; do not send punch_in/punch_out", ["body"])
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else old["punch_out"]
        if new_in is None:
            raise unprocessable("a presence status requires punch_in", ["body", "punch_in"])
        if "punch_in" in sent and attendance_date(new_in, emp) != att_date:
            raise unprocessable("punch_in must fall on the record's attendance date (R1)", ["body", "punch_in"])
        if new_out is not None:
            if new_out <= new_in:
                raise unprocessable("punch_out must be after punch_in", ["body", "punch_out"])
            if new_out - new_in > MAX_SHIFT:
                raise unprocessable("punch_out must be within 24 hours of punch_in", ["body", "punch_out"])

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, emp, att_date)}
    changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED_FIELDS if old[k] != new[k]}
    if not changes:
        raise unprocessable("the request changes nothing", ["body"])

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record still looks exactly as we read it
    # (same history length and same punches/status). A concurrent editor gets 409 instead of losing an entry.
    history_len = len(rec.get("history") or [])
    guard = {"_id": rec["_id"], "status": rec.get("status"),
             "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    if history_len == 0:
        guard["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}, {"history": None}]
    else:
        guard["history"] = {"$size": history_len}

    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": {k: new[k] for k in TRACKED_FIELDS}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return ser_record(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics pipeline builders (used by the endpoints AND by /admin/explain)
# --------------------------------------------------------------------------- #
def is_weekday_expr(date_field: str) -> dict:
    """True when a "YYYY-MM-DD" string falls on Mon-Fri ($isoDayOfWeek: 1 = Monday ... 7 = Sunday)."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}, 5]}


def present_value_expr(prefix: str = "$") -> dict:
    """R5/R6: a presence record counts 1, or 0.5 when half_day; non-presence counts 0."""
    return {"$cond": [
        {"$in": [f"{prefix}status", PRESENCE]},
        {"$cond": [{"$eq": [{"$ifNull": [f"{prefix}half_day", False]}, True]}, 0.5, 1]},
        0,
    ]}


def weekday_present_expr(prefix: str = "$") -> dict:
    """R7: present_days only counts Mon-Fri records."""
    return {"$cond": [is_weekday_expr(f"{prefix}date"), present_value_expr(prefix), 0]}


def late_flag_expr(prefix: str = "$") -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [f"{prefix}late_minutes", 0]}, 0]}, 1, 0]}


def round_half_up_dec(expr: Any, places: int) -> dict:
    """Half-up rounding in Decimal128 (MongoDB's $round is half-to-even, and doubles are inexact)."""
    scale = Decimal128(str(10 ** places))
    return {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}},
        scale,
    ]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},  # emp_code_date_unique
        {"$group": {
            "_id": None,
            "present_days": {"$sum": weekday_present_expr()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": late_flag_expr()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}  # R9: joined by the end of the month
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}
    return [
        # Start from employees, not logs, so people with zero logs are still counted.
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",  # served by emp_code_date_unique
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": weekday_present_expr()},
                    # work_hours summed as integer hundredths so the mean can be rounded exactly in Python
                    "wh_cents": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "wh_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": late_flag_expr()},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "wh_cents": {"$sum": {"$ifNull": ["$s.wh_cents", 0]}},
            "wh_count": {"$sum": {"$ifNull": ["$s.wh_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},  # date_desc_emp_code
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp",
            "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
        }},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # filter BEFORE ranking
    pipeline += [
        # Standard competition ranking (1, 2, 2, 4): $rank, sorted on the score only so ties share a rank.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied after ranking: ties at the cutoff are kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


def department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    lower = datetime.combine(d_from, time(0), UTC)
    upper = datetime.combine(d_to + timedelta(days=1), time(0), UTC)  # $densify bounds: upper is exclusive
    joined_in_range = {"$lte": ["$joined_on", t]}
    return [
        {"$match": {"department": department}},  # department_joined_on
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        # One "join" event per employee (on max(joined_on, from), so earlier joiners are the opening headcount)
        # plus one event per log. Employees joining after `to` add a zero event so the series is never empty.
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"day": {"$cond": [joined_in_range, {"$max": ["$joined_on", f]}, f]},
              "joins": {"$cond": [joined_in_range, 1, 0]}, "present": 0, "late": 0}],
            {"$map": {"input": "$logs", "as": "l", "in": {
                "day": "$$l.date", "joins": 0,
                "present": present_value_expr("$$l."), "late": late_flag_expr("$$l."),
            }}},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.day", "joins": {"$sum": "$events.joins"},
                    "present": {"$sum": "$events.present"}, "late": {"$sum": "$events.late"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # Gap filling: exactly one document per calendar day in [from, to].
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": {"$ifNull": ["$joins", 0]}, "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$set": {
            "present": {"$ifNull": ["$present", 0]},
            "late": {"$ifNull": ["$late", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_dec({"$divide": [{"$toDecimal": "$present"}, {"$toDecimal": "$headcount"}]}, 4),
            None,
        ]}}},
        # Window = current row + up to 6 preceding rows; only rows inside [from, to] exist, so the first
        # rows have a shorter window. $avg ignores nulls and returns null when every value is null.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "ma": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$set": {"moving_avg_7d": {"$cond": [{"$eq": [{"$ifNull": ["$ma", None]}, None]}, None,
                                              round_half_up_dec("$ma", 4)]}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": 1, "headcount": 1, "present_count": "$present", "late_count": "$late",
            "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


def working_days(month: str, joined_on: Optional[str]) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for mid-month joiners (calendar arithmetic, no documents)."""
    first, last = (date.fromisoformat(d) for d in month_bounds(month))
    start = max(first, date.fromisoformat(joined_on)) if joined_on else first
    if start > last:
        return 0
    days = (last - start).days + 1
    full_weeks, rest = divmod(days, 7)
    return full_weeks * 5 + sum(1 for i in range(rest) if (start.weekday() + i) % 7 < 5)


def number(value: Any) -> Any:
    """Present counts are multiples of 0.5; return 3 rather than 3.0 when whole."""
    value = dec_to_float(value) or 0.0
    return int(value) if float(value).is_integer() else value


def validate_trend_range(from_: str, to: str) -> tuple[date, date]:
    d_from = parse_date(from_, ["query", "from"])
    d_to = parse_date(to, ["query", "to"])
    if d_to < d_from:
        raise unprocessable("to must not be before from", ["query", "to"])
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"the range may cover at most {MAX_TREND_DAYS} days", ["query", "to"])
    return d_from, d_to


# --------------------------------------------------------------------------- #
# 8. Endpoints: analytics, admin
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    wd = working_days(month, emp.get("joined_on"))
    present = Decimal(str(dec_to_float(agg.get("present_days")) or 0))
    pct = float(half_up(present / Decimal(wd) * 100, 2)) if wd > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": number(agg.get("present_days")),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        avg = (float(half_up(Decimal(r["wh_cents"]) / Decimal(r["wh_count"]) / 100, 2))
               if r["wh_count"] else None)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": number(r["present_days"]),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    d_from, d_to = validate_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    items = []
    for r in db.employees.aggregate(department_trend_pipeline(department, d_from, d_to)):
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": number(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": dec_to_float(r.get("attendance_rate")),
            "moving_avg_7d": dec_to_float(r.get("moving_avg_7d")),
        })
    return {"department": department, "items": items}


ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def require(value: Any, name: str) -> Any:
    if value is None:
        raise unprocessable(f"{name} is required for this endpoint", ["query", name])
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusLiteral] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q, sort = build_attendance_list_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        cmd = {"find": collection, "filter": q, "projection": {"_id": 0}, "sort": dict(sort),
               "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            pipeline = employee_monthly_pipeline(require(emp_code, "emp_code"), require(month, "month"))
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = department_summary_pipeline(require(month, "month"), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(require(month, "month"), limit, department)
        else:
            collection = "employees"
            d_from, d_to = validate_trend_range(require(from_, "from"), require(to, "to"))
            pipeline = department_trend_pipeline(require(department, "department"), d_from, d_to)
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command("explain", cmd, verbosity="executionStats")
    plain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": plain}
