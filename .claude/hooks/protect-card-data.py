#!/usr/bin/env python3
"""
PreToolUse guard: refuse any operation that would destroy downloaded card data.

Enforces CLAUDE.md Rule 0. The card database and downloaded images represent
hours-to-days of rate-limited provider downloading and are treated as user data,
not a rebuildable cache.

Contract: reads the PreToolUse payload on stdin, prints a JSON decision on
stdout. On a match it emits permissionDecision="deny" plus a systemMessage, so
the block is visible to BOTH the model and the user — never a silent pass.
Anything it does not recognize is allowed through (fail-open by design: this is
a targeted guard, not a sandbox).
"""
import json
import re
import sys
import time
from pathlib import Path

LOG = Path(__file__).resolve().parent / 'protect-card-data.log'

# --- What is protected -----------------------------------------------------
# Matches the data volume and its irreplaceable contents, however it is spelled
# (./data, data/, /data, $PROXYSHOP_DATA_DIR, absolute paths ending in /data).
PROTECTED = re.compile(
    r"""(
          cards\.db
        | data/images
        | data/cache-runs
        | data/bulk
        | \$?\{?PROXYSHOP_DATA_DIR\}?
        | (?:^|[\s'"=:])/?data/?(?:$|[\s'"/;&|])
        | /data\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# --- Destructive shell verbs ----------------------------------------------
DESTRUCTIVE = [
    (re.compile(r'\brm\b'), 'rm'),
    (re.compile(r'\brmdir\b'), 'rmdir'),
    (re.compile(r'\bshred\b'), 'shred'),
    (re.compile(r'\bunlink\b'), 'unlink'),
    (re.compile(r'\btruncate\b'), 'truncate'),
    (re.compile(r'\bmkfs\b|\bdd\s+.*\bof='), 'dd/mkfs'),
    (re.compile(r'-delete\b|-exec\s+rm\b'), 'find -delete'),
    (re.compile(r'shutil\.rmtree|os\.remove|os\.unlink|\.unlink\('), 'python file removal'),
    (re.compile(r'\bmv\b'), 'mv (moves data out of place)'),
    (re.compile(r'>\s*[^|>]*(?:cards\.db|data/)'), 'shell redirect over a data file'),
]

# Destructive SQL against the card DB.
SQL_DESTRUCTIVE = re.compile(
    r'\bDROP\s+TABLE\b|\bTRUNCATE\b|\bDELETE\s+FROM\s+(?:cards|decks|deck_cards|prices)\b'
    r'|\bVACUUM\s+INTO\b|\bDROP\s+INDEX\b',
    re.IGNORECASE)

# Dangerous regardless of an explicit path — these wipe the volume wholesale.
ALWAYS_BLOCK = [
    (re.compile(r'\bdocker\s+volume\s+rm\b', re.I), 'docker volume rm destroys the data volume'),
    (re.compile(r'\bdocker(?:\s+compose|-compose)\s+down\b[^\n]*(?:\s-v\b|--volumes)', re.I),
     'docker compose down -v destroys the data volume'),
    (re.compile(r'\bgit\s+clean\b[^\n]*-[a-z]*[xX]', re.I),
     'git clean -x/-X removes gitignored files, which is exactly where the card data lives'),
]

REASON_HEADER = (
    '🛑 BLOCKED BY CLAUDE.md RULE 0 — DOWNLOADED CARD DATA IS PROTECTED.\n'
)
REASON_BODY = (
    'The local card database and downloaded images are irreplaceable: they '
    'represent hours to days of rate-limited provider downloading, and some '
    'sources (the Weiss Schwarz and Union Arena cardlist scrapes) may not serve '
    'the same cards again. They are user data, not a rebuildable cache.\n\n'
    'This operation was refused, not deferred. Do NOT attempt a variant, a '
    'workaround, a different shell form, or a "just this once" exception. If '
    'you genuinely believe this data must be removed, STOP and ask the user in '
    'plain language what you want to delete and why, and let them decide.\n\n'
    'If you only need a database to work against, use a temporary directory '
    '(tmp_path / a scratch dir) — never the real data volume.'
)


def decide(tool: str, payload: dict):
    """Return (blocked, what, detail) for this tool call."""
    if tool == 'Bash':
        cmd = str(payload.get('command') or '')
        if not cmd.strip():
            return False, '', ''
        for pat, why in ALWAYS_BLOCK:
            if pat.search(cmd):
                return True, cmd, why
        hits_protected = bool(PROTECTED.search(cmd))
        if hits_protected:
            for pat, verb in DESTRUCTIVE:
                if pat.search(cmd):
                    return True, cmd, f'`{verb}` targeting protected card data'
            if SQL_DESTRUCTIVE.search(cmd):
                return True, cmd, 'destructive SQL against the card database'
        # `cd <data> && rm ...` hides the path from the rm itself.
        if re.search(r'\bcd\b[^;&|]*(?:data|cards\.db)', cmd, re.I) and \
                re.search(r'\brm\b|\bshred\b|-delete\b', cmd, re.I):
            return True, cmd, 'a directory change into the data volume followed by a removal'
        if SQL_DESTRUCTIVE.search(cmd) and re.search(r'sqlite3?\b', cmd, re.I):
            return True, cmd, 'destructive SQL against a SQLite database'
        return False, '', ''

    if tool in ('Write', 'Edit', 'NotebookEdit'):
        path = str(payload.get('file_path') or payload.get('notebook_path') or '')
        if path and PROTECTED.search(path):
            return True, path, 'writing over a file inside the protected data volume'
    return False, '', ''


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or '{}')
    except (json.JSONDecodeError, ValueError):
        return 0  # never break the session on a malformed payload
    tool = str(data.get('tool_name') or '')
    payload = data.get('tool_input') or {}
    if not isinstance(payload, dict):
        return 0

    try:
        blocked, what, detail = decide(tool, payload)
    except Exception:  # noqa: BLE001 — a guard bug must not wedge the session
        return 0
    if not blocked:
        return 0

    reason = (
        f'{REASON_HEADER}\n'
        f'Refused {tool} call: {detail}.\n'
        f'  → {what.strip()[:400]}\n\n'
        f'{REASON_BODY}'
    )
    try:
        with LOG.open('a', encoding='utf-8') as fh:
            fh.write(
                f'[{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}] '
                f'BLOCKED {tool}: {detail} :: {what.strip()[:300]}\n')
    except OSError:
        pass

    print(json.dumps({
        # Shown to the USER in the UI — this is the "loudly mention it" half.
        'systemMessage': (
            f'🛑 BLOCKED: an agent tried to delete protected card data '
            f'({detail}). Refused per CLAUDE.md Rule 0. '
            f'Logged to .claude/hooks/protect-card-data.log'),
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        },
    }))
    return 0


if __name__ == '__main__':
    sys.exit(main())
