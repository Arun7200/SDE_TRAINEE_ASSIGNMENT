# DECISIONS.md

1. **Indexes.** attendance_logs: unique (emp_code, date) so a day can't be duplicated and per-employee lookups are fast; (date, emp_code) for the list and the leaderboard; (status, date, emp_code) for status filters; (emp_code, punch_in) to find the open record on punch-out. employees: unique emp_code, (department, joined_on) and (joined_on). I didn't add one on late_minutes because the leaderboard filters by date first.

2. **Punch-in race.** Both requests work out the same date and try to insert. The unique index lets only one in, the other gets a duplicate key error and I return 409. I don't read first.

3. **Ties.** Rank is worked out before the limit is applied, so everyone tied at the cutoff is returned. It can be more rows than the limit.

4. **Headcount.** The summary starts from the employees collection (joined on or before month end) and adds the logs after, so someone with no logs still counts.

5. **100x data.** Keep a monthly summary per employee, updated on each punch or correction, so analytics don't read all the raw logs.
