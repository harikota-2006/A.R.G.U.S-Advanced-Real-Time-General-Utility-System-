"""Signed ntfy command envelopes; normal notes remain inert inbox content.

Run on the phone in Termux: python phone_commands.py "detective mode".
The phone needs NTFY_RX_URL, NTFY_COMMAND_KEY and optionally NTFY_TOKEN.
The hub must already have an unlocked, authenticated HUD session.
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import secrets
import time
from config import env, ntfy_url

REMOTE_COMMANDS = {
    'study mode', 'detective mode', 'normal mode', 'show system health',
    'close chart', 'how much have we covered',
}

class RemoteCommandDenied(PermissionError):
    """A valid envelope requested an operation reserved for the local HUD."""


def _canonical(payload: dict) -> bytes:
    return json.dumps({key: payload[key] for key in ('command', 'timestamp', 'nonce')},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sign_command(command: str, key: str, *, timestamp: int | None = None, nonce: str | None = None) -> dict:
    if len(key) < 32:
        raise ValueError('NTFY_COMMAND_KEY must contain at least 32 characters')
    if not isinstance(command, str) or not 1 <= len(command.strip()) <= 8000:
        raise ValueError('Command must contain 1–8000 characters')
    payload = {'command': command.strip(), 'timestamp': int(time.time()) if timestamp is None else timestamp,
        'nonce': nonce or secrets.token_hex(16)}
    payload['signature'] = hmac.new(key.encode(), _canonical(payload), hashlib.sha256).hexdigest()
    return payload


def verify_command(message: str) -> tuple[str, str] | None:
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or 'command' not in payload:
        return None
    key = env('NTFY_COMMAND_KEY')
    if len(key) < 32:
        raise ValueError('Signed phone commands are not configured')
    if not isinstance(payload.get('command'), str) or not 1 <= len(payload['command'].strip()) <= 8000:
        raise ValueError('Invalid command text')
    if not isinstance(payload.get('timestamp'), int) or abs(time.time() - payload['timestamp']) > 300:
        raise ValueError('Expired phone command')
    nonce = payload.get('nonce')
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
        raise ValueError('Invalid phone command nonce')
    expected = hmac.new(key.encode(), _canonical(payload), hashlib.sha256).hexdigest()
    if not isinstance(payload.get('signature'), str) or not hmac.compare_digest(expected, payload['signature']):
        raise ValueError('Invalid phone command signature')
    command = ' '.join(payload['command'].lower().split()).rstrip('.?')
    if command not in REMOTE_COMMANDS:
        raise RemoteCommandDenied('This command requires a local tablet session')
    return nonce, command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command')
    args = parser.parse_args()
    url = ntfy_url('RX')
    if not url.startswith('https://'):
        raise SystemExit('Set an HTTPS NTFY_RX_URL in the phone .env')
    payload = sign_command(args.command, env('NTFY_COMMAND_KEY'))
    headers = {'Title': 'ARGUS signed command'}
    if env('NTFY_TOKEN'):
        headers['Authorization'] = 'Bearer ' + env('NTFY_TOKEN')
    import httpx
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post(url, content=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
        response.raise_for_status()
    print('Command delivered to the inbox. An unlocked tablet session is required to execute it.')


if __name__ == '__main__':
    main()
