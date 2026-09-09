"""Restore only while the bot is stopped. Preserve current database first."""
import sys
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import config
from database import Database, SCHEMA_VERSION
from runtime import instance_lock

def main():
    source=Path(sys.argv[1]).resolve()
    target=Path(config.DB_PATH).resolve()
    if not source.is_file() or source==target:
        raise SystemExit('Choose an existing backup different from the active database.')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise SystemExit('Backup integrity check failed.')
        if c.execute('PRAGMA user_version').fetchone()[0] not in (1,SCHEMA_VERSION):
            raise SystemExit('Unsupported backup schema version.')
        tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'tasks','shifts','activities','reports','settings'}<=tables:
            raise SystemExit('This is not a work assistant backup.')
        if target.exists():
            Database(target).backup(target.parent/'backups'/('before-restore-'+datetime.now().strftime('%Y%m%d%H%M%S%f')+'.sqlite3'))
        target.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(target)) as out:
            c.backup(out)
    print('Restored. The previous database was backed up if it existed.')
if __name__=='__main__':
    with instance_lock(config.DB_PATH):
        main()
