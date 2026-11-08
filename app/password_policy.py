"""
Password policy for a user choosing their own password (ADR-0026). Deliberately short and explainable.

Rules, and why:
  * at least 12 characters -- length is what actually resists guessing;
  * at most 72 BYTES -- bcrypt silently ignores everything after byte 72, so a longer password
    would be accepted and then verify against only its first 72 bytes. Refusing it is honest;
  * different from the current password -- otherwise "changing" it to force a rotation does nothing;
  * does not contain the person's username or the part of their email before the @ (if 3+ long);
  * not a very common password (a small built-in list; this is not a breach-database check);
  * at least 5 different characters -- stops "aaaaaaaaaaaa".
There are NO "must contain a capital, a digit and a symbol" rules: they make passwords harder to
remember without making them harder to guess (NIST SP 800-63B recommends against them).

This module touches no database and no request: it takes strings and returns a list of problems,
so every rule can be tested on its own.
"""
MIN_LENGTH = 12
MAX_BYTES = 72
MIN_DISTINCT_CHARS = 5

# Lower-case. Only entries of MIN_LENGTH or more matter, because shorter ones fail the length rule.
_COMMON = frozenset({
    "password1234", "password12345", "passwordpassword", "123456789012", "1234567890123",
    "qwertyuiop12", "qwertyuiopas", "letmein12345", "welcome12345", "changeme1234", "changeme12345",
    "changeme123!", "iloveyou1234", "administrator", "administrator1", "guardrail1234", "guardrailrag",
    "abcdefghijkl", "1q2w3e4r5t6y", "trustno1trustno1", "p@ssw0rd1234", "passw0rd1234",
})


def check_new_password(new: str, *, current: str, username: str, email: str) -> list[str]:
    """Return a list of human-readable problems; an empty list means the password is acceptable."""
    problems: list[str] = []
    if len(new) < MIN_LENGTH:
        problems.append(f"must be at least {MIN_LENGTH} characters")
    if len(new.encode("utf-8")) > MAX_BYTES:
        problems.append(f"must be at most {MAX_BYTES} bytes (long or non-English passwords count more than one byte per character)")
    if new == current:
        problems.append("must be different from your current password")
    lowered = new.lower()
    local_part = email.split("@", 1)[0].lower()
    for label, needle in (("username", username.lower()), ("email name", local_part)):
        if len(needle) >= 3 and needle in lowered:
            problems.append(f"must not contain your {label}")
    if lowered in _COMMON:
        problems.append("is too common")
    if len(set(new)) < MIN_DISTINCT_CHARS:
        problems.append(f"must use at least {MIN_DISTINCT_CHARS} different characters")
    return problems
