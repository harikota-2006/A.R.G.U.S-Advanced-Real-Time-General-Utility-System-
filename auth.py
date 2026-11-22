"""Spoken phrase and existing PIN login, with one-use challenges and signed sessions."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
import re

from config import env
from database import db

PHRASE = "I solemnly swear that I'm up to no good."

def phrase_matches(value):
    normalized = value.casefold().replace("’", chr(39)).replace(chr(39), "")
    words = re.findall(r"[a-z]+", normalized)
    return " ".join(words).replace("i am ", "im ") == "i solemnly swear that im up to no good"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))

def hash_secret(value: str, iterations: int = 600000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', value.encode(), salt, iterations)
    return f'pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}'

def verify_secret(value: str, encoded: str, fallback: str = '') -> bool:
    if len(value) > 4096:
        return False
    if encoded:
        try:
            name, iterations, salt, expected = encoded.split('$')
            rounds = int(iterations)
            if name != 'pbkdf2_sha256' or not 100000 <= rounds <= 2000000:
                return False
            actual = hashlib.pbkdf2_hmac('sha256', value.encode(), unb64(salt), rounds)
            return hmac.compare_digest(actual, unb64(expected))
        except (ValueError, TypeError):
            return False
    return bool(fallback) and hmac.compare_digest(value.encode(), fallback.encode())

class AuthError(ValueError):
    pass

class AuthManager:
    def __init__(self):
        self.origin = env('ARGUS_ORIGIN', 'http://localhost:8000').rstrip('/')
        self.challenges = {}
        self.gates = {}
        self.sessions = {}
        self.failures = defaultdict(deque)
        self.lock = threading.RLock()

    def configured(self):
        return bool(len(env('ARGUS_SESSION_SECRET')) >= 32 and
            (env('ARGUS_PIN_HASH') or env('ARGUS_PIN') or env('PASSKEY_PIN')))

    def check_origin(self, origin: str | None):
        if origin != self.origin:
            raise AuthError('Origin rejected; use the configured ARGUS_ORIGIN')

    def _prune(self):
        now = time.time()
        for mapping in (self.challenges, self.gates, self.sessions):
            for key in list(mapping):
                if mapping[key]['exp'] < now:
                    del mapping[key]
        for key in list(self.failures):
            queue = self.failures[key]
            while queue and queue[0] < now - 300:
                queue.popleft()
            if not queue:
                del self.failures[key]

    def _attempt(self, peer):
        self._prune()
        if len(self.failures.get(peer, ())) >= 8:
            raise AuthError('Too many attempts; wait five minutes')
        if len(self.challenges) + len(self.gates) > 512:
            raise AuthError('Too many pending logins')
        self.failures[peer].append(time.time())

    def start_phrase(self, peer):
        with self.lock:
            self._attempt(peer)
            if not self.configured():
                raise AuthError('Configure the session secret and PIN first')
            key = secrets.token_urlsafe(32)
            self.challenges[key] = {'exp': time.time() + 60, 'peer': peer}
            return {'challenge_id': key, 'phrase': PHRASE}

    def consume_phrase(self, challenge_id, peer):
        with self.lock:
            self._prune()
            request = self.challenges.get(challenge_id)
            if not request or request['peer'] != peer:
                raise AuthError('Start the phrase challenge again')
            del self.challenges[challenge_id]

    def verify_phrase(self, transcript, peer):
        # Only the server-side audio transcription route calls this method.
        with self.lock:
            good = isinstance(transcript, str) and phrase_matches(transcript)
            db.log_auth_event('phrase', good)
            if not good:
                raise AuthError('The spoken words did not match. Try again.')
            key = secrets.token_urlsafe(32)
            self.gates[key] = {'exp': time.time() + 90, 'peer': peer}
            return {'gate_id': key}

    def pin(self, gate_id, pin, peer):
        with self.lock:
            self._attempt(peer)
            gate = self.gates.get(gate_id)
            if not gate or gate['peer'] != peer:
                raise AuthError('Complete the spoken phrase first')
            del self.gates[gate_id]
            good = isinstance(pin, str) and pin.isascii() and pin.isdigit() and verify_secret(pin, env('ARGUS_PIN_HASH'), env('ARGUS_PIN', env('PASSKEY_PIN')))
            db.log_auth_event('pin', good)
            if not good or not self.configured():
                raise AuthError('Authentication failed. Start again.')
            self.failures.pop(peer, None)
            payload = {'jti': secrets.token_urlsafe(24), 'exp': time.time() + max(60, min(int(env('SESSION_TIMEOUT_SECONDS', '900')), 86400))}
            encoded = b64(json.dumps(payload, separators=(',', ':')).encode())
            signature = b64(hmac.new(env('ARGUS_SESSION_SECRET').encode(), encoded.encode(), hashlib.sha256).digest())
            self.sessions[payload['jti']] = payload
            return encoded + '.' + signature

    def verify_token(self, token: str | None):
        if not token or not self.configured():
            raise AuthError('Session required')
        try:
            encoded, signature = token.split('.')
            expected = b64(hmac.new(env('ARGUS_SESSION_SECRET').encode(), encoded.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                raise ValueError('signature')
            payload = json.loads(unb64(encoded))
            if payload['exp'] < time.time() or payload['jti'] not in self.sessions:
                raise ValueError('expiry')
            return payload
        except (ValueError, KeyError, TypeError):
            raise AuthError('Session expired or invalid') from None

    def logout(self, token):
        try:
            payload = self.verify_token(token)
            self.sessions.pop(payload['jti'], None)
        except AuthError:
            pass

auth = AuthManager()
