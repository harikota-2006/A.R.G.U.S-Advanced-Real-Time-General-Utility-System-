"""Interactive local setup. Never prints provider keys, PINs, or passwords."""
from __future__ import annotations
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
from datetime import datetime, timezone
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent

def _hash(value):
    salt = secrets.token_bytes(16)
    enc = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    digest = hashlib.pbkdf2_hmac('sha256', value.encode(), salt, 600000)
    return f'pbkdf2_sha256$600000${enc(salt)}${enc(digest)}'

def _write(path, values):
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        shutil.copy2(path, path.with_name(path.name + '.' + stamp + '.bak'))
    temporary = path.with_suffix('.tmp')
    # Single quoted dotenv strings retain $ in password hashes without interpolation.
    text = '\n'.join(key + "='" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'" for key, value in values.items() if value is not None)
    temporary.write_text(text + '\n', encoding='utf-8')
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-env', type=Path)
    parser.add_argument('--laptop', action='store_true')
    parser.add_argument('--rules-import', type=Path)
    parser.add_argument('--rules-export', type=Path)
    args = parser.parse_args()
    if args.rules_import or args.rules_export:
        from database import db
        if args.rules_import:
            print(f'Imported {db.import_rules(args.rules_import)} rules.')
        if args.rules_export:
            print('Exported rules to', db.export_rules(args.rules_export))
        return
    from dotenv import dotenv_values
    old = dict(dotenv_values(BASE / '.env', interpolate=False)) if (BASE / '.env').exists() else {}
    if args.legacy_env:
        old = {**dict(dotenv_values(args.legacy_env, interpolate=False)), **{k:v for k,v in old.items() if v}}
    if args.laptop:
        from laptop_daemon import validate_hub_url
        hub = input('Tablet WebSocket URL (press Enter to keep the current address): ').strip() or old.get('ARGUS_HUB_WS', '')
        validate_hub_url(hub)
        secret = getpass.getpass('Shared ARGUS_DAEMON_KEY (press Enter to keep the current key): ').strip() or old.get('ARGUS_DAEMON_KEY') or old.get('ARGUS_WORKSTATION_TOKEN', '')
        if len(secret) < 32:
            raise SystemExit('The shared key must have at least 32 characters.')
        values = {'ARGUS_HUB_WS': hub, 'ARGUS_DAEMON_KEY': secret}
        values.update({k:old[k] for k in ('ARGUS_DOCUMENTS','ARGUS_WORKSPACE','ARGUS_APP_PATHS') if old.get(k)})
        _write(BASE / '.env', values)
        print('Laptop .env saved. Run python laptop_daemon.py.')
        return
    allowed = set(dotenv_values(BASE / '.env.example', interpolate=False))
    allowed.update(('ARGUS_DB_PATH','ARGUS_DATA_DIR','GMAIL_USER','GMAIL_APP_PASSWORD',
                    'GEMINI_VISION_MODEL','TTS_PITCH','TTS_RATE'))
    values = {key: value for key, value in old.items() if key in allowed and value}
    values.setdefault('TABLET_HOST', '0.0.0.0')
    values.setdefault('TABLET_PORT', '8000')
    values.setdefault('ARGUS_ORIGIN', 'http://localhost:8000')
    origin = input(f"HUD origin [{values['ARGUS_ORIGIN']}]: ").strip() or values['ARGUS_ORIGIN']
    parsed = urlsplit(origin)
    if parsed.hostname != 'localhost' and (parsed.scheme != 'https' or not parsed.hostname or parsed.hostname.replace('.', '').isdigit()):
        raise SystemExit('Use http://localhost:8000 or an HTTPS hostname for microphone access.')
    values['ARGUS_ORIGIN'] = origin.rstrip('/')
    if not values.get('NTFY_TX_URL') and old.get('NTFY_TOPIC'):
        values['NTFY_TX_URL'] = (old.get('NTFY_BASE_URL') or 'https://ntfy.sh').rstrip('/') + '/' + old['NTFY_TOPIC'].strip('/')
    for key in ('NTFY_TX_URL', 'NTFY_RX_URL'):
        if not values.get(key):
            values[key] = input(f'{key} (leave blank to disable): ').strip()
    if values.get('NTFY_TX_URL') and values.get('NTFY_TX_URL') == values.get('NTFY_RX_URL'):
        raise SystemExit('RX and TX must be separate topics to avoid feedback loops.')
    if not values.get('ARGUS_PIN_HASH'):
        pin = old.get('ARGUS_PIN') or old.get('PASSKEY_PIN') or old.get('GATE2_PIN') or old.get('GATE1_PIN')
        if not pin:
            pin = getpass.getpass('Choose your numeric PIN: ')
            if pin != getpass.getpass('Repeat PIN: '):
                raise SystemExit('PINs did not match.')
        if not pin.isascii() or not pin.isdigit():
            raise SystemExit('The configured PIN must contain digits.')
        values['ARGUS_PIN_HASH'] = _hash(pin)
    if not values.get('ARGUS_DAEMON_KEY') and old.get('ARGUS_WORKSTATION_TOKEN'):
        values['ARGUS_DAEMON_KEY'] = old['ARGUS_WORKSTATION_TOKEN']
    for key in ('ARGUS_SESSION_SECRET', 'ARGUS_DAEMON_KEY'):
        if not values.get(key):
            values[key] = secrets.token_urlsafe(48)
        elif len(values[key]) < 32:
            raise SystemExit(f'{key} must have at least 32 characters. Update the configured key explicitly.')
    for key in ('ARGUS_PIN', 'ARGUS_PASSWORD', 'PASSKEY_PIN'):
        values.pop(key, None)
    _write(BASE / '.env', values)
    print('Saved .env with the existing PIN protected by a salted hash. Any previous .env was backed up.')
    print('Open the HUD, speak the displayed phrase, then enter your configured PIN.')
    print('Copy only ARGUS_DAEMON_KEY to the laptop setup. Provider keys stay on the tablet.')
    print('Start: python server.py  |  HUD: ' + values['ARGUS_ORIGIN'])

if __name__ == '__main__':
    main()
