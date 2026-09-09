"""Interactive local setup. Never echoes tokens or API keys."""
import getpass
import sys
from pathlib import Path
from dotenv import dotenv_values,set_key,unset_key
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from config import validate_bot_token
from credential_store import read_secret,write_secret

def hidden_value(prompt):
    value=getpass.getpass(prompt).strip()
    if value and (not value.isascii() or not value.isprintable() or any(ch.isspace() for ch in value)):
        raise SystemExit(
            'The pasted value contains a hidden character. No changes saved. '
            'Run setup again and right-click to paste; Ctrl+V may insert a control character '
            'in a hidden Windows terminal prompt.'
        )
    return value

def main():
    path=Path(__file__).resolve().parents[1]/'.env'
    values=dotenv_values(path) if path.exists() else {}
    print('Local setup. Enter leaves an existing valid value unchanged.')
    print('For hidden fields, right-click to paste. Ctrl+V may insert a hidden control character.')
    updates={}
    token=hidden_value('Telegram bot token from @BotFather (hidden): ')
    effective_token=token or values.get('BOT_TOKEN','') or read_secret('BOT_TOKEN') or ''
    try:
        validate_bot_token(effective_token)
    except ValueError as exc:
        raise SystemExit(f'{exc} No changes saved.') from exc
    if token:
        try:
            write_secret('BOT_TOKEN',token)
            if values.get('BOT_TOKEN'): unset_key(str(path),'BOT_TOKEN')
        except OSError:
            updates['BOT_TOKEN']=token
            print('Windows Credential Manager is unavailable in this session; BOT_TOKEN will remain in .env.')
    owner=(input('Your numeric Telegram user ID (not phone number; get it from @userinfobot): ').strip()
           or values.get('OWNER_ID',''))
    if not owner.isdigit() or int(owner)<=0:
        raise SystemExit('A positive Telegram user ID is required. Do not enter a phone number. No changes saved.')
    updates['OWNER_ID']=owner
    key=hidden_value('Gemini API key (hidden, optional): ')
    if key:
        try:
            write_secret('GEMINI_API_KEY',key)
            if values.get('GEMINI_API_KEY'): unset_key(str(path),'GEMINI_API_KEY')
        except OSError:
            updates['GEMINI_API_KEY']=key
            print('Windows Credential Manager is unavailable in this session; GEMINI_API_KEY will remain in .env.')
    model=input('Gemini model ID (optional; use a model available to your key): ').strip()
    if model: updates['AI_MODEL']=model
    if not effective_token:
        raise SystemExit('A bot token is required. No changes saved.')
    updates.setdefault('SHIFT_TIMEZONE',values.get('SHIFT_TIMEZONE') or 'Asia/Kolkata')
    for name,value in updates.items():
        set_key(str(path),name,value)
    print('Configuration saved locally. Secrets use Windows Credential Manager when available.')
    print('Run scripts/run.ps1 to start the bot.')
if __name__=='__main__': main()
