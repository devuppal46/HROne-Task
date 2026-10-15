# DECISIONS.md

1. **Indexes.**
   - `employees`: `emp_code` unique (uniqueness and joins); `(department, emp_code)` for the sorted list; `(department, joined_on)` and `joined_on` for the summary and trend.
   - `attendance_logs`: `(emp_code, date)` unique (one record per day, race-safe punch-in, monthly stats, joins); `(date desc, emp_code)`, which matches the list sort and serves month ranges; `(status, date desc, emp_code)` for status filters.
   - Rejected: an index on `late_minutes`. The date index already narrows the leaderboard's slice.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index admits exactly one; the other gets `DuplicateKeyError`, which becomes 409. There is no read-then-write window.

3. **Ties.** `$rank` sorts on the total only, so ties share a rank and the next rank is skipped. Filtering `rank <= limit` afterwards returns everyone tied at the cutoff.

4. **Headcount.** The summary starts from `employees` (`joined_on` <= month end). `$lookup` with `preserveNullAndEmptyArrays` keeps employees who have no logs.

5. **100x the data.** Precompute daily rollups when punches are written, and shard logs on `emp_code`.
