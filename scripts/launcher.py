"""Small Windows control panel for the personal bot."""
import subprocess
import sys
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from database import Database
from maintenance import create_rotating_backup

TASK = 'Telegram Work Assistant'


def powershell(command):
    return subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True, text=True, timeout=20)


def main():
    database = Database(config.DB_PATH)
    root = tk.Tk(); root.title('Telegram Work Assistant'); root.geometry('430x280')
    status = tk.StringVar(value='Ready')

    def run_task(action):
        result = powershell(f'{action}-ScheduledTask -TaskName "{TASK}"')
        status.set('Command completed.' if result.returncode == 0 else (result.stderr.strip() or 'Command failed.'))

    def open_dashboard():
        token = database.get_setting('dashboard_token')
        if not token:
            messagebox.showinfo('Dashboard', 'Start the bot once so it can create the dashboard token.')
            return
        webbrowser.open(f'http://127.0.0.1:{config.DASHBOARD_PORT}/?token={token}')

    def backup():
        path, _ = create_rotating_backup(database, config.BASE_DIR/'storage'/'backups',
                                         config.BACKUP_RETENTION, 'manual')
        status.set('Backup created: ' + path.name)

    tk.Label(root, text='Telegram Work Assistant', font=('Segoe UI', 17, 'bold')).pack(pady=18)
    tk.Button(root, text='Open local dashboard', width=30, command=open_dashboard).pack(pady=4)
    tk.Button(root, text='Start bot', width=30, command=lambda: run_task('Start')).pack(pady=4)
    tk.Button(root, text='Stop bot', width=30, command=lambda: run_task('Stop')).pack(pady=4)
    tk.Button(root, text='Create backup', width=30, command=backup).pack(pady=4)
    tk.Label(root, textvariable=status, wraplength=390, fg='#425466').pack(pady=15)
    root.mainloop()


if __name__ == '__main__':
    main()
