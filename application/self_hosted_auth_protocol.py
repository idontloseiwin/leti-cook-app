"""Domain-separated worker request proof. Never a browser session or DB key."""
import hashlib
import hmac
import re
import time

HEADER = 'X-LIC-Runtime-Proof'

def worker_proof(key, *, now=None):
    stamp = str(int(time.time() if now is None else now))
    signature = hmac.new(key.encode(), b'lic-runtime-request-v1\0GET\0/internal/hot-runtime\0'+stamp.encode(), hashlib.sha256).hexdigest()
    return stamp + '.' + signature


def valid_worker_proof(value, key, *, now=None):
    if not key or not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,12}\.[0-9a-f]{64}',value):
        return False
    current = int(time.time() if now is None else now)
    stamp = int(value.split('.')[0])
    return current-10 <= stamp <= current+1 and hmac.compare_digest(value,worker_proof(key,now=stamp))
