import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes() -> None:
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="employees_emp_code_unique"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employees_department_emp_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="employees_joined_department",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="employees_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_emp_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="attendance_emp_date_desc",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_emp_punchin",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_late_emp",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        create_indexes()
    except PyMongoError:
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
DateQuery = Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def parse_date(value: str, field_name: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"invalid {field_name}") from exc


def month_bounds(month: str) -> tuple[date, date, str, str]:
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(422, "invalid month") from exc
    next_month = (
        date(start.year + 1, 1, 1)
        if start.month == 12
        else date(start.year, start.month + 1, 1)
    )
    end = next_month - timedelta(days=1)
    return start, end, start.isoformat(), next_month.isoformat()


def truncate_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_seconds(datetime.now(UTC))


def from_epoch_ms(value: int | None) -> datetime:
    if value is None:
        return now_utc()
    try:
        return truncate_seconds(datetime.fromtimestamp(value / 1000, UTC))
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(422, "invalid epoch milliseconds") from exc


def to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def parse_shift(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def attendance_date_for(punch_in: datetime, employee: dict[str, Any]) -> str:
    local = punch_in.astimezone(IST)
    result = local.date()
    start = parse_shift(employee["shift_start"])
    end = parse_shift(employee["shift_end"])
    if end <= start and local.time().replace(tzinfo=None) < end:
        result -= timedelta(days=1)
    return result.isoformat()


def compute_late_minutes(
    punch_in: datetime, shift_start: str, record_date: str | None = None
) -> int:
    local = punch_in.astimezone(IST)
    day = parse_date(record_date) if record_date else local.date()
    start = datetime.combine(day, parse_shift(shift_start), IST).astimezone(UTC)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    value = (seconds / Decimal("3600")).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return float(value)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str | None = None,
) -> int:
    day = parse_date(date_str)
    end_time = parse_shift(shift_end)
    if shift_start is not None and end_time <= parse_shift(shift_start):
        day += timedelta(days=1)
    end = datetime.combine(day, end_time, IST).astimezone(UTC)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def normalize_document(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_epoch_ms(value)
    if isinstance(value, dict):
        return {
            key: normalize_document(item)
            for key, item in value.items()
            if key != "_id"
        }
    if isinstance(value, list):
        return [normalize_document(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    return value


def attendance_response(document: dict[str, Any]) -> dict[str, Any]:
    result = normalize_document(document)
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return result


def employee_response(document: dict[str, Any]) -> dict[str, Any]:
    return normalize_document(document)


def mongo_round_half_up(expression: Any, places: int) -> dict[str, Any]:
    factor = 10**places
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expression, factor]}, 0.5]}},
            factor,
        ]
    }


def is_weekday_expression(field: str) -> dict[str, Any]:
    parsed = {
        "$dateFromString": {
            "dateString": field,
            "timezone": "Asia/Kolkata",
            "onError": None,
            "onNull": None,
        }
    }
    return {
        "$in": [
            {
                "$dayOfWeek": {
                    "date": parsed,
                    "timezone": "Asia/Kolkata",
                }
            },
            [2, 3, 4, 5, 6],
        ]
    }


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[
        str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    ]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def validate_nullable_times(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in cannot be null")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out cannot be null")
        return self


def attendance_filter(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict[str, Any]:
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def attendance_hint(emp_code: str | None, status: str | None) -> str:
    if status:
        return "attendance_status_date_emp"
    if emp_code:
        return "attendance_emp_date_desc"
    return "attendance_date_emp"


def employee_monthly_pipeline(
    emp_code: str, month: str, start: str, end_exclusive: str
) -> list[dict[str, Any]]:
    effective_start = {
        "$cond": [{"$gt": ["$joined_on", start]}, "$joined_on", start]
    }
    effective_start_date = {
        "$dateFromString": {
            "dateString": effective_start,
            "timezone": "Asia/Kolkata",
        }
    }
    end_date = {
        "$dateFromString": {
            "dateString": end_exclusive,
            "timezone": "Asia/Kolkata",
        }
    }
    offsets = {
        "$range": [
            0,
            {
                "$max": [
                    0,
                    {
                        "$dateDiff": {
                            "startDate": effective_start_date,
                            "endDate": end_date,
                            "unit": "day",
                            "timezone": "Asia/Kolkata",
                        }
                    },
                ]
            },
        ]
    }
    workday_count = {
        "$size": {
            "$filter": {
                "input": offsets,
                "as": "offset",
                "cond": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": effective_start_date,
                                        "unit": "day",
                                        "amount": "$$offset",
                                        "timezone": "Asia/Kolkata",
                                    }
                                },
                                "timezone": "Asia/Kolkata",
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        }
    }
    weekday = is_weekday_expression("$$log.date")
    present_days = {
        "$sum": {
            "$map": {
                "input": "$logs",
                "as": "log",
                "in": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                weekday,
                            ]
                        },
                        {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]},
                        0,
                    ]
                },
            }
        }
    }
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", end_exclusive]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$set": {"working_days": workday_count, "present_days": present_days}},
        {
            "$set": {
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        mongo_round_half_up(
                            {
                                "$multiply": [
                                    {"$divide": ["$present_days", "$working_days"]},
                                    100,
                                ]
                            },
                            2,
                        ),
                        None,
                    ]
                },
            }
        },
    ]


def department_summary_pipeline(
    start: str, end_exclusive: str, month_end: str, department: str | None
) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    weekday = is_weekday_expression("$logs.date")
    present_condition = {
        "$and": [
            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
            weekday,
        ]
    }
    worked_condition = {
        "$and": [
            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
            {"$ne": ["$logs.work_hours", None]},
        ]
    }
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", end_exclusive]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            present_condition,
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {"$cond": [worked_condition, "$logs.work_hours", None]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$avg_work_hours", None]},
                        None,
                        mongo_round_half_up("$avg_work_hours", 2),
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    start: str, end_exclusive: str, limit: int, department: str | None
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start, "$lt": end_exclusive},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
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
    )
    return pipeline


def department_trend_pipeline(
    department: str, start: date, end: date
) -> list[dict[str, Any]]:
    start_datetime = datetime.combine(start, time.min, IST).astimezone(UTC)
    number_of_days = (end - start).days + 1
    date_value = {
        "$dateAdd": {
            "startDate": start_datetime,
            "unit": "day",
            "amount": "$day_offset",
            "timezone": "Asia/Kolkata",
        }
    }
    present_value = {
        "$sum": {
            "$map": {
                "input": "$logs",
                "as": "log",
                "in": {
                    "$cond": [
                        {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]},
                        0,
                    ]
                },
            }
        }
    }
    late_value = {
        "$size": {
            "$filter": {
                "input": "$logs",
                "as": "log",
                "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
            }
        }
    }
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
                "employee_codes": {"$push": "$emp_code"},
            }
        },
        {"$set": {"day_offset": {"$range": [0, number_of_days]}}},
        {"$unwind": "$day_offset"},
        {"$set": {"date_value": date_value}},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$date_value",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"target_date": "$date", "codes": "$employee_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$target_date"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "date": "$date_value",
                                "timezone": "Asia/Kolkata",
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {"$lte": ["$$employee.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": present_value,
                "late_count": late_value,
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        mongo_round_half_up(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_value": 1},
                "output": {
                    "moving_avg_raw": {
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
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_raw", None]},
                        None,
                        mongo_round_half_up("$moving_avg_raw", 4),
                    ]
                },
            }
        },
    ]


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["created_at"] = now_utc()
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_response(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = {"department": department} if department is not None else {}
    hint = "employees_department_emp_code" if department is not None else "employees_emp_code_unique"
    items = list(
        db.employees.find(query, {"_id": 0})
        .hint(hint)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    total = db.employees.count_documents(query, hint=hint)
    return {
        "items": [employee_response(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    timestamp = from_epoch_ms(body.punched_at)
    record_date = attendance_date_for(timestamp, employee)
    document = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": timestamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            timestamp, employee["shift_start"], record_date
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_response(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    timestamp = from_epoch_ms(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": timestamp}},
        sort=[("punch_in", DESCENDING)],
        hint="attendance_emp_punchin",
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    punch_in_time = record.get("punch_in")
    if punch_in_time is None:
        raise HTTPException(404, "no punch-in found")
    duration = timestamp - punch_in_time
    if duration.total_seconds() <= 0 or duration > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(punch_in_time, timestamp)
    update = {
        "punch_out": timestamp,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            timestamp,
            employee["shift_end"],
            record["date"],
            employee["shift_start"],
        ),
        "half_day": work_hours < 4.5,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: DateQuery = None,
    date_to: DateQuery = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    hint = attendance_hint(emp_code, status)
    cursor = (
        db.attendance_logs.find(query)
        .hint(hint)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [attendance_response(item) for item in cursor]
    total = db.attendance_logs.count_documents(query, hint=hint)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    record_date = date
    parse_date(record_date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if record is None:
        raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set
    requested_status = body.status if "status" in supplied else record["status"]
    if requested_status in ("ABSENT", "LEAVE") and (
        "punch_in" in supplied or "punch_out" in supplied
    ):
        raise HTTPException(422, "absence and leave cannot include punch times")
    if requested_status in ("ABSENT", "LEAVE"):
        punch_in_time = None
        punch_out_time = None
    else:
        punch_in_time = (
            from_epoch_ms(body.punch_in)
            if "punch_in" in supplied
            else record.get("punch_in")
        )
        punch_out_time = (
            from_epoch_ms(body.punch_out)
            if "punch_out" in supplied
            else record.get("punch_out")
        )
        if punch_in_time is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date_for(punch_in_time, employee) != record_date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if punch_out_time is not None:
            duration = punch_out_time - punch_in_time
            if duration.total_seconds() <= 0 or duration > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    final: dict[str, Any] = {
        "status": requested_status,
        "punch_in": punch_in_time,
        "punch_out": punch_out_time,
    }
    if requested_status in ("ABSENT", "LEAVE"):
        final.update(
            {
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            }
        )
    else:
        late_minutes = compute_late_minutes(
            punch_in_time, employee["shift_start"], record_date
        )
        if punch_out_time is None:
            final.update(
                {
                    "work_hours": None,
                    "late_minutes": late_minutes,
                    "overtime_minutes": 0,
                    "half_day": False,
                }
            )
        else:
            hours = compute_work_hours(punch_in_time, punch_out_time)
            final.update(
                {
                    "work_hours": hours,
                    "late_minutes": late_minutes,
                    "overtime_minutes": compute_overtime(
                        punch_out_time,
                        employee["shift_end"],
                        record_date,
                        employee["shift_start"],
                    ),
                    "half_day": hours < 4.5,
                }
            )
    changes: dict[str, Any] = {}
    defaults = {
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    for field, new_value in final.items():
        old_value = record.get(field, defaults.get(field))
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "regularization changes nothing")
    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    snapshot_filter = {key: value for key, value in record.items()}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter,
        {"$set": final, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    _, _, start, end_exclusive = month_bounds(month)
    pipeline = employee_monthly_pipeline(emp_code, month, start, end_exclusive)
    rows = list(db.employees.aggregate(pipeline, hint="employees_emp_code_unique"))
    if not rows:
        raise HTTPException(404, "employee not found")
    return rows[0]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: str | None = None):
    _, month_end, start, end_exclusive = month_bounds(month)
    pipeline = department_summary_pipeline(
        start, end_exclusive, month_end.isoformat(), department
    )
    hint = (
        "employees_department_joined"
        if department is not None
        else "employees_joined_department"
    )
    items = list(db.employees.aggregate(pipeline, hint=hint))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Month,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
):
    _, _, start, end_exclusive = month_bounds(month)
    pipeline = late_leaderboard_pipeline(start, end_exclusive, limit, department)
    items = list(
        db.attendance_logs.aggregate(pipeline, hint="attendance_date_late_emp")
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[str, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to_date: Annotated[str, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")],
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, start, end)
    items = list(
        db.employees.aggregate(pipeline, hint="employees_department_joined")
    )
    return {"department": department, "items": items}


def explain_find_attendance(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    page: int,
    page_size: int,
) -> tuple[str, dict[str, Any]]:
    query = attendance_filter(emp_code, date_from, date_to, status)
    command = {
        "find": "attendance_logs",
        "filter": query,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": attendance_hint(emp_code, status),
    }
    return "attendance_logs", db.command("explain", command, verbosity="executionStats")


def explain_aggregate(
    collection: str, pipeline: list[dict[str, Any]], hint: str
) -> dict[str, Any]:
    command = {
        "aggregate": collection,
        "pipeline": pipeline,
        "cursor": {},
        "hint": hint,
    }
    return db.command("explain", command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: DateQuery = None,
    date_to: DateQuery = None,
    status: Status | None = None,
    from_date: Annotated[
        str | None, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ] = None,
    to_date: Annotated[
        str | None, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection, explained = explain_find_attendance(
            emp_code, date_from, date_to, status, page, page_size
        )
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        _, _, start, end_exclusive = month_bounds(month)
        collection = "employees"
        explained = explain_aggregate(
            collection,
            employee_monthly_pipeline(emp_code, month, start, end_exclusive),
            "employees_emp_code_unique",
        )
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        _, month_end, start, end_exclusive = month_bounds(month)
        collection = "employees"
        hint = (
            "employees_department_joined"
            if department is not None
            else "employees_joined_department"
        )
        explained = explain_aggregate(
            collection,
            department_summary_pipeline(
                start, end_exclusive, month_end.isoformat(), department
            ),
            hint,
        )
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        _, _, start, end_exclusive = month_bounds(month)
        collection = "attendance_logs"
        explained = explain_aggregate(
            collection,
            late_leaderboard_pipeline(start, end_exclusive, limit, department),
            "attendance_date_late_emp",
        )
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        collection = "employees"
        explained = explain_aggregate(
            collection,
            department_trend_pipeline(department, start, end),
            "employees_department_joined",
        )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": normalize_document(explained),
    }
