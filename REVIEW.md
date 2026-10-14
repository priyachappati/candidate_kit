# REVIEW.md

Here is what I found wrong in the starter `app/main.py`, in the order I hit it. Most of these I only caught because I wrote small tests against each rule from `openapi.yaml`.

## Helpers

**1. `/health` never touches Mongo.**
The starter just returns `{"status": "ok"}` no matter what. If Mongo is down, the readiness poll passes and every other test blows up. It needs to actually ping.

Fix: `client.admin.command("ping")` inside a try, 200 on success, 503 on any exception.

**2. `compute_late_minutes` has no grace period.**
The starter does `minutes if minutes > 10 else 0`. That is close, but not what R2 says. R2 is strict: a punch-in is late only if it is strictly more than 10:00 after shift_start. Punch at 09:40:00 exactly is not late. Punch at 09:40:01 is late, and `late_minutes` is the whole elapsed minutes since shift_start (10), not since the grace ended.

It also works on naive local time, which is wrong on any machine not set to IST.

Fix: convert punch_in to IST first, compute seconds since shift_start, return 0 if <= 600, else floor of the minutes.

**3. `compute_work_hours` uses `round()`.**
Python's `round` is banker's rounding. R4 wants half-up. `round(2.675, 2)` gives 2.67, R4 wants 2.68. Fix is to use `Decimal` with `ROUND_HALF_UP`.

**4. `compute_overtime` counts any positive minutes.**
R3 says overtime only counts when the gap between shift_end and punch_out is at least 30 minutes. The starter returns 5 when someone leaves 5 minutes late. Fix is `mins if mins >= 30 else 0`.

**5. `compute_overtime` uses the wrong day for overnight shifts.**
The starter builds the shift_end instant from the attendance date. For a 22:00-06:00 shift that is wrong; the shift ends the next calendar day. Fix: if `shift_end <= shift_start`, add one day to the shift end before subtracting.

**6. `compute_overtime` mixes naive and aware datetimes.**
`datetime.fromisoformat(date_str)` is naive; `.replace(tzinfo=IST)` tags it IST; but punch_out from Mongo is naive UTC. Subtracting them gives nonsense. I hit this with an overnight employee and the overtime number was off by exactly 5h30m.

Fix: convert both operands to aware IST before subtracting.

**7. No truncation to whole seconds.**
R1 is explicit about this. The starter stores whatever the client sends, milliseconds and all. I noticed because a punch sent with `punched_at=1783312500123` came back with the same millisecond value via epoch. Fix: `dt.replace(microsecond=0)` on every instant at the API boundary, before any calculation and before storing.

## Employees

**8. `create_employee` checks then inserts.**
Classic race. Two parallel POSTs with the same `emp_code` both see "no existing employee", both insert, two documents. I fired 5 parallel requests with the same code and got 5 × 201.

Fix: rely on the unique index on `emp_code`. Wrap `insert_one` in try/except `DuplicateKeyError`, return 409.

**9. `create_employee` writes a naive local `created_at`.**
`datetime.now()` gives local time without a timezone. That number in epoch ms is wrong unless the server runs in UTC. Fix: `datetime.now(timezone.utc)`.

**10. `create_employee` does not check `shift_start != shift_end`.**
The contract says they must differ. The starter accepts them equal. Fix: 422 before insert.

**11. No unique index at startup.**
Without it, #8 cannot be caught. Fix: `create_index([("emp_code", 1)], unique=True)` at startup.

## Pagination

**12. `list_employees` skip is off by one page.**
`skip = page * page_size` means `?page=1` starts at item 21, not item 1. Fix: `(page - 1) * page_size`.

**13. `list_employees` reports `total` on the whole collection.**
R10 says `total` must reflect the filters. `?department=Sales` returns 2 items but `total: 6`. Fix: `count_documents(q)`.

**14. `list_employees` does not bound `page` / `page_size`.**
R10 caps `page_size` at 100 and `page` starts at 1. The starter accepts `page_size=10000`. Fix with `Query(1, ge=1)` and `Query(20, ge=1, le=100)`.

## Punch-in

**15. `punch_in` throws 500 on unknown employee.**
`emp["shift_start"]` on None is a `KeyError`. Fix: explicit 404.

**16. `punch_in` uses local date for the attendance day.**
R1 says the attendance date is the IST calendar date of the punch. The starter uses `ts.date()` on a naive local datetime. Fix: convert to IST, apply the overnight rule, use that date.

**17. `punch_in` ignores the overnight rule entirely.**
An overnight employee punching in at 02:00 IST belongs to the previous day's shift. The starter dates it today. Fix: if `shift_end <= shift_start` and the IST time is before shift_end, subtract one day.

**18. `punch_in` is not race-safe.**
Two parallel requests for the same (emp_code, date) both pass the check. Fix: same unique index as #11, catch `DuplicateKeyError`, return 409.

**19. `punch_in` returns an `"id"` field.**
It is not in the contract, and `_id` is meant to stay internal. Fix: removed.

**20. `punch_in` does not validate `punched_at`.**
Seconds-looking values, floats, and strings must be 422. I sent `12345` and it was accepted. Fix: Pydantic bounds `ge=100_000_000_000, le=4_102_444_800_000`.

**21. `punch_in` accepts any status string.**
Only PRESENT, WFH, ON_DUTY are valid here. Fix: enum validator.

**22. `punch_in` does not initialize all derived fields.**
The spec requires the full shape even when the punch is fresh. Fix: write `late_minutes`, `work_hours=None`, `overtime_minutes=0`, `half_day=False`, `history=[]`.

## Attendance list

**23. `list_attendance` loads everything into memory.**
`list(db.attendance_logs.find(q))` reads the whole result set, then sorts and slices in Python. At 100k docs this is unusable and also burns memory. Fix: filter, sort, skip, limit at the database. `count_documents` for `total`.

**24. `list_attendance` sorts in Python.**
No stable tiebreak. Fix: `.sort([("date", -1), ("emp_code", 1)])` in the query.

**25. `list_attendance` returns `"id"`.**
Not in the contract. Fix: removed.

**26. `list_attendance` does not reject `date_from > date_to`.**
Spec says 422. Starter returns an empty list silently. Fix: explicit 422.

**27. `list_attendance` does not bound `page` / `page_size` and does not validate `status`.**
`?page_size=10000` and `?status=FOO` both go through. Fix with Query bounds and a status check.

## Global

**28. No indexes created at startup.**
The grader seeds ~100k records with only the default `_id` index. Every query would COLLSCAN. Fix: create the indexes we need at startup, idempotently.

**29. PyMongo returns naive datetimes by default.**
Every date value read from Mongo lacks a timezone. Any comparison against IST anywhere is silently wrong. Fix: `MongoClient(..., tz_aware=True)`.

**30. No environment validation.**
If `.env` is missing and the real env vars aren't set, the app starts and then fails somewhere deep. Fix: read `MONGO_URI` and `MONGO_DB` at import, raise immediately if either is missing.

## Things I looked at and decided were fine

- FastAPI's default 422 body. The contract uses it as-is.
- `_id` stays in Mongo; it is dropped at the API boundary via projection. Not a bug.
- `history` missing on legacy docs is expected; treat as empty.
- `half_day` missing on legacy docs is expected; treat as false.
- `history: []` for records that were never corrected. Correct.
- The 404 at `/` for the browser root and `/favicon.ico` is not a defect. Neither route is in `openapi.yaml`.