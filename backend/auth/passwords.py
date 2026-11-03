"""Password hashing for the admin login. Stdlib only.

Hashes are self-describing (`scrypt$n$r$p$salt$hash`) so the cost can be raised
later without breaking stored hashes. Builds of OpenSSL without scrypt fall back
to PBKDF2-SHA256 (600k rounds), stored as `pbkdf2_sha256$rounds$salt$hash`.
"""
import base64
import hashlib
import hmac
import os

MIN_LENGTH = 10
_N, _R, _P = 2 ** 14, 8, 1
_PBKDF2_ROUNDS = 600_000


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    return base64.b64decode(s.encode("ascii"))


def hash_password(password):
    salt = os.urandom(16)
    pw = password.encode("utf-8")
    try:
        dk = hashlib.scrypt(pw, salt=salt, n=_N, r=_R, p=_P, dklen=32)
        return "scrypt$%d$%d$%d$%s$%s" % (_N, _R, _P, _b64(salt), _b64(dk))
    except (AttributeError, ValueError):
        dk = hashlib.pbkdf2_hmac("sha256", pw, salt, _PBKDF2_ROUNDS)
        return "pbkdf2_sha256$%d$%s$%s" % (_PBKDF2_ROUNDS, _b64(salt), _b64(dk))


def verify_password(password, stored):
    if not stored or not isinstance(password, str):
        return False
    parts = stored.split("$")
    pw = password.encode("utf-8")
    try:
        if parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt, want = _unb64(parts[4]), _unb64(parts[5])
            got = hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=len(want))
        elif parts[0] == "pbkdf2_sha256" and len(parts) == 4:
            salt, want = _unb64(parts[2]), _unb64(parts[3])
            got = hashlib.pbkdf2_hmac("sha256", pw, salt, int(parts[1]))
        else:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def check_new_password(password):
    """A user-facing reason the password can't be used, or None."""
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        return "Please use at least %d characters." % MIN_LENGTH
    if len(password) > 1024:
        return "That password is too long."
    return None
