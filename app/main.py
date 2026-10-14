"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real
      environment variables always take priority)

Layout of this file
    1. Configuration, constants and database handle
    2. Index definitions (created idempotently at startup)
    3. Pure helpers: time, shifts, derived fields (R1-R5), rounding (R8)
    4. Request models (all validation lives here, so bad input is a 422)
    5. Response serialisers (BSON -> API shapes; no _id ever leaves the API)
    6. Pipeline builders (shared by the endpoints AND /admin/explain, so the
       explained query is by construction the query the endpoint runs)
    7. Endpoints

Design notes
    * MongoDB stores instants as BSON datetimes (UTC) and calendar values as
      strings. The API speaks epoch milliseconds for instants and IST strings
      for calendar values. Conversion happens only at the boundary.
    * Every uniqueness / "exactly one wins" guarantee is enforced by the
      database (unique index, conditional update), never by check-then-write.
    * Analytics are single aggregation pipelines; Python only rounds numbers.
"""

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and constants
# --------------------------------------------------------------------------- #

load_dotenv()  # does not override variables that are already set

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
    tz_aware=True,  # PyMongo returns aware UTC datetimes, never naive ones
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Anything below this looks like epoch *seconds*; anything above is out of range.
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

LATE_GRACE_SECONDS = 600           # R2: strictly more than 10:00 is late
OVERTIME_THRESHOLD_MINUTES = 30    # R3
HALF_DAY_BELOW_HOURS = Decimal("4.50")  # R5
MAX_PUNCH_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")      # R6
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")

Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

EpochMs = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
MonthParam = Annotated[
    str, Query(pattern=MONTH_PATTERN, description="Calendar month, YYYY-MM")
]


# --------------------------------------------------------------------------- #
# 2. Indexes (idempotent; called at startup)
# --------------------------------------------------------------------------- #

def ensure_indexes() -> None:
    # employees
    db.employees.create_index(
        [("emp_code", 1)], unique=True, name="uq_employee_emp_code"
    )
    # GET /employees?department=X sorted by emp_code
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)], name="ix_employee_department_code"
    )
    # headcount: department + joined_on <= day (summary, trend)
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)], name="ix_employee_department_joined"
    )
    # headcount across all departments: joined_on <= end of month
    db.employees.create_index([("joined_on", 1)], name="ix_employee_joined_on")

    # attendance_logs
    # one record per employee per attendance date; makes punch-in race-safe and
    # serves per-employee monthly analytics and the $lookup joins.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date"
    )
    # default listing order and month-wide scans (summary, leaderboard)
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="ix_attendance_date_employee"
    )
    # status filter + the listing order
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="ix_attendance_status_date_employee",
    )
    # punch-out: latest record of an employee with punch_in <= instant
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)], name="ix_attendance_employee_punch_in"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, __: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


# --------------------------------------------------------------------------- #
# 3. Pure helpers
# --------------------------------------------------------------------------- #

def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def round_half_up(value: Any, places: int) -> float:
    """Decimal half-up rounding (R8). Never uses float or banker's rounding."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def as_ist(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored datetime to an aware, whole-second IST instant."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST).replace(microsecond=0)


def ist_from_epoch_ms(ms: Optional[int]) -> datetime:
    """Epoch ms -> whole-second IST instant (R1). None means 'now'."""
    if ms is None:
        return datetime.now(UTC).astimezone(IST).replace(microsecond=0)
    if ms < MIN_EPOCH_MS or ms > MAX_EPOCH_MS:
        raise unprocessable("Timestamp must be epoch milliseconds")
    # Integer division truncates sub-second precision without float error.
    return datetime.fromtimestamp(ms // 1000, tz=UTC).astimezone(IST)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def parse_date(value: str, field: str) -> Date:
    """Strict YYYY-MM-DD: rejects 2026-3-1, 20260301 and 2026-02-30."""
    if not DATE_RE.fullmatch(value):
        raise unprocessable(f"{field} must use YYYY-MM-DD format")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{field} must be a valid calendar date")


def month_bounds(month: str) -> tuple[Date, Date]:
    """'2026-03' -> (2026-03-01, 2026-03-31)."""
    year, mon = int(month[:4]), int(month[5:7])
    try:  # year 0000 and the month after 9999-12 are not representable
        first = Date(year, mon, 1)
        nxt = Date(year + 1, 1, 1) if mon == 12 else Date(year, mon + 1, 1)
    except ValueError:
        raise unprocessable("month is out of range")
    return first, nxt - timedelta(days=1)


def parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def shift_of(emp: dict) -> tuple[str, str]:
    return emp.get("shift_start") or "09:30", emp.get("shift_end") or "18:30"


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in, shifted back a day for the early
    part of an overnight shift (e.g. 02:00 on a 22:00-06:00 shift)."""
    local = punch_in.astimezone(IST)
    start, end = parse_hhmm(shift_start), parse_hhmm(shift_end)
    day = local.date()
    if end <= start and local.time() < end:
        day -= timedelta(days=1)
    return day


def shift_window(day: Date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that belongs to attendance date `day`."""
    start, end = parse_hhmm(shift_start), parse_hhmm(shift_end)
    end_day = day + timedelta(days=1) if end <= start else day
    return (
        datetime.combine(day, start, tzinfo=IST),
        datetime.combine(end_day, end, tzinfo=IST),
    )


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; the value is
    whole minutes since shift start (not since the end of the grace period)."""
    day = attendance_date(punch_in, shift_start, shift_end)
    start_dt, _ = shift_window(day, shift_start, shift_end)
    elapsed = int((punch_in - start_dt).total_seconds())
    return elapsed // 60 if elapsed > LATE_GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def compute_overtime_minutes(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes after shift end, counted only if >= 30."""
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_THRESHOLD_MINUTES else 0


def compute_derived(
    emp: dict, day: Date, punch_in: Optional[datetime], punch_out: Optional[datetime]
) -> dict:
    """All four derived fields from the final punch times (R2-R5)."""
    if punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    shift_start, shift_end = shift_of(emp)
    derived = {
        "late_minutes": compute_late_minutes(punch_in, shift_start, shift_end),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        _, end_dt = shift_window(day, shift_start, shift_end)
        derived["work_hours"] = compute_work_hours(punch_in, punch_out)
        derived["overtime_minutes"] = compute_overtime_minutes(punch_out, end_dt)
        derived["half_day"] = Decimal(str(derived["work_hours"])) < HALF_DAY_BELOW_HOURS
    return derived


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_PATTERN)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("name", "department")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        if not DATE_RE.fullmatch(value):
            raise ValueError("must use YYYY-MM-DD format")
        Date.fromisoformat(value)  # ValueError -> 422
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    """Derived fields are not declared, so a client that sends them is ignored."""

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None

    @field_validator("reason")
    @classmethod
    def reason_has_content(cls, value: str) -> str:
        if len(value.strip()) < 5:
            raise ValueError("reason must be at least 5 characters")
        return value

    @field_validator("regularized_by")
    @classmethod
    def by_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("regularized_by must not be blank")
        return value

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        # The contract types these as non-nullable: omit a field to keep it.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null; omit it to leave it unchanged")
        return self


# --------------------------------------------------------------------------- #
# 5. Response serialisers (whitelist fields: _id can never leak)
# --------------------------------------------------------------------------- #

def employee_response(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def history_response(entry: dict) -> dict:
    changes = {}
    for field, delta in (entry.get("changes") or {}).items():
        delta = dict(delta)
        if field in ("punch_in", "punch_out"):
            for side in ("from", "to"):
                if side in delta:
                    delta[side] = to_epoch_ms(delta[side])
        changes[field] = delta
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def attendance_response(doc: dict) -> dict:
    """Seeded documents may omit derived fields and history; default them."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_response(h) for h in (doc.get("history") or [])],
    }


def to_jsonable(value: Any) -> Any:
    """Explain output contains BSON types (Timestamp, ObjectId, dates)."""
    return json.loads(
        json_util.dumps(value, json_options=json_util.RELAXED_JSON_OPTIONS)
    )


# --------------------------------------------------------------------------- #
# 6. Query / pipeline builders (shared by endpoints and /admin/explain)
# --------------------------------------------------------------------------- #

def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> tuple[dict, dict]:
    """Returns (filter, sort) for GET /attendance."""
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code

    lower = parse_date(date_from, "date_from") if date_from is not None else None
    upper = parse_date(date_to, "date_to") if date_to is not None else None
    if lower and upper and lower > upper:
        raise unprocessable("date_from cannot be later than date_to")
    if lower or upper:
        query["date"] = {}
        if lower:
            query["date"]["$gte"] = date_from
        if upper:
            query["date"]["$lte"] = date_to

    if status:
        query["status"] = status

    sort = {"date": -1, "emp_code": 1}
    return query, sort


# --- reusable aggregation expressions -------------------------------------- #
# `prefix` is the path of the log sub-document: "" for logs read directly,
# "logs." / "_logs." when the log was joined onto an employee row.

# Mon-Fri flag from the 'YYYY-MM-DD' string. $dayOfWeek: 1=Sun ... 7=Sat.
WEEKDAY_EXPR = {
    "$in": [
        {
            "$dayOfWeek": {
                "$dateFromString": {
                    "dateString": "$date",
                    "format": "%Y-%m-%d",
                    "onError": None,
                    "onNull": None,
                }
            }
        },
        [2, 3, 4, 5, 6],
    ]
}


def _ref(prefix: str, name: str) -> str:
    return f"${prefix}{name}"


def _status(prefix: str) -> dict:
    return {"$ifNull": [_ref(prefix, "status"), ""]}


def status_is(prefix: str, status: str) -> dict:
    return {"$cond": [{"$eq": [_status(prefix), status]}, 1, 0]}


def late_flag(prefix: str) -> dict:
    late = {"$ifNull": [_ref(prefix, "late_minutes"), 0]}
    return {"$cond": [{"$gt": [late, 0]}, 1, 0]}


def present_credit(prefix: str, weekdays_only: bool) -> dict:
    """1 for a present-status record, 0.5 for a half day, 0 otherwise (R5-R7)."""
    conditions: list = [{"$in": [_status(prefix), list(PRESENCE_STATUSES)]}]
    if weekdays_only:
        conditions.append({"$eq": [_ref(prefix, "weekday"), True]})
    return {
        "$cond": [
            {"$and": conditions},
            {"$cond": [{"$eq": [_ref(prefix, "half_day"), True]}, 0.5, 1]},
            0,
        ]
    }


def work_hours_value(prefix: str) -> dict:
    """work_hours of present-status records that have one; null otherwise ($avg skips null)."""
    hours = _ref(prefix, "work_hours")
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": [_status(prefix), list(PRESENCE_STATUSES)]},
                    {"$isNumber": hours},
                ]
            },
            hours,
            None,
        ]
    }


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$set": {"weekday": WEEKDAY_EXPR}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_credit("", True)},
                "leave_days": {"$sum": status_is("", "LEAVE")},
                "late_count": {"$sum": late_flag("")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Runs on `employees` so that people with zero logs still count (R9),
    then joins that employee's month of logs through the (emp_code, date) index."""
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$set": {"weekday": WEEKDAY_EXPR}},
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "weekday": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        # keep employees without logs: they still count towards headcount
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": present_credit("logs.", True)},
                "avg_work_hours": {"$avg": work_hours_value("logs.")},
                "late_count": {"$sum": late_flag("logs.")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": status_is("logs.", "LEAVE")},
                "on_duty_count": {"$sum": status_is("logs.", "ON_DUTY")},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    first: str, last: str, limit: int, department: Optional[str]
) -> list:
    """Group the month's logs per employee first (cheap), then join employees
    and rank. $rank is standard competition ranking: 1, 2, 2, 4."""
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": late_flag("")},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        # filter BEFORE ranking: ranks are computed inside the department
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        # limit applies AFTER ranking, so ties at the cutoff are all kept
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def parse_trend_range(date_from: str, date_to: str) -> tuple[Date, Date]:
    start = parse_date(date_from, "from")
    end = parse_date(date_to, "to")
    if end < start:
        raise unprocessable("to cannot be earlier than from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"Date range cannot exceed {MAX_TREND_DAYS} days")
    return start, end


def department_trend_pipeline(department: str, start: Date, end: Date) -> list:
    """One row per calendar day, built entirely inside MongoDB.

    1. Calendar: one anchor employee document is densified into every day of
       the range (weekends and empty days included) and each day gets its
       headcount (employees with joined_on <= that day).
    2. Activity: the department's employees are joined to their logs for the
       range and grouped by date. $unionWith appends these per-day counts.
    3. Rows are merged per date, the rate is derived and a 7-row window
       averages it (shorter at the start of the range).

    Note: $densify-generated documents carry only the densified field, so
    headcount is computed with a $lookup rather than read from the anchor.
    """
    first, last = start.isoformat(), end.isoformat()
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)  # exclusive

    return [
        # ---- calendar rows with headcount ---------------------------------
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "_date": {"$literal": start_dt}}},
        {
            "$densify": {
                "field": "_date",
                "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
            }
        },
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_date"}}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.n", 0]}, 0]},
            }
        },
        # ---- per-day activity of the department's employees ---------------
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [
                                {"$match": {"date": {"$gte": first, "$lte": last}}},
                                {
                                    "$project": {
                                        "_id": 0,
                                        "date": 1,
                                        "status": 1,
                                        "half_day": 1,
                                        "late_minutes": 1,
                                    }
                                },
                            ],
                            "as": "_logs",
                        }
                    },
                    {"$unwind": "$_logs"},
                    {
                        "$group": {
                            "_id": "$_logs.date",
                            "present_count": {"$sum": present_credit("_logs.", False)},
                            "late_count": {"$sum": late_flag("_logs.")},
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": "$_id",
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                ],
            }
        },
        # ---- merge calendar + activity, derive rate and moving average ----
        {
            "$group": {
                "_id": "$date",
                "headcount": {"$sum": {"$ifNull": ["$headcount", 0]}},
                "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
            }
        },
        {
            "$set": {
                "date": "$_id",
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$_id",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    # $avg ignores nulls; null when the whole window is null
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
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
        {"$sort": {"date": 1}},
    ]


def run_explain(command: dict) -> dict:
    return to_jsonable(db.command("explain", command, verbosity="executionStats"))


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------ #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)  # unique index decides concurrent creates
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query)
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_response(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---- attendance ----------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    punched_at = ist_from_epoch_ms(body.punched_at)
    shift_start, shift_end = shift_of(emp)
    day = attendance_date(punched_at, shift_start, shift_end)

    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **compute_derived(emp, day, punched_at, None),
        "history": [],
    }
    try:
        # The unique (emp_code, date) index makes this race-safe: of N
        # simultaneous requests exactly one insert succeeds.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="Already punched in for this attendance date"
        )
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    punched_at = ist_from_epoch_ms(body.punched_at)

    # Most recent record whose punch_in is at or before the punch-out instant.
    # Because it is not restricted to open records, a record that is already
    # closed is reported as a conflict instead of silently closing an older one.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409, detail="Attendance record has already been punched out"
        )

    punch_in_at = as_ist(record["punch_in"])
    if punched_at <= punch_in_at:
        raise unprocessable("Punch-out must be after punch-in")
    if punched_at - punch_in_at > MAX_PUNCH_SPAN:
        raise unprocessable("Punch-out cannot be more than 24 hours after punch-in")

    derived = compute_derived(
        emp, Date.fromisoformat(record["date"]), punch_in_at, punched_at
    )
    changes = {
        "punch_out": punched_at,
        "work_hours": derived["work_hours"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }
    # Conditional update: of two simultaneous punch-outs only one matches.
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None}, {"$set": changes}
    )
    if result.matched_count != 1:
        raise HTTPException(
            status_code=409, detail="Attendance record has already been punched out"
        )
    record.update(changes)
    return attendance_response(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query, sort = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort(list(sort.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date")
    emp = get_employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    sent = body.model_fields_set  # omitted = unchanged
    shift_start, shift_end = shift_of(emp)

    old_status = record.get("status", "PRESENT")
    old_in = as_ist(record.get("punch_in"))
    old_out = as_ist(record.get("punch_out"))

    new_status = body.status if "status" in sent else old_status

    if new_status in NON_PRESENCE_STATUSES:
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable("ABSENT and LEAVE cannot be supplied with punch times")
        new_in = new_out = None  # ABSENT / LEAVE clear both punch times
    else:
        new_in = ist_from_epoch_ms(body.punch_in) if "punch_in" in sent else old_in
        new_out = ist_from_epoch_ms(body.punch_out) if "punch_out" in sent else old_out
        if new_in is None:
            raise unprocessable("Presence statuses require a punch_in")
        if attendance_date(new_in, shift_start, shift_end) != day:
            raise unprocessable("punch_in must belong to the attendance date")

    if new_out is not None:
        if new_out <= new_in:
            raise unprocessable("punch_out must be after punch_in")
        if new_out - new_in > MAX_PUNCH_SPAN:
            raise unprocessable("punch_out cannot be more than 24 hours after punch_in")

    # ---- what actually changed (inputs first, then recomputed fields) ------
    changes: dict = {}

    def note(field: str, before: Any, after: Any) -> None:
        if before != after:
            changes[field] = {"from": before, "to": after}

    note("status", old_status, new_status)
    note("punch_in", old_in, new_in)
    note("punch_out", old_out, new_out)
    if not changes:
        raise unprocessable("The regularization does not change the record")

    derived = compute_derived(emp, day, new_in, new_out)
    note("work_hours", record.get("work_hours"), derived["work_hours"])
    note("late_minutes", record.get("late_minutes") or 0, derived["late_minutes"])
    note(
        "overtime_minutes",
        record.get("overtime_minutes") or 0,
        derived["overtime_minutes"],
    )
    note("half_day", bool(record.get("half_day", False)), derived["half_day"])

    entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by.strip(),
        "reason": body.reason.strip(),
        "changes": changes,
    }
    updates = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }

    # Optimistic concurrency: the update only applies if nothing relevant moved
    # since we read the record. The history length makes sure two concurrent
    # corrections can never overwrite each other's audit entry; the loser gets
    # 409 and can retry. Punch fields are included so a concurrent punch-out
    # (which does not touch history) is detected as well.
    history = record.get("history")
    guard = {
        "_id": record["_id"],
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if isinstance(history, list):
        guard["history"] = {"$size": len(history)}
        update = {"$set": updates, "$push": {"history": entry}}
    else:  # seeded document without a usable history: start the audit trail
        guard["history"] = None  # matches missing and null
        history = []
        update = {"$set": {**updates, "history": [entry]}}
    result = db.attendance_logs.update_one(guard, update)
    if result.matched_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request",
        )

    record.update(updates)
    record["history"] = history + [entry]
    return attendance_response(record)


# ---- analytics ------------------------------------------------------------ #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)

    # R7: weekdays from the later of month start and joined_on
    counted_from = max(first, Date.fromisoformat(emp["joined_on"]))
    working_days = sum(
        1
        for offset in range((last - counted_from).days + 1)
        if (counted_from + timedelta(days=offset)).weekday() < 5
    )

    rows = list(
        db.attendance_logs.aggregate(
            employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())
        )
    )
    stats = rows[0] if rows else {}
    present_days = round_half_up(stats.get("present_days", 0), 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": (
            round_half_up(Decimal(str(present_days)) / working_days * 100, 2)
            if working_days
            else None
        ),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.employees.aggregate(
        department_summary_pipeline(first.isoformat(), last.isoformat(), department)
    )
    items = [
        {
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": (
                round_half_up(row["avg_work_hours"], 2)
                if row.get("avg_work_hours") is not None
                else None
            ),
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthParam,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(
        late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
    )
    return {
        "month": month,
        "items": [
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
            for row in rows
        ],
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    start, end = parse_trend_range(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Department not found")

    items = []
    for row in db.employees.aggregate(department_trend_pipeline(department, start, end)):
        rate, moving = row.get("attendance_rate"), row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": round_half_up(row["present_count"], 2),
                "late_count": int(row["late_count"]),
                "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
                "moving_avg_7d": round_half_up(moving, 4) if moving is not None else None,
            }
        )
    return {"department": department, "items": items}


# ---- admin ---------------------------------------------------------------- #

ExplainTarget = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


def _require(**params: Any) -> None:
    missing = [name for name, value in params.items() if value in (None, "")]
    if missing:
        raise unprocessable(f"Missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Explains the main query of an endpoint, built by the same builders the
    endpoint itself uses."""
    if endpoint == "attendance_list":
        query, sort = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": query,
            "sort": sort,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": employee_monthly_pipeline(
                emp_code, first.isoformat(), last.isoformat()
            ),
            "cursor": {},
        }

    elif endpoint == "department_summary":
        _require(month=month)
        first, last = month_bounds(month)
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": department_summary_pipeline(
                first.isoformat(), last.isoformat(), department
            ),
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": late_leaderboard_pipeline(
                first.isoformat(), last.isoformat(), limit, department
            ),
            "cursor": {},
        }

    else:  # department_trend
        _require(department=department, **{"from": trend_from, "to": trend_to})
        start, end = parse_trend_range(trend_from, trend_to)
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": department_trend_pipeline(department, start, end),
            "cursor": {},
        }

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": run_explain(command),
    }