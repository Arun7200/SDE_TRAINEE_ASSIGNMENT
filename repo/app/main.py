import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# tz_aware so pymongo gives back UTC aware datetimes, not naive ones
client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]


def create_indexes():
    # safe to run on every start, existing indexes are left as they are
    employees.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")
    employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined")
    employees.create_index([("joined_on", 1)], name="joined")
    # one record per employee per day, this is what stops double punch-ins
    logs.create_index([("emp_code", 1), ("date", -1)], unique=True, name="emp_date_unique")
    logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp")
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(app):
    try:
        create_indexes()
    except Exception as e:
        print("could not create indexes:", e)  # /health will show 503 if mongo is really down
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


# ---------------------------------------------------------------- helpers

def ms_to_dt(ms):
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt):
    if dt is None:
        return None
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_time(ms):
    """epoch ms from the client -> datetime, dropping the milliseconds (R1)"""
    return ms_to_dt(ms // 1000 * 1000)


def round_half_up(value, places):
    # python round() and mongo $round both round .5 to even, so use Decimal
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def hhmm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(emp):
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in, emp):
    # R1: date is the IST date, but early punches on a night shift belong to yesterday
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        d = d - timedelta(days=1)
    return d


def shift_start_dt(d, emp):
    return datetime.combine(d, hhmm(emp["shift_start"]), tzinfo=IST)


def shift_end_dt(d, emp):
    end = datetime.combine(d, hhmm(emp["shift_end"]), tzinfo=IST)
    if is_overnight(emp):
        end = end + timedelta(days=1)
    return end


def compute_late_minutes(punch_in, start):
    # R2: late only if more than 10 min (600 s) after start, then count from start
    seconds = int((punch_in - start).total_seconds())
    if seconds > 600:
        return seconds // 60
    return 0


def compute_overtime(punch_out, end):
    # R3
    minutes = int((punch_out - end).total_seconds()) // 60
    if minutes >= 30:
        return minutes
    return 0


def compute_work_hours(punch_in, punch_out):
    # R4
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(emp, day, status, punch_in, punch_out):
    """late, overtime, work hours and half day for a record"""
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENT_STATUSES or punch_in is None:
        return result
    result["late_minutes"] = compute_late_minutes(punch_in, shift_start_dt(day, emp))
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        result["work_hours"] = hours
        result["overtime_minutes"] = compute_overtime(punch_out, shift_end_dt(day, emp))
        result["half_day"] = hours < 4.5  # R5, uses the rounded value
    return result


def working_days(first, last):
    # R7: Mon-Fri between two dates, both included
    count = 0
    d = first
    while d <= last:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def month_range(month):
    year, m = int(month[:4]), int(month[5:])
    return date(year, m, 1), date(year, m, calendar.monthrange(year, m)[1])


def error422(field, message):
    return RequestValidationError([{"type": "value_error", "loc": ("query", field), "msg": message, "input": None}])


def employee_out(doc):
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entry):
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        old, new = change.get("from"), change.get("to")
        if field in ("punch_in", "punch_out"):
            old, new = dt_to_ms(old), dt_to_ms(new)
        changes[field] = {"from": old, "to": new}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def attendance_out(doc):
    # old records can be missing half_day / history, treat as false / empty
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
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# ---------------------------------------------------------------- request models

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# epoch milliseconds, whole numbers only. Anything under 1e11 looks like seconds
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


def check_date(value):
    if value is None:
        return value
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real date")
    return value


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v):
        return check_date(v)

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end can't be the same")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None


class RegularizeIn(BaseModel):
    # derived fields in the body are just ignored
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# query parameter types
QPage = Annotated[int, Query(ge=1, le=1_000_000_000)]
QPageSize = Annotated[int, Query(ge=1, le=100)]
QMonth = Annotated[str, Query(pattern=MONTH_PATTERN)]


def query_date(name=None):
    return Annotated[Optional[str], Query(alias=name, pattern=DATE_PATTERN)]


# ---------------------------------------------------------------- mongo queries
# these are shared by the endpoints and the explain endpoint

def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    if date_from and date_to and date_from > date_to:
        raise error422("date_from", "date_from is after date_to")
    flt = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status

    if emp_code is not None:
        index = "emp_date_unique"
        sort = {"date": -1}  # emp_code is fixed so this is the same order
    elif status is not None:
        index = "status_date_emp"
        sort = {"date": -1, "emp_code": 1}
    else:
        index = "date_emp"
        sort = {"date": -1, "emp_code": 1}
    return {"filter": flt, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size, "hint": index}


# the next few functions build small mongo expressions. `d` is the prefix of the
# field: "$" for a normal document, "$$l." when we are inside $map / $filter

def is_present(d):
    return {"$in": [d + "status", PRESENT_STATUSES]}


def is_weekday(d):
    # dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri is 2 to 6
    day = {"$dayOfWeek": {"$dateFromString": {"dateString": d + "date", "onError": None, "onNull": None}}}
    return {"$in": [day, [2, 3, 4, 5, 6]]}


def is_late(d):
    return {"$gt": [{"$ifNull": [d + "late_minutes", 0]}, 0]}


def present_value(d, weekdays_only):
    # 1 for a present day, 0.5 for a present half day, 0 if not present
    conditions = [is_present(d)]
    if weekdays_only:
        conditions.append(is_weekday(d))
    half_day = {"$eq": [{"$ifNull": [d + "half_day", False]}, True]}
    return {"$cond": [{"$and": conditions}, {"$cond": [half_day, 0.5, 1]}, 0]}


def monthly_pipeline(emp_code, month):
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value("$", True)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [is_late("$"), 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


LOG_FIELDS = {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}


def summary_pipeline(month, department):
    # starts from employees so people with no logs are still counted
    first, last = month_range(month)
    match = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match["department"] = department

    d = "$$l."
    with_hours = {"$filter": {
        "input": "$logs", "as": "l",
        "cond": {"$and": [is_present(d), {"$isNumber": d + "work_hours"}]},
    }}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": LOG_FIELDS},
            ],
            "as": "logs",
        }},
        # numbers for each employee first, then add them up per department
        {"$project": {
            "_id": 0,
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": present_value(d, True)}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": is_late(d)}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": [d + "late_minutes", 0]}}}},
            "leave_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": [d + "status", "LEAVE"]}}}},
            "on_duty_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": [d + "status", "ON_DUTY"]}}}},
            "hours_sum": {"$sum": {"$map": {"input": with_hours, "as": "l", "in": d + "work_hours"}}},
            "hours_count": {"$size": with_hours},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_sum": {"$sum": "$hours_sum"},
            "hours_count": {"$sum": "$hours_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(month, limit, department):
    first, last = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [is_late("$"), 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code",
            "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
            "as": "emp",
        }},
        {"$unwind": "$emp"},  # logs of employees that don't exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank gives 1, 2, 2, 4 for ties. the limit comes after the ranking
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


def trend_pipeline(department, date_from, date_to):
    number_of_days = (date_to - date_from).days + 1
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
    d = "$logs."
    return [
        {"$match": {"department": department, "joined_on": {"$lte": date_to.isoformat()}}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": date_from.isoformat(), "$lte": date_to.isoformat()}}},
                {"$project": LOG_FIELDS},
            ],
            "as": "logs",
        }},
        # shrink to two small lists: how many joined on each date, and totals for each day with logs
        {"$facet": {
            "joins": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            "days": [
                {"$unwind": "$logs"},
                {"$group": {
                    "_id": d + "date",
                    "present": {"$sum": present_value(d, False)},
                    "late": {"$sum": {"$cond": [is_late(d), 1, 0]}},
                }},
            ],
        }},
        # one row for every calendar day, even if nothing was logged
        {"$project": {"joins": 1, "days": 1, "rows": {"$map": {
            "input": {"$range": [0, number_of_days]}, "as": "i",
            "in": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": {
                "$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}}}},
        }}}},
        {"$unwind": "$rows"},
        {"$project": {
            "_id": 0,
            "date": "$rows.date",
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$rows.date"}}},
            "headcount": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j._id", "$rows.date"]}}},
                "as": "j", "in": "$$j.n",
            }}},
            "stat": {"$arrayElemAt": [
                {"$filter": {"input": "$days", "as": "x", "cond": {"$eq": ["$$x._id", "$rows.date"]}}}, 0]},
        }},
        {"$addFields": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "present_count": {"$ifNull": ["$stat.present", 0]},
            "late_count": {"$ifNull": ["$stat.late", 0]},
        }},
        # rate as a whole number of ten-thousandths, rounded half up (0.5 added then floor)
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$add": [{"$divide": [{"$multiply": ["$present_count", 10000]}, "$headcount"]}, 0.5]}},
            None,
        ]}}},
        # moving average over this row and the 6 before it, null rates are skipped by $avg
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"avg_int": {"$avg": "$rate_int", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$avg_int", None]}, None,
                {"$divide": [{"$floor": {"$add": ["$avg_int", 0.5]}}, 10000]},
            ]},
        }},
    ]


def check_trend_dates(from_text, to_text):
    date_from = date.fromisoformat(from_text)
    date_to = date.fromisoformat(to_text)
    if date_to < date_from:
        raise error422("to", "to is before from")
    if (date_to - date_from).days + 1 > 92:
        raise error422("to", "range is longer than 92 days")
    return date_from, date_to


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database not available")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:  # the unique index decides, so no race
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: QPage = 1, page_size: QPageSize = 20):
    flt = {}
    if department is not None:
        flt["department"] = department
    total = employees.count_documents(flt)
    cursor = employees.find(flt, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(e) for e in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    punched = to_time(body.punched_at) if body.punched_at is not None else now()
    day = attendance_date(punched, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **derive(emp, day, body.status, punched, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique index on (emp_code, date), one parallel request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    punched = to_time(body.punched_at) if body.punched_at is not None else now()

    # latest record that started at or before this punch-out
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if punched <= rec["punch_in"]:
        raise HTTPException(422, "punch-out must be after punch-in")
    if punched - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch-out is more than 24 hours after punch-in")

    values = derive(emp, date.fromisoformat(rec["date"]), rec["status"], rec["punch_in"], punched)
    # only update if it is still open, so two parallel punch-outs can't both win
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": punched, **values}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: query_date() = None,
    date_to: query_date() = None,
    status: Optional[Status] = None,
    page: QPage = 1,
    page_size: QPageSize = 20,
):
    check_query_dates(date_from, date_to)
    q = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q["filter"], hint=q["hint"])
    cursor = (logs.find(q["filter"], {"_id": 0}).hint(q["hint"])
              .sort(list(q["sort"].items())).skip(q["skip"]).limit(q["limit"]))
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def check_query_dates(*values):
    # the pattern only checks the shape, this catches things like 2026-02-30
    for v in values:
        if v is not None:
            try:
                check_date(v)
            except ValueError:
                raise error422("date", "not a real date")


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, body: RegularizeIn, day: Annotated[str, Path(pattern=DATE_PATTERN)]):
    try:
        check_date(day)
    except ValueError:
        raise error422("date", "not a real date")
    emp = get_employee(emp_code)
    sent = body.model_fields_set

    if body.status in ("ABSENT", "LEAVE") and ("punch_in" in sent or "punch_out" in sent):
        raise HTTPException(422, "ABSENT and LEAVE clear the punch times, don't send them")

    rec = logs.find_one({"emp_code": emp_code, "date": day}, {"_id": 0})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    old_history_count = len(rec.get("history") or [])

    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        new_in, new_out = None, None
    else:
        new_in = to_time(body.punch_in) if "punch_in" in sent else old_in
        new_out = to_time(body.punch_out) if "punch_out" in sent else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if attendance_date(new_in, emp).isoformat() != day:
            raise HTTPException(422, "punch_in must stay on the record's date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if status == rec["status"] and new_in == old_in and new_out == old_out:
        raise HTTPException(422, "nothing to change")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **derive(emp, date.fromisoformat(day), status, new_in, new_out)}
    old = {
        "status": rec["status"], "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {}
    for field in new:
        if new[field] != old[field]:
            changes[field] = {"from": old[field], "to": new[field]}
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # only update if nobody changed the record since we read it (same punches, same history length).
    # if someone did, the caller gets a 409 and can retry
    if old_history_count == 0:
        history_check = {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    else:
        history_check = {"history": {"$size": old_history_count}}
    updated = logs.find_one_and_update(
        {"emp_code": emp_code, "date": day, "status": rec["status"],
         "punch_in": old_in, "punch_out": old_out, **history_check},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, try again")
    return attendance_out(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: QMonth):
    emp = get_employee(emp_code)
    first, last = month_range(month)
    rows = list(logs.aggregate(monthly_pipeline(emp_code, month), hint="emp_date_unique"))
    row = rows[0] if rows else {}

    # R7: count working days from the joining date if they joined this month
    days = working_days(max(first, date.fromisoformat(emp["joined_on"])), last)
    present = row.get("present_days", 0)
    percent = None
    if days > 0:
        percent = round_half_up(Decimal(str(present)) / days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": percent,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: QMonth, department: Optional[str] = None):
    index = "dept_joined" if department is not None else "joined"
    items = []
    for r in employees.aggregate(summary_pipeline(month, department), hint=index):
        # average over records = total hours / number of records
        avg = None
        if r["hours_count"] > 0:
            avg = round_half_up(Decimal(str(round(r["hours_sum"], 8))) / r["hours_count"], 2)
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: QMonth, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    items = []
    for r in logs.aggregate(leaderboard_pipeline(month, limit, department), hint="date_emp"):
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_PATTERN)],
    to: Annotated[str, Query(pattern=DATE_PATTERN)],
):
    check_query_dates(from_, to)
    date_from, date_to = check_trend_dates(from_, to)
    if employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = list(employees.aggregate(trend_pipeline(department, date_from, date_to), hint="dept_joined"))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: query_date() = None,
    date_to: query_date() = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_PATTERN)] = None,
    to: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    page: QPage = 1,
    page_size: QPageSize = 20,
):
    def required(**params):
        for name, value in params.items():
            if value is None:
                raise error422(name, name + " is required for " + endpoint)

    check_query_dates(date_from, date_to, from_, to)

    if endpoint == "attendance_list":
        q = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        command = {"find": "attendance_logs", "filter": q["filter"], "sort": q["sort"],
                   "skip": q["skip"], "limit": q["limit"], "hint": q["hint"]}
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            required(emp_code=emp_code, month=month)
            collection, pipeline, index = "attendance_logs", monthly_pipeline(emp_code, month), "emp_date_unique"
        elif endpoint == "department_summary":
            required(month=month)
            index = "dept_joined" if department is not None else "joined"
            collection, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            required(month=month)
            collection, pipeline, index = "attendance_logs", leaderboard_pipeline(month, limit, department), "date_emp"
        else:
            required(department=department, **{"from": from_, "to": to})
            date_from2, date_to2 = check_trend_dates(from_, to)
            collection, pipeline, index = "employees", trend_pipeline(department, date_from2, date_to2), "dept_joined"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": index}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
