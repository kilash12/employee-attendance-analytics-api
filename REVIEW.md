# Starter code review

| # | Where | What is wrong | How I noticed it | How I fixed it |
|---|---|---|---|---|
| 1 | `health` | It returned success without checking MongoDB. | Stop MongoDB and `/health` still returned 200. | Ping MongoDB and return 503 when it is unavailable. |
| 2 | Startup | Required indexes were never created. | A fresh database only had `_id` indexes. | Create named, idempotent indexes during application startup. |
| 3 | `create_employee` | Duplicate detection used check-then-insert and was race-prone. | Parallel requests could both pass `find_one`. | Enforce a unique database index and map `DuplicateKeyError` to 409. |
| 4 | `create_employee` | Input formats and lengths were not validated. | Invalid employee codes, dates, emails and times were accepted. | Added Pydantic constraints and date/shift validation. |
| 5 | `create_employee` | Equal shift start and end were accepted. | A `09:30` to `09:30` shift produced an ambiguous overnight shift. | Reject equal shift times with 422. |
| 6 | `create_employee` | `created_at` was a naive local datetime and was returned as an ISO string. | The response did not contain epoch milliseconds. | Store an aware UTC datetime and serialize it to epoch milliseconds. |
| 7 | `list_employees` | Pagination skipped `page * page_size`. | Page 1 started at item 21. | Use `(page - 1) * page_size`. |
| 8 | `list_employees` | `total` ignored the department filter. | A filtered response reported the entire collection size. | Count using the same query as the item cursor. |
| 9 | `list_employees` | Results were not sorted and pagination inputs were unrestricted. | Page contents changed with natural order and oversized pages were accepted. | Sort by `emp_code` and validate page/page size. |
| 10 | `compute_late_minutes` | It mixed naive time with shift time and did not implement the exact grace boundary. | `09:40:01` and UTC input produced incorrect results. | Calculate against the IST shift start using truncated instants and whole seconds. |
| 11 | `compute_work_hours` | Python `round` uses banker's rounding. | Half-way values disagreed with half-up examples. | Use `Decimal` with `ROUND_HALF_UP`. |
| 12 | `compute_overtime` | Overnight shifts and the 30-minute minimum were not handled. | A 22:00-06:00 shift used the wrong calendar-day end and 5 minutes counted. | Build the IST shift end on the correct day and apply the threshold. |
| 13 | `punch_in` | Unknown employees caused a 500 while accessing `emp["shift_start"]`. | Punching an unknown code raised `TypeError`. | Return 404 before using the employee. |
| 14 | `punch_in` | Epoch milliseconds were interpreted in server local time and milliseconds were not truncated. | The stored attendance date changed with server timezone. | Parse as UTC, truncate to seconds, then derive the IST attendance date. |
| 15 | `punch_in` | Overnight attendance dates were not supported. | A 02:00 IST punch was assigned to the new day. | Apply the overnight rule using employee shift times. |
| 16 | `punch_in` | Any status was accepted and duplicate prevention was race-prone. | `LEAVE` could be punched in and parallel inserts could both succeed. | Restrict presence statuses and rely on the unique `(emp_code, date)` index. |
| 17 | `punch_in` | It returned an internal attendance `id`. | The response differed from the contract. | Never expose MongoDB `_id` or an attendance id. |
| 18 | `list_attendance` | It loaded every matching record and sorted/paginated in Python. | Memory and latency grew with the full collection. | Filter, sort, skip and limit in MongoDB with supporting indexes. |
| 19 | `list_attendance` | Sort order was incomplete. | Employees on the same date followed arbitrary order. | Sort by date descending and employee code ascending. |
| 20 | `list_attendance` | Date ranges, pagination and status were insufficiently validated. | Reversed ranges and invalid page values were accepted. | Added typed query validation and cross-field range validation. |
| 21 | API responses | BSON datetimes and nested history datetimes were not converted consistently. | Responses contained datetime strings instead of epoch milliseconds. | Added recursive API-boundary serialization and legacy defaults. |

I also checked the use of strings for calendar dates and shift times. That is intentional in the supplied data model, so I kept those stored values as strings. The synchronous PyMongo client is also permitted by the assignment, so I did not treat it as a defect.
