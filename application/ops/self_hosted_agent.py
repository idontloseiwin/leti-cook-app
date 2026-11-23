"""Owner command for research credentials. The secret is displayed once on a TTY."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_access import issue
from db.agent_access import AgentAccessRepository
from db.self_hosted_auth import OwnerAuthRepository, AuthStoreError

# manage.py names this refusal from the exit status alone; it never shows the
# helper's captured output.
EXIT_OWNER_ACCESS_NOT_CONFIGURED = 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'list', 'revoke'))
    parser.add_argument('--name')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--key-id')
    args = parser.parse_args(argv)
    try:
        owner = OwnerAuthRepository(os.environ['LIC_AUTH_DATABASE'])
        binding = owner.agent_binding()
        if not binding:
            raise AuthStoreError('owner_access_not_configured')
        store = AgentAccessRepository(owner.path)
        if args.action == 'create':
            if not sys.stdin.isatty():
                raise AuthStoreError('interactive_terminal_required')
            with open('CONOUT$' if os.name == 'nt' else '/dev/tty', 'w', encoding='utf-8') as tty:
                if not tty.isatty():
                    raise AuthStoreError('interactive_terminal_required')
                metadata, secret = issue(owner, args.name, args.days)
                tty.write('Research read-only key (shown once; keep it private):\n' + secret + '\n')
                tty.flush()
            print(json.dumps(metadata))
        elif args.action == 'list':
            print(json.dumps({'keys': store.list(binding, int(time.time())), 'scope': 'research.read'}))
        else:
            identity = str(uuid.UUID(args.key_id or ''))
            print(json.dumps({'revoked': store.revoke(identity), 'id': identity}))
        return 0
    except (OSError, ValueError, KeyError, AuthStoreError) as error:
        if isinstance(error, AuthStoreError) and str(error) == 'owner_access_not_configured':
            print('Agent access command refused: owner access is not configured yet.', file=sys.stderr)
            return EXIT_OWNER_ACCESS_NOT_CONFIGURED
        print('Agent access command refused. Check the name, expiry (1–365 days), key ID, '
              'configured owner access and private storage. Creation needs an interactive terminal.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
