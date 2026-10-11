"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.mai
n:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

from datetime import datetime, timedelta, timezone
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, StrictInt
from decimal import Decimal, ROUND_HALF_UP
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employee_department_joined",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1)],
        name="ix_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1)],
        name="ix_attendance_status_date",
    )


@app.on_event("startup")
def startup():
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_instant(value: Optional[int]) -> datetime:
    """Parse epoch milliseconds and return a whole-second IST instant."""
    if value is None:
        return datetime.now(timezone.utc).astimezone(IST).replace(
            microsecond=0
        )

    if value < 100_000_000_000:
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be epoch milliseconds",
        )

    try:
        return datetime.fromtimestamp(
            value / 1000, tz=timezone.utc
        ).astimezone(IST).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(status_code=422, detail="Invalid timestamp")


def attendance_date(punch_in: datetime, shift_start: str,
                    shift_end: str) -> str:
    """Determine the attendance date, including overnight shifts."""
    local_time = punch_in.astimezone(IST)
    start = datetime.strptime(shift_start, "%H:%M").time()
    end = datetime.strptime(shift_end, "%H:%M").time()

    day = local_time.date()
    if end <= start and local_time.time() < end:
        day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(
    punch_in: datetime, shift_start: str, shift_end: str
) -> int:
    local = punch_in.astimezone(IST)
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    start_day = local.date()
    if end_time <= start_time and local.time() < end_time:
        start_day -= timedelta(days=1)

    shift_start_dt = datetime.combine(
        start_day, start_time, tzinfo=IST
    )
    elapsed = int((local - shift_start_dt).total_seconds() // 60)

    return elapsed if (local - shift_start_dt).total_seconds() > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )
    hours = (seconds / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str,
    shift_start: str
) -> int:
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()
    day = datetime.strptime(date_str, "%Y-%m-%d").date()

    end_day = day + timedelta(days=1) if end_time <= start_time else day
    shift_end_dt = datetime.combine(end_day, end_time, tzinfo=IST)

    minutes = int(
        (punch_out.astimezone(IST) - shift_end_dt).total_seconds() // 60
    )
    return minutes if minutes >= 30 else 0



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

def to_epoch_ms(value):
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    # Instants are stored and returned at whole-second precision.
    return int(value.timestamp()) * 1000


def attendance_response(doc):
    doc.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        doc[field] = to_epoch_ms(doc.get(field))

    # Support legacy records without a history field
    doc.setdefault("history", [])

    for entry in doc["history"]:
        if "at" in entry:
            entry["at"] = to_epoch_ms(entry["at"])

        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                for side in ("from", "to"):
                    if side in changes[field]:
                        changes[field][side] = to_epoch_ms(
                            changes[field][side]
                        )

    return doc

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

class AttendanceRegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="Status must be PRESENT, WFH, or ON_DUTY",
        )

    ts = parse_instant(body.punched_at)

    date_str = attendance_date(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this attendance date",
        )

    doc["id"] = str(result.inserted_id)
    return attendance_response(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    # Filter by employee code
    if emp_code:
        query["emp_code"] = emp_code

    # Validate and filter by attendance date
    for field_name, date_value in (
        ("date_from", date_from),
        ("date_to", date_to),
    ):
        if date_value is not None:
            try:
                parsed_date = datetime.strptime(date_value, "%Y-%m-%d")
                if parsed_date.date().isoformat() != date_value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field_name} must use YYYY-MM-DD format",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be later than date_to",
        )

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    # Validate and filter by status
    if status:
        allowed_statuses = {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }
        if status not in allowed_statuses:
            raise HTTPException(
                status_code=422,
                detail="Invalid status",
            )
        query["status"] = status

    # Count only records matching the supplied filters
    total = db.attendance_logs.count_documents(query)

    # Filter, sort, and paginate in MongoDB
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []
    for doc in cursor:
        doc["id"] = str(doc["_id"])
        items.append(attendance_response(doc))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = parse_instant(body.punched_at)

    # Find the latest open punch-in for this employee.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404, detail="No open punch-in found"
        )

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    punch_in_time = punch_in_time.astimezone(IST).replace(microsecond=0)

    if punched_at <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if punched_at > punch_in_time + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, punched_at)
    overtime_minutes = compute_overtime(
        punched_at,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )
    half_day = work_hours < 4.50

    # Conditional update prevents two concurrent requests from both
    # successfully closing the same attendance record.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record has already been punched out",
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    if updated is None:
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve updated attendance record"
        )

    updated["id"] = str(updated["_id"])
    return attendance_response(updated)

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: AttendanceRegularizeIn,
):
    # Validate the attendance date
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d")
        if parsed_date.date().isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format",
        )

    if not body.reason.strip() or not body.regularized_by.strip():
        raise HTTPException(
            status_code=422,
            detail="reason and regularized_by are required",
        )

    # Find employee and attendance record
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    fields = body.model_fields_set
    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }

    if "status" in fields:
        if body.status not in allowed_statuses:
            raise HTTPException(
                status_code=422,
                detail="Invalid attendance status",
            )
        new_status = body.status
    else:
        new_status = record.get("status", "PRESENT")

    # ABSENT and LEAVE must not be supplied with punch timestamps
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in fields or "punch_out" in fields:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot be supplied with punch timestamps",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Omitted fields keep their existing values.
        # Explicit null clears the corresponding timestamp.
        if "punch_in" in fields:
            new_punch_in = (
                parse_instant(body.punch_in)
                if body.punch_in is not None
                else None
            )
        else:
            new_punch_in = record.get("punch_in")

        if "punch_out" in fields:
            new_punch_out = (
                parse_instant(body.punch_out)
                if body.punch_out is not None
                else None
            )
        else:
            new_punch_out = record.get("punch_out")

    # Normalize stored timestamps for comparison and calculations
    if new_punch_in is not None:
        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)
        new_punch_in = new_punch_in.astimezone(IST).replace(microsecond=0)

    if new_punch_out is not None:
        if new_punch_out.tzinfo is None:
            new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
        new_punch_out = new_punch_out.astimezone(IST).replace(microsecond=0)

    # A presence record must have a punch-in
    if new_status in {"PRESENT", "WFH", "ON_DUTY"}:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence statuses require punch_in",
            )

        calculated_date = attendance_date(
            new_punch_in,
            emp["shift_start"],
            emp["shift_end"],
        )
        if calculated_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

    # Validate punch-out
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires punch_in",
            )

        if new_punch_out <= new_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )

        if new_punch_out > new_punch_in + timedelta(hours=24):
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    # Record only actual changes in the audit history
    changes = {}

    if new_status != record.get("status"):
        changes["status"] = {
            "from": record.get("status"),
            "to": new_status,
        }

    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")

    if old_punch_in is not None:
        if old_punch_in.tzinfo is None:
            old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
        old_punch_in = old_punch_in.astimezone(IST).replace(microsecond=0)

    if old_punch_out is not None:
        if old_punch_out.tzinfo is None:
            old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
        old_punch_out = old_punch_out.astimezone(IST).replace(microsecond=0)

    if new_punch_in != old_punch_in:
        changes["punch_in"] = {
            "from": old_punch_in,
            "to": new_punch_in,
        }

    if new_punch_out != old_punch_out:
        changes["punch_out"] = {
            "from": old_punch_out,
            "to": new_punch_out,
        }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The regularization does not change the record",
        )

    # Recalculate derived attendance fields
    if new_punch_in is None:
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            new_punch_in,
            emp["shift_start"],
            emp["shift_end"],
        )

        if new_punch_out is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = compute_work_hours(
                new_punch_in,
                new_punch_out,
            )
            overtime_minutes = compute_overtime(
                new_punch_out,
                emp["shift_end"],
                date,
                emp["shift_start"],
            )
            half_day = work_hours < 4.50

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by.strip(),
        "reason": body.reason.strip(),
        "changes": changes,
    }

    # Optimistic concurrency check: avoid overwriting another edit
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record.get("status"),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    if updated is None:
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve updated attendance record",
        )

    updated["id"] = str(updated["_id"])
    return attendance_response(updated)
 
 
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    # 1. Verify employee exists
    emp = db.employees.find_one({"emp_code": emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # 2. Calculate the selected month's date range
    month_start = datetime(year, month, 1).date()

    if month == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, month + 1, 1).date()

    month_end = next_month - timedelta(days=1)

    # 3. Count weekdays from the later of joining date
    # and the beginning of the month
    joined_on = datetime.strptime(
        emp["joined_on"], "%Y-%m-%d"
    ).date()

    first_working_date = max(month_start, joined_on)
    working_days = 0

    current_date = first_working_date

    while current_date <= month_end:
        if current_date.weekday() < 5:
            working_days += 1

        current_date += timedelta(days=1)

    # 4. Aggregate attendance data inside MongoDB
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$set": {
                "_attendance_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$set": {
                "_day_of_week": {
                    "$dayOfWeek": "$_attendance_date"
                }
            }
        },
        {
            "$group": {
                "_id": None,

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$gte": ["$_day_of_week", 2]
                                    },
                                    {
                                        "$lte": ["$_day_of_week", 6]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    stats = results[0] if results else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
    }

    present_days = float(stats["present_days"])

    # 5. Calculate attendance percentage
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (
                Decimal(str(present_days))
                / Decimal(working_days)
                * Decimal("100")
            ).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

    return {
        "emp_code": emp_code,
        "name": emp["name"],
        "department": emp["department"],
        "year": year,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    department: Optional[str] = None,
):
    # Determine the selected month's date range
    month_start = datetime(year, month, 1).date()

    if month == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, month + 1, 1).date()

    month_end = next_month - timedelta(days=1)
    start_str = month_start.isoformat()
    end_str = month_end.isoformat()

    # Only count employees who joined by the end of the month
    employee_match = {"joined_on": {"$lte": end_str}}

    if department is not None:
        employee_match["department"] = department

    headcount_pipeline = [
        {"$match": employee_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    headcounts = list(db.employees.aggregate(headcount_pipeline))

    # Return no departments if no employees qualify
    if not headcounts:
        return {
            "year": year,
            "month": month,
            "items": [],
        }

    headcount_by_department = {
        row["_id"]: row["headcount"]
        for row in headcounts
    }

    # Aggregate monthly attendance records in MongoDB
    attendance_match = {
        "date": {"$gte": start_str, "$lte": end_str}
    }

    if department is not None:
        attendance_pipeline = [
            {"$match": attendance_match},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {
                "$match": {
                    "employee.department": department,
                    "employee.joined_on": {"$lte": end_str},
                }
            },
        ]
    else:
        attendance_pipeline = [
            {"$match": attendance_match},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {
                "$match": {
                    "employee.joined_on": {"$lte": end_str}
                }
            },
        ]

    attendance_pipeline += [
        {
            "$set": {
                "_attendance_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$set": {
                "_day_of_week": {
                    "$dayOfWeek": "$_attendance_date"
                }
            }
        },
        {
            "$group": {
                "_id": "$employee.department",

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$_day_of_week", 2]},
                                    {"$lte": ["$_day_of_week", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            None,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },

                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    attendance_rows = list(
        db.attendance_logs.aggregate(attendance_pipeline)
    )

    attendance_by_department = {
        row["_id"]: row
        for row in attendance_rows
    }

    # Combine headcount and attendance statistics
    items = []

    for department_name in sorted(headcount_by_department):
        stats = attendance_by_department.get(department_name, {})

        avg_hours = stats.get("avg_work_hours")

        if avg_hours is not None:
            avg_hours = float(
                Decimal(str(avg_hours)).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

        items.append({
            "department": department_name,
            "headcount": headcount_by_department[department_name],
            "present_days": float(stats.get("present_days", 0)),
            "avg_work_hours": avg_hours,
            "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get(
                "total_late_minutes", 0
            ),
            "leave_count": stats.get("leave_count", 0),
            "on_duty_count": stats.get("on_duty_count", 0),
        })

    return {
        "year": year,
        "month": month,
        "items": items,
    }

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    # Calculate the selected month's date range
    month_start = datetime(year, month, 1).date()

    if month == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, month + 1, 1).date()

    month_end = next_month - timedelta(days=1)

    # Filter attendance records before joining employee information
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # Optional exact department filter; apply before ranking
    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    pipeline += [
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        # Filter by rank, not document count, so ties are preserved
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "year": year,
        "month": month,
        "department": department,
        "limit": limit,
        "items": items,
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str,
    date_to: str,
):
    # Validate dates
    try:
        start_day = datetime.strptime(date_from, "%Y-%m-%d").date()
        end_day = datetime.strptime(date_to, "%Y-%m-%d").date()

        if start_day.isoformat() != date_from:
            raise ValueError
        if end_day.isoformat() != date_to:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if end_day < start_day:
        raise HTTPException(
            status_code=422,
            detail="date_to cannot be earlier than date_from",
        )

    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Unknown departments must return 404
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    start_dt = datetime.combine(
        start_day, datetime.min.time(), tzinfo=timezone.utc
    )
    end_dt = datetime.combine(
        end_day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    # Generate a calendar row for each employee and each requested date.
    # MongoDB handles the date expansion; Python does not loop over dates.
    pipeline = [
        {"$match": {"department": department}},
        {
            "$set": {
                "_date": start_dt,
            }
        },
        {
            "$densify": {
                "field": "_date",
                "partitionByFields": ["emp_code"],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "attendance_date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$_date",
                            "timezone": "UTC",
                        }
                    },
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {
                                        "$eq": [
                                            "$date",
                                            "$$attendance_date",
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "_day_log",
            }
        },
        {
            "$set": {
                "_log": {"$arrayElemAt": ["$_day_log", 0]},
                "_date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_date",
                        "timezone": "UTC",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_date_string",

                "headcount": {
                    "$sum": {
                        "$cond": [
                            {"$lte": ["$joined_on", "$_date_string"]},
                            1,
                            0,
                        ]
                    }
                },

                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$_date_string",
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$_log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$_log.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$_date_string",
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {"$ifNull": [
                                                "$_log.late_minutes", 0
                                            ]},
                                            0,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$set": {
                "date": "$_id",
                "is_working_day": {
                    "$and": [
                        {
                            "$gte": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$_id"
                                        }
                                    }
                                },
                                2,
                            ]
                        },
                        {
                            "$lte": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$_id"
                                        }
                                    }
                                },
                                6,
                            ]
                        },
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
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
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
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

    items = list(db.employees.aggregate(pipeline))

    # Use decimal half-up rounding for reported rates
    for item in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            value = item.get(field)
            if value is not None:
                item[field] = float(
                    Decimal(str(value)).quantize(
                        Decimal("0.0001"),
                        rounding=ROUND_HALF_UP,
                    )
                )

    return {
        "department": department,
        "date_from": date_from,
        "date_to": date_to,
        "items": items,
    }

def run_explain(collection_name, pipeline):
    return db.command(
        "explain",
        {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        },
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    year: Optional[int] = Query(None, ge=2000, le=2100),
    month: Optional[int] = Query(None, ge=1, le=12),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
):
    # Attendance listing: same filters, sorting and pagination
    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            date_filter = {}

            if date_from:
                date_filter["$gte"] = date_from
            if date_to:
                date_filter["$lte"] = date_to

            query["date"] = date_filter

        if status:
            query["status"] = status

        return db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    # All remaining analytics require year and month
    if endpoint in {
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
    }:
        if year is None or month is None:
            raise HTTPException(
                status_code=422,
                detail="year and month are required for this endpoint",
            )

        month_start = datetime(year, month, 1).date()

        if month == 12:
            next_month = datetime(year + 1, 1, 1).date()
        else:
            next_month = datetime(year, month + 1, 1).date()

        month_end = next_month - timedelta(days=1)

        start_str = month_start.isoformat()
        end_str = month_end.isoformat()

    # Monthly employee analytics
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(
                status_code=422,
                detail="emp_code is required",
            )

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": start_str,
                        "$lte": end_str,
                    },
                }
            },
            {
                "$set": {
                    "_day": {
                        "$dayOfWeek": {
                            "$dateFromString": {
                                "dateString": "$date",
                            }
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$gte": ["$_day", 2]},
                                        {"$lte": ["$_day", 6]},
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]

        return run_explain("attendance_logs", pipeline)

    # Department summary: monthly attendance aggregation
    if endpoint == "department_summary":
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start_str,
                        "$lte": end_str,
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {
                "$match": {
                    "employee.joined_on": {"$lte": end_str}
                }
            },
        ]

        if department is not None:
            pipeline.append({
                "$match": {
                    "employee.department": department
                }
            })

        pipeline.append({
            "$group": {
                "_id": "$employee.department",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        })

        return run_explain("attendance_logs", pipeline)

    # Late-arrival leaderboard
    if endpoint == "late_leaderboard":
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start_str,
                        "$lte": end_str,
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append({
                "$match": {
                    "employee.department": department
                }
            })

        pipeline += [
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                }
            },
            {
                "$match": {
                    "total_late_minutes": {"$gt": 0}
                }
            },
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$limit": limit},
        ]

        return run_explain("attendance_logs", pipeline)

    # Department trend: explain the indexed employee lookup
    # and attendance join used to produce the trend.
    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(
                status_code=422,
                detail="department, date_from, and date_to are required",
            )

        try:
            start_day = datetime.strptime(date_from, "%Y-%m-%d").date()
            end_day = datetime.strptime(date_to, "%Y-%m-%d").date()

            if start_day.isoformat() != date_from:
                raise ValueError
            if end_day.isoformat() != date_to:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Dates must use YYYY-MM-DD format",
            )

        if end_day < start_day:
            raise HTTPException(
                status_code=422,
                detail="date_to cannot be earlier than date_from",
            )

        if (end_day - start_day).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        start_dt = datetime.combine(
            start_day, datetime.min.time(), tzinfo=timezone.utc
        )
        end_dt = datetime.combine(
            end_day + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )

        pipeline = [
            {"$match": {"department": department}},
            {"$set": {"_date": start_dt}},
            {
                "$densify": {
                    "field": "_date",
                    "partitionByFields": ["emp_code"],
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_dt],
                    },
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "employee_code": "$emp_code",
                        "attendance_date": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$_date",
                                "timezone": "UTC",
                            }
                        },
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {
                                            "$eq": [
                                                "$emp_code",
                                                "$$employee_code",
                                            ]
                                        },
                                        {
                                            "$eq": [
                                                "$date",
                                                "$$attendance_date",
                                            ]
                                        },
                                    ]
                                }
                            }
                        },
                        {"$limit": 1},
                    ],
                    "as": "_day_log",
                }
            },
        ]

        return run_explain("employees", pipeline)

    raise HTTPException(
        status_code=404,
        detail=(
            "Unknown endpoint. Use attendance_list, employee_monthly, "
            "department_summary, late_leaderboard, or department_trend."
        ),
    )
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
