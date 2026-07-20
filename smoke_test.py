from pathlib import Path
import json
import config
from database import Database
import reports
from models import TaskStatus

p = Path(config.DB_PATH)
p.parent.mkdir(parents=True, exist_ok=True)
# reset storage for deterministic test
p.write_text(json.dumps({"tasks": [], "settings": {}}, indent=2, ensure_ascii=False), encoding='utf-8')

print('STORAGE:', config.DB_PATH)

db = Database(config.DB_PATH)

t1 = db.add_task('Perform idle claim testing')
t2 = db.add_task('Test localization')

db.mark_status(t2.id, TaskStatus.COMPLETED)

print('\nAll tasks:')
for t in db.list_tasks():
    print(f"{t.id}. {t.title} [{t.status}]")

print('\nPending tasks (list_tasks with PENDING):')
for t in db.list_tasks(TaskStatus.PENDING):
    print(f"{t.id}. {t.title}")

print('\nBOS report:')
print(reports.generate_bos(db.list_tasks()))

print('\nPre Lunch report:')
print(reports.generate_prelunch(db.list_tasks()))

print('\nEOD report:')
print(reports.generate_eod(db.list_tasks()))
