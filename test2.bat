bash

cd /home/claude/project && pip install python-telegram-bot==21.4 APScheduler==3.10.4 python-dotenv==1.0.1 --break-system-packages -q 2>&1 | tail -20 && python3 -c "
import ast, sys
for f in ['config.py','models.py','database.py','reports.py','utils.py','handlers.py','scheduler.py','bot.py']:
    with open(f) as fh:
        src = fh.read()
    try:
        ast.parse(src)
        print(f, 'OK syntax')
    except SyntaxError as e:
        print(f, 'SYNTAX ERROR', e)
"
Output

ERROR: Could not find a version that satisfies the requirement python-telegram-bot==21.4 (from versions: none)
ERROR: No matching distribution found for python-telegram-bot==21.4
config.py OK syntax
models.py OK syntax
database.py OK syntax
reports.py OK syntax
utils.py OK syntax
handlers.py OK syntax
scheduler.py OK syntax
bot.py OK syntax