"""Automatic SQLite backups with bounded local retention."""
from datetime import datetime, timedelta, timezone
from pathlib import Path


def create_rotating_backup(database, directory, retention, prefix='auto'):
    backup_dir = Path(directory).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f'{prefix}-{datetime.now():%Y%m%d-%H%M%S%f}.sqlite3'
    database.backup(destination)
    files = sorted(backup_dir.glob('*.sqlite3'), key=lambda path: path.stat().st_mtime, reverse=True)
    removed = []
    for old in files[max(2, int(retention)):]:
        resolved = old.resolve()
        if resolved.parent != backup_dir or resolved == database.path.resolve():
            raise RuntimeError('Refusing to prune a backup outside the configured backup directory.')
        resolved.unlink()
        removed.append(resolved)
    return destination, removed


def prune_evidence_files(database, directory, retention_days):
    """Delete retained media files only; the audit metadata remains in SQLite."""
    evidence_dir = Path(directory).resolve()
    if not evidence_dir.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat()
    with database.connect() as connection:
        rows = connection.execute('''SELECT id,path FROM evidence
            WHERE path IS NOT NULL AND created_at<?''', (cutoff,)).fetchall()
        removed = []
        for row in rows:
            candidate = Path(row['path']).resolve()
            try:
                candidate.relative_to(evidence_dir)
            except ValueError as exc:
                raise RuntimeError('Refusing to prune evidence outside the configured directory.') from exc
            if candidate.is_file():
                candidate.unlink()
                removed.append(candidate)
            connection.execute('UPDATE evidence SET path=NULL WHERE id=?', (row['id'],))
        return removed
