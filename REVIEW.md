# REVIEW.md

## Review of `app/main.py`

I reviewed the starter application for correctness, validation, concurrency, timezone handling, database performance, and consistency with `openapi.yaml`.

| # | Where | What was wrong | How to notice | How fixed |
|---|---|---|---|---|
| 1 | Timestamp helper | Timestamp values must be validated as integer epoch milliseconds, not accepted as seconds, floats, or strings. | Send a timestamp in seconds or as a string; expect HTTP 422. | Validate the input type and permitted range before converting it. |
| 2 | Timestamp conversion | Naive MongoDB datetimes can be interpreted incorrectly if timezone handling is inconsistent. | Convert an instant between MongoDB storage and the API response and compare the epoch milliseconds. | Use a consistent UTC conversion strategy at the database boundary. |
| 3 | Attendance date calculation | Attendance dates must use the IST calendar date of punch-in. | Punch in near midnight UTC and check the resulting attendance date. | Derive the date in Asia/Kolkata after truncating the instant to whole seconds. |
| 4 | Shift calculation | Overnight shifts require special handling when the shift end is earlier than or equal to the shift start. | Test a 22:00–06:00 shift with a punch-in at 02:00 IST. | Determine the applicable shift start and end dates before calculating lateness or overtime. |
| 5 | Late calculation | The ten-minute grace period is strict, and late minutes are measured from shift start. | Test 09:40:00, 09:40:01, and 10:15:59 for a 09:30 shift. | Apply the strict grace-period comparison and floor whole minutes from shift start. |
| 6 | Punch-in endpoint | Concurrent requests must not create duplicate attendance records. | Send multiple simultaneous punch-ins for the same employee and date. | Enforce uniqueness with a MongoDB compound index and handle duplicate-key conflicts as HTTP 409. |
| 7 | Punch-in validation | Only PRESENT, WFH, and ON_DUTY are valid punch-in statuses. | Submit ABSENT or LEAVE to the punch-in endpoint. | Validate the permitted status values before inserting a record. |
| 8 | Punch-out endpoint | Punch-out must locate the correct open record and reject invalid or duplicate punch-outs. | Try an unknown employee, a missing punch-in, a repeated punch-out, and a punch-out before punch-in. | Validate the record and timestamp, then update the open record atomically. |
| 9 | Derived attendance fields | Work hours, overtime, and half-day status must follow the specified rounding and threshold rules. | Test work durations around 4.50 hours and overtime around 30 minutes. | Calculate work hours using half-up rounding and apply the exact overtime and half-day thresholds. |
| 10 | Attendance listing | Filtering, ordering, pagination, and total counts must be consistent. | Combine date and status filters and request multiple pages. | Apply filters and sorting in MongoDB before pagination, and calculate the total for the filtered query. |
| 11 | Regularization endpoint | Corrections must preserve an audit trail and must not silently overwrite concurrent edits. | Submit a no-op correction or issue concurrent PATCH requests. | Validate actual changes and use an atomic update strategy that preserves history; reject no-op changes with HTTP 422. |
| 12 | Monthly analytics | Monthly totals must follow working-day, join-date, presence, and half-day rules. | Test a mid-month joiner, a weekend attendance record, and an employee with no logs. | Use a MongoDB aggregation pipeline and apply the contract's date and presence rules. |
| 13 | Department summary | Department data comes from employees, and headcount includes eligible employees without attendance records. | Add an employee who joined before month-end but has no logs. | Aggregate from the employee collection and combine attendance metrics without excluding zero-log employees. |
| 14 | Late leaderboard | Ties require competition ranking, and the limit must be applied after ranking. | Create tied late-minute totals at the requested cutoff. | Rank using the appropriate MongoDB window function and apply the limit after calculating ranks. |
| 15 | Department trend | The response must include every date in the range, including weekends and dates without records. | Query a period containing dates with no attendance logs. | Use MongoDB date densification and window calculations to fill gaps and compute the moving average. |
| 16 | Database indexes | Queries over a large attendance collection must avoid unnecessary collection scans. | Run the explain endpoint against representative filters and inspect the execution plan. | Create the required indexes at startup and verify query plans with execution statistics. |

## Items checked

I also checked the endpoint contracts, HTTP status codes, response field names, date and timestamp formats, and pagination defaults against `openapi.yaml`. These should be retained wherever the implementation already complies with the contract. The final defect list should include only defects verified in the starter code and should describe the corresponding fixes actually made.