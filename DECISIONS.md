# DECISIONS.md

1. **Indexes.** Unique `employees(emp_code)` for 409 races and lookups; `department` and `joined_on` for headcount filters. Unique `attendance_logs(emp_code, date)` for punch-in races; `(date −1, emp_code 1)` for list sort; `date` for month ranges; `status` for status filters; `(emp_code, punch_in −1)` for punch-out search. Considered a text index on names — rejected, only exact matches are used.
2. **Punch-in race.** Both requests compute the same truncated instant and IST date, then `insert_one`. The unique index lets one win; the loser gets `DuplicateKeyError`, mapped to 409. No check-then-insert, so no interleaving window.
3. **Ties.** Pipeline groups totals, then `$setWindowFields` with `$rank` (competition ranking: 1, 2, 2, 4), sorts by total desc plus emp_code, and keeps rows with `rank <= limit`. A tie at the cutoff returns more than `limit` rows, which the contract requires.
4. **Headcount.** The summary aggregates from `employees`, matching `joined_on <= month-end`, then `$lookup`s that month's logs with preserved nulls. Headcount is the size of `$addToSet emp_code`, so zero-log people count and later joiners never enter.
5. **100x data.** Shard `attendance_logs` by `emp_code` hash with `date` as sort key, keep all queries emp- or date-scoped, pre-aggregate monthly rollups via change streams, and move cold months to tiered storage.
