# DECISIONS.md

## 1. Indexes

I use a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code` and `attendance_logs.date`. I also use indexes supporting attendance date ordering, employee/month queries, status filters, and department-related lookups. These reduce the amount of data MongoDB must scan. I considered relying on application-side duplicate checks, but rejected that approach because concurrent requests can pass the check simultaneously.

## 2. Concurrent punch-ins

Both requests may initially find no attendance record. The unique compound index is the final authority: MongoDB permits only one insert for the employee and date. The successful request returns 201, while the duplicate-key failure is translated to 409. This remains safe even when requests arrive simultaneously.

## 3. Leaderboard ties

I use competition ranking based on total late minutes. Equal totals receive the same rank, and the next rank is skipped, so ranks can be 1, 2, 2, 4. I apply the limit after calculating ranks and keep all employees tied at the cutoff rank.

## 4. Employees without logs

The department summary starts from eligible employees, including those whose `joined_on` is on or before the month's final day. Attendance metrics are combined with those employees, so having no logs does not remove someone from headcount.

## 5. Scaling further

For 100 times more data, I would measure execution plans and workload first, then consider archival policies, carefully designed compound indexes, and workload-specific aggregation optimizations.