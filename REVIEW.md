## Defects Found

| # | Where (function / line) | What is wrong | How I noticed it (test, input, or symptom) | How I fixed it |
|---|---|---|---|---|
| 1 | `punch_out()` | An already-closed attendance record returned `404 Not Found` instead of the required `409 Conflict`. | Repeated the punch-out request for an employee whose attendance record was already closed. The API returned `404`. | Updated the record lookup and validation logic to distinguish an already-closed record and return `409 Conflict`. |
| 2 | `punch_out()` | The lookup filtered out closed attendance records, making it impossible to distinguish an already-closed record from a missing open punch-in record. | Inspected the query after observing the duplicate punch-out response. | Changed the lookup to inspect the relevant attendance record and added a check for an existing `punch_out` value. |
| 3 | `compute_overtime()` and `shift_end_datetime()` usage | Overtime can be calculated incorrectly when the attendance date does not match the punch timestamps. | Inspected a test record dated `2026-10-12` with punch-in and punch-out timestamps on `2026-10-14`. The API reported `2730` overtime minutes. | Identified inconsistent test data as the immediate cause. The overtime calculation and date handling require verification with consistent attendance dates and timestamps. |

## Reviewed but Not Confirmed as Defects

- **`shift_end_datetime()`**: The function constructs the shift-end timestamp using the attendance date, shift-end time, and overnight-shift handling. The function itself appeared reasonable during inspection. The overtime issue was associated with inconsistent attendance data; further tests with consistent dates are needed.
- **Punch-out duration validation**: Tested a punch-out more than 24 hours after punch-in. The API returned `422 Unprocessable Entity`, as expected.
- **Duplicate punch-out handling**: After the change to `punch_out()`, a repeated punch-out returned `409 Conflict`, as required.
- **Python syntax**: Ran `python -m py_compile app/main.py` successfully after the punch-out change.

## Limitations

This review records the defects and behaviors observed during testing. Other requirements, including rounding edge cases, concurrent updates, and all analytics calculations, should be validated against the complete assignment contract before considering the implementation fully verified.
