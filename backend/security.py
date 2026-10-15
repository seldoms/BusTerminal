import hashlib
import hmac
import os


PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password).encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False

    parts = str(stored_hash).split("$")
    if len(parts) == 4 and parts[0] == PASSWORD_HASH_ALGORITHM:
        try:
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
        except ValueError:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            salt,
            iterations,
        ).hex()
        return hmac.compare_digest(digest, parts[3])

    # Legacy compatibility for old unsalted SHA256 hashes.
    legacy_digest = hashlib.sha256(str(password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy_digest, str(stored_hash))
