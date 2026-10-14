# Design Decisions

## 1. Indexes

I use MongoDB indexes to support employee lookups and attendance queries. The employee code should be unique so the same employee cannot be registered twice. Attendance queries frequently filter by employee code and date, so a compound index on these fields helps retrieve records efficiently. A unique compound index on employee code and attendance date also prevents duplicate daily attendance records.

## 2. Punch-in Race

Two identical punch-in requests can arrive at the same time. Application-level checks alone are not enough because both requests could pass before either inserts a record. A unique compound index on employee code and date provides database-level protection. One insert succeeds; the other must be handled as a duplicate conflict.

## 3. Leaderboard Ties

I use MongoDB ranking functionality so employees with equal late-minute totals receive the same rank. Tied employees should not be arbitrarily treated as having different performance. Sorting and limiting must follow the assignment's specified leaderboard behavior.

## 4. Department Headcount

The department summary must include employees who have no attendance logs. I use the employee collection as the basis for headcount and combine attendance data through aggregation, rather than counting only employees who appear in attendance records.

## 5. Scaling

If the dataset grew by 100 times, I would review query execution plans, verify indexes against real query patterns, and measure aggregation performance. I would also add load testing and monitoring before considering caching or precomputed analytics.
