# REVIEW.md

Problems I found in the starter `app/main.py`.

| # | Where | What is wrong | How you'd notice | How I fixed it |
|---|---|---|---|---|
| 1 | compute_late_minutes | Shift start is built on a UTC/naive datetime, so it is 5h30 off from IST. | Punch in 09:35 IST (04:05 UTC), shift start becomes 09:30 UTC and the person is hours late. | Use IST datetimes for shift start. |
| 2 | compute_late_minutes | Floors to minutes and then checks `> 10`. R2 says more than 10:00 exactly. | Shift 09:30, punch 09:40:01. Should be 10, gives 0. | Compare in seconds (`> 600`), then floor. |
| 3 | compute_late_minutes | No seconds truncation, no overnight shifts. | 09:40:00.900 counts as late. Night shift after midnight gets the wrong base date. | Truncate the punch time, build shift start from the attendance date. |
| 4 | compute_work_hours | `round()` rounds half to even, R4 wants half up. | 7218 seconds = 2.005 hours, should be 2.01. | Decimal with ROUND_HALF_UP. |
| 5 | compute_overtime | No 30 minute minimum. Uses shift end on the same date, wrong for night shifts. Mixes aware and naive datetimes so it can crash. | Punch out 5 minutes after shift end gives 5, should be 0. | Minimum 30, shift end moves to next day for night shifts, all datetimes aware. |
| 6 | punch_in | `fromtimestamp` and `now()` use the server's local time and give naive datetimes. | Change the server time zone and stored times change. | UTC aware datetimes, client created with tz_aware. |
| 7 | punch_in | Date comes from the local date, not IST, and there is no night shift rule (R1). | Punch at 23:50 UTC is already the next day in IST. | attendance_date() converts to IST and applies the night shift rule. |
| 8 | punch_in | Unknown employee gives `None`, then `emp["shift_start"]` crashes (500). | emp_code "NOPE". | Return 404. |
| 9 | punch_in | find_one then insert. Two requests at the same time both pass the check. | Send two at once, get two 201s. | Unique index on (emp_code, date), catch DuplicateKeyError, return 409. |
| 10 | PunchInIn | status is any string, punched_at accepts seconds, and 0 is treated as "not given". | status ABSENT or "banana" returns 201. Seconds value lands in 1970. | Literal status, strict int in the epoch ms range, `is not None`. |
| 11 | punch_in, list_attendance, create_employee | Responses have an internal `id` and datetimes instead of epoch ms. | Look at any response. | Own output functions, no id, times in ms. |
| 12 | create_employee | Same check-then-insert problem, no unique index, no validation, created_at is naive local time. | joined_on "2026-02-30" is accepted. | Unique index, field patterns and validators, UTC created_at. |
| 13 | list_employees | `skip = page * page_size` skips page 1. total counts everyone, not the filter. No sort. No limits on page and page_size. | page=1 returns the second page. page_size=-1 gives a Mongo error. | `(page - 1) * page_size`, count with the filter, sort by emp_code, limits. |
| 14 | list_attendance | Loads everything into memory and sorts in Python, only by date. No index. | Slow with 100k records. | find with sort, skip, limit, plus indexes. Sort is date desc, emp_code asc. |
| 15 | list_attendance | No validation for dates, status or page size. | date_from after date_to returns 200. | Return 422. |
| 16 | list_attendance | Old records without half_day or history come back with those keys missing. | Sample 9 in DATA_MODEL.md. | Default to false and empty list. |
| 17 | health | Always returns ok. | Stop MongoDB, still 200. | Ping MongoDB, 503 if it fails. |
| 18 | whole file | No indexes at all, every query scans the collection. | explain shows COLLSCAN. | create_indexes() at startup. |

## Looked at, not a problem

- `load_dotenv()` does not override real environment variables, so that is fine.
- The endpoints are normal `def` functions, so FastAPI runs them in threads and blocking pymongo calls are ok.
- The default `mongodb://localhost:27017` is only used when MONGO_URI is not set. I left it for local use.
- `history: []` on punch-in is correct, only corrections write to history.
