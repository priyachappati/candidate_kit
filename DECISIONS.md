# DECISIONS.md

## 1. Indexes

Four of them, and each one earns its place.

`employees.emp_code` is unique. That is the mechanism behind "duplicate create returns 409". It also makes every employee lookup a single index seek.

`attendance_logs (emp_code, date)` is unique. Same job for punch-in (one record per employee per day), plus it serves punch-out, PATCH, and the employee monthly aggregation.

`attendance_logs (date desc, emp_code asc)` matches the list endpoint's sort exactly. Without it, pagination would sort the whole matching set every time.

`employees.department` covers the department filter on `/employees` and the outer match in the department summary pipeline.

I considered a single-field index on `status` and threw it out: five values, terrible selectivity, never queried alone. Same for `punch_in` alone. I added a compound `(emp_code, punch_out, punch_in desc)` for punch-out because otherwise the "most recent open record" lookup sorts in memory.

## 2. Punch-in race

Two requests hit `/attendance/punch-in` at once. Both see the same employee, both compute the same attendance date, both call `insert_one` with the same `(emp_code, date)`. There is no `find_one` guard in front of the insert; I removed that pattern on purpose.

MongoDB's unique index lets exactly one insert commit. The other raises `DuplicateKeyError`, which I catch and turn into a 409. The winner returns 201. This holds under retries too, because the second insert can never beat the unique index.

## 3. Ties

The pipeline groups by `emp_code`, sums `late_minutes`, then applies `$setWindowFields` with `$rank` sorted by total descending. `$rank` is competition ranking, so 1, 2, 2, 4 is what the server produces without any post-processing.

Then I filter `{ rank: { $lte: limit } }`. The filter is on the rank value, not on the position in the result array. If three employees tie for rank 2 and `limit=2`, the response is four rows: the rank-1 employee plus all three at rank 2. Rows are sorted by `total_late_minutes` desc, then `emp_code` asc, so the order is deterministic.

## 4. Headcount

The department summary pipeline starts on `employees`, not on `attendance_logs`. It matches `joined_on <= last day of month`, groups by department, and only then `$lookup`s into `attendance_logs` for that department's records in the month.

Because the employee side is the outer pipeline, an employee with no logs at all still contributes 1 to `headcount`. Their contribution to `present_days` and `late_count` is 0, and they are excluded from the work-hours average (that average is over records with a `work_hours` value, not over employees). A department with a headcount of 0 after the join is dropped by a final `$match`.

## 5. If this had to serve 100x the data

I would stop computing analytics on the raw log collection. A nightly job would materialise per-employee-per-month summaries into a separate collection, and the read endpoints would hit that.

On top of that: shard `attendance_logs` on hashed `emp_code` to spread both writes and reads, add a covering index on `(date, status, late_minutes)` for the leaderboard and trend, and cache the department summary in Redis with a short TTL that is invalidated on punch and PATCH. Punch-in and punch-out would go through a queue so bursts don't contend on the unique index. Old logs past a retention window move to cold storage.