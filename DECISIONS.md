# Design decisions

1. I created unique indexes on employee code and attendance `(emp_code, date)` for identity and race safety. Other compound indexes cover employee/department listing, punch-out lookup, date/status listing, and monthly analytics. I rejected one very wide attendance index because it would increase write cost without matching every query order.

2. Two equal punch-ins both attempt an insert. MongoDB's unique attendance index accepts one insert and raises `DuplicateKeyError` for the other, which becomes HTTP 409. There is no check-then-insert race.

3. The leaderboard uses MongoDB `$rank`. Employees tied at the cutoff receive the same competition rank, so all rows whose rank is within `limit` are returned, ordered by employee code within the tie.

4. Department summary starts from eligible employees and looks up their monthly logs. Therefore an employee contributes to the headcount even when the lookup returns no records; joining after month-end excludes them.

5. At 100 times the data, I would pre-aggregate immutable daily records into monthly department and employee summaries. Punch writes would remain authoritative, while a resumable worker would update summary documents and reconciliation jobs would detect drift.
