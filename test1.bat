bash

cd /home/claude/project && python3 -c "
from database import Database
from models import TaskStatus
import reports

db = Database(':memory:') if False else Database('/tmp/test_tasks.db')
import os
if os.path.exists('/tmp/test_tasks.db'):
    os.remove('/tmp/test_tasks.db')
db = Database('/tmp/test_tasks.db')

t1 = db.add_task('Task A')
t2 = db.add_task('Task B')
t3 = db.add_task('Task C')

print('--- BOS ---')
print(reports.generate_bos(db.list_tasks()))

db.mark_status(t1.id, TaskStatus.COMPLETED)
db.mark_status(t2.id, TaskStatus.IN_PROGRESS)

print()
print('--- PRELUNCH ---')
print(reports.generate_prelunch(db.list_tasks()))

db.mark_status(t2.id, TaskStatus.BLOCKED, 'Waiting for Dev')

print()
print('--- EOD ---')
print(reports.generate_eod(db.list_tasks()))

print()
print('--- list_pending ---')
for t in db.list_pending():
    print(t.id, t.title, t.status)

os.remove('/tmp/test_tasks.db')
print()
print('ALL TESTS RAN OK')
"
Output

--- BOS ---
Beginning of Shift
• Task A
• Task B
• Task C

--- PRELUNCH ---
Pre Lunch
Completed
• Task A
In Progress
• Task B
Remaining
• Task C

--- EOD ---
End of Day
Completed
• Task A
Blocked
• Task B — Waiting for Dev
Carry Forward
• Task C

--- list_pending ---
3 Task C TaskStatus.PENDING

ALL TESTS RAN OK