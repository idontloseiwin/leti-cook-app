"""Choose bounded documentation checks or full CI; verify the final job results."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
# Only user-facing prose and its existing screenshot qualify. Generated docs,
# executable Markdown, new paths and every CI change deliberately take full CI.
DOC_FILES = frozenset({
    'README.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'docs/INSTALLATION.md', 'docs/CONFIGURATION.md', 'docs/DATA.md',
    'docs/TROUBLESHOOTING.md', 'extensions/chromium/README.md',
    'docs/images/channel-explorer-overview.png',
})
FULL_JOBS = ('application', 'dependencies', 'images', 'delivery')
SHA = re.compile(r'[0-9a-f]{40}')
RAW = re.compile(rb':([0-7]{6}) ([0-7]{6}) [0-9a-f]+ [0-9a-f]+ ([A-Z])')


def classify_diff(raw: bytes) -> str:
    """Parse NUL-delimited raw Git records, including deletions and mode changes.

    --no-renames makes both the old and new paths participate in the decision.
    Empty, malformed or unfamiliar input must never select the short path.
    """
    fields = raw.split(b'\0')
    if not raw or fields.pop() != b'' or len(fields) % 2:
        return 'full'
    for header, path in zip(fields[::2], fields[1::2]):
        match = RAW.fullmatch(header)
        if not match:
            return 'full'
        old, new, status = match.groups()
        modes = {b'A': (b'000000', b'100644'),
                 b'D': (b'100644', b'000000'),
                 b'M': (b'100644', b'100644')}
        if modes.get(status) != (old, new):
            return 'full'
        try:
            name = path.decode('utf-8')
        except UnicodeDecodeError:
            return 'full'
        if name not in DOC_FILES:
            return 'full'
    return 'docs'


def git(*args: str, root: Path) -> bytes:
    return subprocess.run(['git', *args], cwd=root, check=True,
                          capture_output=True, timeout=60).stdout


def select_profile(event_name: str, event: dict, *, root: Path = ROOT) -> tuple[str, str]:
    if event_name not in ('pull_request', 'push'):
        return 'full', 'Explicit or unfamiliar event runs full CI.'
    try:
        if event_name == 'pull_request':
            before = event['pull_request']['base']['sha']
            after = event['pull_request']['head']['sha']
        else:
            before, after = event['before'], event['after']
        if any(not isinstance(x, str) or not SHA.fullmatch(x) or x == '0' * 40
               for x in (before, after)):
            return 'full', 'No complete commit range; full CI required.'
        if event_name == 'pull_request':
            before = git('merge-base', before, after, root=root).decode('ascii').strip()
            if not SHA.fullmatch(before):
                return 'full', 'No reliable merge base; full CI required.'
        raw = git('diff', '--raw', '-z', '--no-renames', '--no-ext-diff',
                  before, after, '--', root=root)
        profile = classify_diff(raw)
        return profile, ('Only allowlisted documentation changed.' if profile == 'docs'
                         else 'Code, unknown paths, modes or an empty diff require full CI.')
    except (KeyError, TypeError, ValueError, UnicodeError, OSError, subprocess.SubprocessError):
        return 'full', 'Change detection was incomplete; full CI required.'


def verify_results(needs: dict) -> str:
    """Success requires the exact expected graph, never an incidental skip."""
    if not isinstance(needs, dict) or set(needs) != {'scope', *FULL_JOBS}:
        raise ValueError('Missing or unexpected CI job results')
    scope = needs['scope']
    if not isinstance(scope, dict) or scope.get('result') != 'success':
        raise ValueError('Change detection and documentation validation must succeed')
    profile = scope.get('outputs', {}).get('profile')
    if profile not in ('docs', 'full'):
        raise ValueError('Missing or invalid CI profile')
    expected = 'skipped' if profile == 'docs' else 'success'
    for name in FULL_JOBS:
        job = needs[name]
        if not isinstance(job, dict) or job.get('result') != expected:
            raise ValueError(f'{name} must be {expected} for {profile} CI')
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('classify', 'result'))
    args = parser.parse_args()
    if args.command == 'classify':
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        profile, reason = select_profile(os.environ['GITHUB_EVENT_NAME'], event)
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(f'profile={profile}\n')
        message = f'CI profile: **{profile}**. {reason}'
    else:
        profile = verify_results(json.loads(os.environ['CI_NEEDS']))
        message = f'CI result: **{profile}** checks passed.'
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(message + '\n')


if __name__ == '__main__':
    main()
