---
status: active
type: reference
---
# Deadlines

The deadline sensor (`scripts/sensor_deadlines.py`, run by the job runner) reads this note and puts anything due soon on the work list. One line per item:

`- 2026-11-02 09:00 | what is due | tier | value`

The time is optional. Tier is 0 to 2 (default 1). Value is 1 to 5 (default 3): the closer the deadline, the higher it is raised. Mark an item finished with `- [x]` at the start of the line.

(no deadlines yet)
