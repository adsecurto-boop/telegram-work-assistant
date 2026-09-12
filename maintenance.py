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
    """Delete retained media files only; the audit metadata remains in SQLite.

    Safety order:
    1. Collect candidate paths and null the DB paths inside the transaction.
    2. Commit (transaction exits).
    3. Delete files from disk AFTER the commit.

    This means: if file deletion fails or the process crashes after commit, the
    DB is still consistent (path=NULL) — files are orphaned but recoverable.
    If the process crashes before commit, both DB and files remain intact.
    """
    evidence_dir = Path(directory).resolve()
    if not evidence_dir.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat()

    # Step 1: Collect candidates and nullify in DB (transactional).
    to_delete: list[Path] = []
    with database.connect() as connection:
        rows = connection.execute('''SELECT id,path FROM evidence
            WHERE path IS NOT NULL AND created_at<?''', (cutoff,)).fetchall()
        for row in rows:
            candidate = Path(row['path']).resolve()
            try:
                candidate.relative_to(evidence_dir)
            except ValueError as exc:
                raise RuntimeError('Refusing to prune evidence outside the configured directory.') from exc
            # Null the DB path first — this commits when the 'with' block exits.
            connection.execute('UPDATE evidence SET path=NULL WHERE id=?', (row['id'],))
            to_delete.append(candidate)
    # --- Transaction committed above ---

    # Step 2: Delete files from disk after the DB commit.
    removed = []
    for candidate in to_delete:
        if candidate.is_file():
            try:
                candidate.unlink()
                removed.append(candidate)
            except OSError:
                # Log but don't raise — DB is already consistent.
                import logging
                logging.getLogger(__name__).warning(
                    'Failed to delete evidence file %s (DB path already nulled)', candidate)
    return removed
