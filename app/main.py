import os

import json

import re

from copy import deepcopy

from datetime import date, datetime, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Literal, Optional

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query, Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from pymongo import MongoClient, ReturnDocument

from pymongo.errors import DuplicateKeyError

from bson.json_util import dumps, loads

import socket

from pymongo import MongoClient

load_dotenv()

_original_getaddrinfo = socket.getaddrinfo

def ipv4_getaddrinfo(*args, **kwargs):

    results = _original_getaddrinfo(*args, **kwargs)

    ipv4_results = [item for item in results if item[0] == socket.AF_INET]

    return ipv4_results or results

socket.getaddrinfo = ipv4_getaddrinfo

client = MongoClient(

    os.getenv("MONGO_URI", "mongodb://localhost:27017"),

    serverSelectionTimeoutMS=15000,

    connectTimeoutMS=10000,

    tz_aware=True,

)

db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

STATUS = Literal["PRESENT", "WFH", "ON_DUTY"]

class EmployeeIn(BaseModel):

    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(min_length=1, max_length=40)

    name: str = Field(min_length=1)

    email: str = Field(min_length=3)

    department: str = Field(min_length=1)

    shift_start: str = "09:30"

    shift_end: str = "18:30"

    joined_on: str

    @field_validator("shift_start", "shift_end")

    @classmethod

    def validate_shift(cls, value):

        if not re.fullmatch(r"\d{2}:\d{2}", value):

            raise ValueError("time must be HH:MM")

        h, m = map(int, value.split(":"))

        if h > 23 or m > 59:

            raise ValueError("time must be a valid HH:MM value")

        return value

    @field_validator("joined_on")

    @classmethod

    def validate_joined_on(cls, value):

        parse_date(value)

        return value

class PunchInIn(BaseModel):

    model_config = ConfigDict(extra="forbid")

    emp_code: str

    punched_at: Optional[StrictInt] = None

    status: STATUS = "PRESENT"

class PunchOutIn(BaseModel):

    model_config = ConfigDict(extra="forbid")

    emp_code: str

    punched_at: Optional[StrictInt] = None

class CorrectionIn(BaseModel):

    model_config = ConfigDict(extra="forbid")

    status: Optional[STATUS] = None

    punch_in: Optional[StrictInt] = None

    punch_out: Optional[StrictInt] = None

    reason: str = Field(min_length=1, max_length=1000)

# Date/time helpers -----------------------------------------------------------

def parse_date(value: str) -> date:

    try:

        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):

            raise ValueError

        return date.fromisoformat(value)

    except (TypeError, ValueError):

        raise HTTPException(422, "Date must be YYYY-MM-DD")

def parse_month(value: str) -> tuple[date, date]:

    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):

        raise HTTPException(422, "Month must be YYYY-MM")

    try:

        start = date.fromisoformat(value + "-01")

    except ValueError:

        raise HTTPException(422, "Month must be YYYY-MM")

    next_month = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)

    return start, next_month

def parse_epoch_ms(value: Optional[int]) -> datetime:

    try:

        if value is None:

            return datetime.now(UTC).replace(microsecond=0)

        # StrictInt in the request model rejects bool and float.

        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)

    except (ValueError, OverflowError, OSError):

        raise HTTPException(422, "Timestamp must be a valid epoch-millisecond integer")

def hhmm(value: str) -> tuple[int, int]:

    h, m = map(int, value.split(":"))

    return h, m

def is_overnight(employee: dict) -> bool:

    return hhmm(employee["shift_end"]) <= hhmm(employee["shift_start"])

def attendance_date_for(punch_in: datetime, employee: dict) -> str:

    local = punch_in.astimezone(IST)

    if is_overnight(employee) and (local.hour, local.minute) < hhmm(employee["shift_end"]):

        local -= timedelta(days=1)

    return local.date().isoformat()

def shift_start_datetime(date_str: str, employee: dict) -> datetime:

    d = parse_date(date_str)

    h, m = hhmm(employee["shift_start"])

    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)

def shift_end_datetime(date_str: str, employee: dict) -> datetime:

    d = parse_date(date_str)

    if is_overnight(employee):

        d += timedelta(days=1)

    h, m = hhmm(employee["shift_end"])

    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:

    local = punch_in.astimezone(IST).replace(microsecond=0)

    h, m = hhmm(shift_start)

    start = local.replace(hour=h, minute=m, second=0, microsecond=0)

    elapsed = int((local - start).total_seconds())

    return 0 if elapsed <= 600 else elapsed // 60

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:

    seconds = int((punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds())

    value = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return float(value)

def compute_overtime(punch_out: datetime, date_str: str, employee: dict) -> int:

    end = shift_end_datetime(date_str, employee).astimezone(UTC)

    seconds = int((punch_out.replace(microsecond=0) - end).total_seconds())

    return seconds // 60 if seconds >= 1800 else 0

def half_up(value, places=2):

    quantum = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def serialize_doc(doc: dict) -> dict:

    result = dict(doc)

    result.pop("_id", None)

    return result

def ensure_indexes():

    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employee_emp_code")

    db.employees.create_index([("department", 1), ("emp_code", 1)], name="ix_employee_department_code")

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_emp_date")

    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_emp")

    db.attendance_logs.create_index([("status", 1), ("date", -1)], name="ix_attendance_status_date")

    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="ix_attendance_emp_date_desc")

@app.on_event("startup")

def startup():

    ensure_indexes()

@app.get("/health")

def health():

    return {"status": "ok"}

# Employee endpoints ----------------------------------------------------------

@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:

        db.employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "emp_code already exists")

    return serialize_doc(doc)

@app.get("/employees")

def list_employees(

    department: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    query = {"department": department} if department else {}

    total = db.employees.count_documents(query)

    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))

    return {"items": items, "total": total, "page": page, "page_size": page_size}

# Attendance endpoints --------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    employee = db.employees.find_one({"emp_code": body.emp_code})

    if employee is None:

        raise HTTPException(404, "Employee not found")

    punch_time = parse_epoch_ms(body.punched_at)

    attendance_date = attendance_date_for(punch_time, employee)

    document = {

        "emp_code": body.emp_code,

        "date": attendance_date,

        "status": body.status,

        "punch_in": punch_time,

        "punch_out": None,

        "work_hours": None,

        "late_minutes": compute_late_minutes(punch_time, employee["shift_start"]),

        "overtime_minutes": 0,

        "half_day": False,

        "history": [],

    }

    try:

        db.attendance_logs.insert_one(document)

    except DuplicateKeyError:

        raise HTTPException(409, "Already punched in for this attendance date")

    return serialize_doc(document)

@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    employee = db.employees.find_one({"emp_code": body.emp_code})

    if employee is None:

        raise HTTPException(404, "Employee not found")

    punch_time = parse_epoch_ms(body.punched_at)

    local_date = punch_time.astimezone(IST).date().isoformat()

    # An overnight shift may have started the previous day.

    candidate_dates = [local_date, (parse_date(local_date) - timedelta(days=1)).isoformat()]

    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": {"$in": candidate_dates}, "punch_out": None}, sort=[("date", -1)])

    if record is None:

        raise HTTPException(409, "No open punch-in record found")

    work_hours = compute_work_hours(record["punch_in"], punch_time)

    overtime = compute_overtime(punch_time, record["date"], employee)

    updated = db.attendance_logs.find_one_and_update(

        {"_id": record["_id"], "punch_out": None},

        {"$set": {"punch_out": punch_time, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": work_hours < 4.50}},

        return_document=ReturnDocument.AFTER,

    )

    if updated is None:

        raise HTTPException(409, "Punch-out already recorded")

    return serialize_doc(updated)

@app.patch("/attendance/{emp_code}/{date_str}")

def correct_attendance(emp_code: str, date_str: str, body: CorrectionIn):

    parse_date(date_str)

    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:

        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})

    if record is None:

        raise HTTPException(404, "Attendance record not found")

    changes = body.model_dump(exclude_unset=True)

    reason = changes.pop("reason")

    updates = {}

    if "status" in changes and changes["status"] is not None:

        updates["status"] = changes["status"]

    if "punch_in" in changes and changes["punch_in"] is not None:

        updates["punch_in"] = parse_epoch_ms(changes["punch_in"])

    if "punch_out" in changes and changes["punch_out"] is not None:

        updates["punch_out"] = parse_epoch_ms(changes["punch_out"])

    current_in = updates.get("punch_in", record.get("punch_in"))

    current_out = updates.get("punch_out", record.get("punch_out"))

    if current_out is not None:

        if current_in is None or current_out < current_in:

            raise HTTPException(422, "punch_out must be after punch_in")

        updates["work_hours"] = compute_work_hours(current_in, current_out)

        updates["overtime_minutes"] = compute_overtime(current_out, date_str, employee)

        updates["half_day"] = updates["work_hours"] < 4.50

    elif "punch_in" in updates:

        updates["work_hours"] = None

        updates["overtime_minutes"] = 0

        updates["half_day"] = False

    if "punch_in" in updates:

        updates["late_minutes"] = compute_late_minutes(updates["punch_in"], employee["shift_start"])

    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "reason": reason, "changes": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in updates.items()}}

    updated = db.attendance_logs.find_one_and_update(

        {"_id": record["_id"]},

        {"$set": updates, "$push": {"history": history_entry}},

        return_document=ReturnDocument.AFTER,

    )

    return serialize_doc(updated)

@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[STATUS] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    query = {}

    if emp_code:

        query["emp_code"] = emp_code

    if status:

        query["status"] = status

    if date_from or date_to:

        query["date"] = {}

        if date_from:

            parse_date(date_from)

            query["date"]["$gte"] = date_from

        if date_to:

            parse_date(date_to)

            query["date"]["$lte"] = date_to

        if date_from and date_to and date_from > date_to:

            raise HTTPException(422, "date_from must be <= date_to")

    total = db.attendance_logs.count_documents(query)

    projection = {"_id": 0, "emp_code": 1, "date": 1, "status": 1, "punch_in": 1, "punch_out": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1, "history": 1}

    items = list(db.attendance_logs.find(query, projection).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))

    return {"items": items, "total": total, "page": page, "page_size": page_size}

# Analytics: aggregation pipelines only ---------------------------------------

def build_employee_monthly_pipeline(emp_code: str, start: date, next_month: date):

    start_s, end_s = start.isoformat(), next_month.isoformat()

    return [

        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lt": end_s}}},

        {"$group": {

            "_id": None,

            "records": {"$sum": 1},

            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},

            "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},

            "work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},

            "half_days": {"$sum": {"$cond": ["$half_day", 1, 0]}},

            "present_days": {"$sum": {"$cond": [{"$and": [

                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},

                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}

            ]}, 1, 0]}}

        }},

        {"$project": {

            "_id": 0,

            "records": 1,

            "present_days": 1,

            "late_minutes": 1,

            "overtime_minutes": 1,

            "work_hours": {"$round": ["$work_hours", 2]},

            "half_days": 1

        }}

    ]

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(..., description="YYYY-MM")):

    start, next_month = parse_month(month)

    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "name": 1, "department": 1})

    if employee is None:

        raise HTTPException(404, "Employee not found")

    start_s, end_s = start.isoformat(), next_month.isoformat()

    pipeline = build_employee_monthly_pipeline(emp_code, start, next_month)

    rows = list(db.attendance_logs.aggregate(pipeline))

    stats = rows[0] if rows else {"records": 0, "present_days": 0, "late_minutes": 0, "overtime_minutes": 0, "work_hours": 0.0, "half_days": 0}

    return {"emp_code": emp_code, "name": employee.get("name"), "department": employee.get("department"), "month": month, **stats}

@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(..., description="YYYY-MM")):

    start, next_month = parse_month(month)

    end_s = (next_month - timedelta(days=1)).isoformat()

    pipeline = [

        {"$match": {"joined_on": {"$lte": end_s}}},

        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lt": ["$date", next_month.isoformat()]}]}}}, {"$group": {"_id": None, "records": {"$sum": 1}, "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}, "work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}}}}], "as": "stats"}},

        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},

        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "employees_with_records": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$stats.records", 0]}, 0]}, 1, 0]}}, "records": {"$sum": {"$ifNull": ["$stats.records", 0]}}, "late_minutes": {"$sum": {"$ifNull": ["$stats.late_minutes", 0]}}, "overtime_minutes": {"$sum": {"$ifNull": ["$stats.overtime_minutes", 0]}}, "work_hours": {"$sum": {"$ifNull": ["$stats.work_hours", 0]}}}},

        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "employees_with_records": 1, "records": 1, "late_minutes": 1, "overtime_minutes": 1, "work_hours": {"$round": ["$work_hours", 2]}}},

        {"$sort": {"department": 1}},

    ]

    return {"month": month, "items": list(db.employees.aggregate(pipeline))}

@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str = Query(..., description="YYYY-MM"), limit: int = Query(10, ge=1, le=100)):

    start, next_month = parse_month(month)

    pipeline = [

        {"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}},

        {"$group": {"_id": "$emp_code", "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_days": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},

        {"$setWindowFields": {"sortBy": {"late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},

        {"$match": {"rank": {"$lte": limit}}},

        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": {"path": "$employee", "preserveNullAndEmptyArrays": True}},

        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "late_minutes": 1, "late_days": 1, "rank": 1}},

        {"$sort": {"rank": 1, "emp_code": 1}},

    ]

    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, start_date: str = Query(...), end_date: str = Query(...), window: int = Query(7, ge=1, le=31)):

    start_d, end_d = parse_date(start_date), parse_date(end_date)

    if end_d < start_d:

        raise HTTPException(422, "end_date must be >= start_date")

    days = (end_d - start_d).days + 1

    if days > 366:

        raise HTTPException(422, "Date range cannot exceed 366 days")

    # $densify ensures days with no attendance are present; lookup employees to constrain department.

    pipeline = [

        {"$match": {"date": {"$gte": start_date, "$lte": end_date}}},

        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"},

        {"$match": {"employee.department": department}},

        {"$group": {"_id": "$date", "records": {"$sum": 1}, "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}, "work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}}}},

        {"$project": {"_id": 0, "date": "$_id", "records": 1, "late_minutes": 1, "overtime_minutes": 1, "work_hours": {"$round": ["$work_hours", 2]}}},

        {"$set": {"date": {"$dateFromString": {"dateString": "$date"}}}},

        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start_d, time.min), datetime.combine(end_d, time.min) + timedelta(days=1)]}}},

        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date", "timezone": "+00:00"}}, "records": {"$ifNull": ["$records", 0]}, "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}, "work_hours": {"$ifNull": ["$work_hours", 0]}}},

        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_work_hours": {"$avg": "$work_hours", "window": {"documents": [-(window - 1), 0]}}}}},

        {"$project": {"_id": 0, "date": 1, "records": 1, "late_minutes": 1, "overtime_minutes": 1, "work_hours": 1, "moving_avg_work_hours": {"$round": ["$moving_avg_work_hours", 2]}}},

        {"$sort": {"date": 1}},

    ]

    return {"department": department, "start_date": start_date, "end_date": end_date, "items": list(db.attendance_logs.aggregate(pipeline))}

# Explain helper --------------------------------------------------------------
# Explain helper ---------------------------------------------------------------
def json_safe_explain(result):
    """Convert MongoDB BSON-specific values to ordinary JSON-compatible values."""
    return json.loads(dumps(result))


def find_ixscan(plan):
    """Return True if an explain plan contains an IXSCAN stage."""
    if isinstance(plan, dict):
        if plan.get("stage") == "IXSCAN":
            return True
        return any(find_ixscan(value) for value in plan.values())
    if isinstance(plan, list):
        return any(find_ixscan(value) for value in plan)
    return False


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[STATUS] = None,
    department: Optional[str] = None,
    month: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=100),
    window: int = Query(7, ge=1, le=31),
):
    """Run MongoDB executionStats explain for supported endpoints."""
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                parse_date(date_from)
                query["date"]["$gte"] = date_from
            if date_to:
                parse_date(date_to)
                query["date"]["$lte"] = date_to
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must be <= date_to")

        command = {
            "find": "attendance_logs",
            "filter": query,
            "projection": {
                "_id": 0, "emp_code": 1, "date": 1, "status": 1,
                "punch_in": 1, "punch_out": 1, "work_hours": 1,
                "late_minutes": 1, "overtime_minutes": 1,
                "half_day": 1, "history": 1,
            },
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        result = db.attendance_logs.database.command(
            "explain", command, verbosity="executionStats"
        )
        return json_safe_explain(result)

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, next_month = parse_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, start, next_month)
        result = db.attendance_logs.database.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return json_safe_explain(result)

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, next_month = parse_month(month)
        end_s = (next_month - timedelta(days=1)).isoformat()
        pipeline = [
            {"$match": {"joined_on": {"$lte": end_s}}},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", start.isoformat()]},
                        {"$lt": ["$date", next_month.isoformat()]},
                    ]}}},
                    {"$group": {
                        "_id": None,
                        "records": {"$sum": 1},
                        "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                        "work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                    }},
                ],
                "as": "stats",
            }},
            {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "employees_with_records": {"$sum": {"$cond": [
                    {"$gt": [{"$ifNull": ["$stats.records", 0]}, 0]}, 1, 0
                ]}},
                "records": {"$sum": {"$ifNull": ["$stats.records", 0]}},
                "late_minutes": {"$sum": {"$ifNull": ["$stats.late_minutes", 0]}},
                "overtime_minutes": {"$sum": {"$ifNull": ["$stats.overtime_minutes", 0]}},
                "work_hours": {"$sum": {"$ifNull": ["$stats.work_hours", 0]}},
            }},
            {"$project": {
                "_id": 0, "department": "$_id", "headcount": 1,
                "employees_with_records": 1, "records": 1,
                "late_minutes": 1, "overtime_minutes": 1,
                "work_hours": {"$round": ["$work_hours", 2]},
            }},
            {"$sort": {"department": 1}},
        ]
        result = db.employees.database.command(
            "explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return json_safe_explain(result)

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, next_month = parse_month(month)
        pipeline = [
            {"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}},
            {"$group": {
                "_id": "$emp_code",
                "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_days": {"$sum": {"$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
                ]}},
            }},
            {"$setWindowFields": {
                "sortBy": {"late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$lookup": {
                "from": "employees", "localField": "_id",
                "foreignField": "emp_code", "as": "employee",
            }},
            {"$unwind": {"path": "$employee", "preserveNullAndEmptyArrays": True}},
            {"$project": {
                "_id": 0, "emp_code": "$_id", "name": "$employee.name",
                "department": "$employee.department", "late_minutes": 1,
                "late_days": 1, "rank": 1,
            }},
            {"$sort": {"rank": 1, "emp_code": 1}},
        ]
        result = db.attendance_logs.database.command(
            "explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return json_safe_explain(result)

    if endpoint == "department_trend":
        if not department or not start_date or not end_date:
            raise HTTPException(422, "department, start_date and end_date are required")
        start_d, end_d = parse_date(start_date), parse_date(end_date)
        if end_d < start_d:
            raise HTTPException(422, "end_date must be >= start_date")
        if (end_d - start_d).days + 1 > 366:
            raise HTTPException(422, "Date range cannot exceed 366 days")

        pipeline = [
            {"$match": {"date": {"$gte": start_date, "$lte": end_date}}},
            {"$lookup": {
                "from": "employees", "localField": "emp_code",
                "foreignField": "emp_code", "as": "employee",
            }},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$group": {
                "_id": "$date", "records": {"$sum": 1},
                "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            }},
            {"$project": {
                "_id": 0, "date": "$_id", "records": 1,
                "late_minutes": 1, "overtime_minutes": 1,
                "work_hours": {"$round": ["$work_hours", 2]},
            }},
            {"$set": {"date": {"$dateFromString": {"dateString": "$date"}}}},
            {"$densify": {
                "field": "date",
                "range": {
                    "step": 1, "unit": "day",
                    "bounds": [
                        datetime.combine(start_d, time.min),
                        datetime.combine(end_d, time.min) + timedelta(days=1),
                    ],
                },
            }},
            {"$set": {
                "date": {"$dateToString": {
                    "format": "%Y-%m-%d", "date": "$date", "timezone": "+00:00"
                }},
                "records": {"$ifNull": ["$records", 0]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "work_hours": {"$ifNull": ["$work_hours", 0]},
            }},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_work_hours": {
                    "$avg": "$work_hours",
                    "window": {"documents": [-(window - 1), 0]},
                }},
            }},
            {"$project": {
                "_id": 0, "date": 1, "records": 1,
                "late_minutes": 1, "overtime_minutes": 1, "work_hours": 1,
                "moving_avg_work_hours": {"$round": ["$moving_avg_work_hours", 2]},
            }},
            {"$sort": {"date": 1}},
        ]
        result = db.attendance_logs.database.command(
            "explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return json_safe_explain(result)

    raise HTTPException(
        status_code=404,
        detail=(
            "Unknown endpoint. Use attendance_list, employee_monthly, "
            "department_summary, late_leaderboard, department_trend"
        ),
    )
