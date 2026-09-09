"""Small Windows Credential Manager adapter with environment fallback."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

SERVICE = 'TelegramWorkAssistant'


if os.name == 'nt':
    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
            ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
            ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
            ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
            ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
            ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR),
        ]
    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
    _advapi = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(PCREDENTIALW)]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]


def _target(name: str) -> str:
    return f'{SERVICE}/{name}'


def read_secret(name: str) -> str | None:
    if os.name != 'nt':
        return None
    pointer = PCREDENTIALW()
    if not _advapi.CredReadW(_target(name), 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        credential = pointer.contents
        if not credential.CredentialBlob or not credential.CredentialBlobSize:
            return ''
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return blob.decode('utf-16-le')
    finally:
        _advapi.CredFree(pointer)


def write_secret(name: str, value: str) -> None:
    if os.name != 'nt':
        raise RuntimeError('Windows Credential Manager is only available on Windows.')
    blob = value.encode('utf-16-le')
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = CREDENTIALW()
    credential.Type = 1
    credential.TargetName = _target(name)
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    credential.UserName = os.environ.get('USERNAME', 'local-user')
    if not _advapi.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_secret(name: str) -> None:
    if os.name != 'nt':
        return
    if not _advapi.CredDeleteW(_target(name), 1, 0) and ctypes.get_last_error() != 1168:
        raise ctypes.WinError(ctypes.get_last_error())


def configured_secret(env_name: str, credential_name: str | None = None) -> str:
    value = os.getenv(env_name, '')
    if value:
        return value
    try:
        return read_secret(credential_name or env_name) or ''
    except OSError:
        return ''
