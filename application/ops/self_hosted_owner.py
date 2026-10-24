"""Trusted local owner setup/recovery. Credentials go only to an interactive TTY."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
import time
import warnings
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from db.self_hosted_auth import OwnerAuthRepository, AuthStoreError
from db.user_work.reconnect_admission import ChannelAddAdmission, ChannelAddAdmissionUnavailable
from self_hosted_auth import PASSWORD_HASHER, token_hash, valid_password
from provider_admission import AdmissionUnavailable


def initialize(path):
    # No usable account/password. A dummy hash equalizes password verification.
    admission = ChannelAddAdmission.for_installation(path, os.getenv('LIC_DATABASE_ID'))
    repository = OwnerAuthRepository.initialize(path,PASSWORD_HASHER.hash(secrets.token_urlsafe(32)),int(time.time()))
    ChannelAddAdmission.initialize(admission.path, admission.installation_id)
    return repository


def prepare_channel_add_state(path, *, legacy_transition=False):
    """Updater-only explicit first transition; a current missing file is refused."""
    admission = ChannelAddAdmission.for_installation(path, os.getenv('LIC_DATABASE_ID'))
    if legacy_transition and not admission.path.exists():
        ChannelAddAdmission.initialize(admission.path, admission.installation_id)
    admission.verify()


def setup_code(repository):
    code=secrets.token_urlsafe(32)
    repository.issue_setup(token_hash(code),int(time.time()))
    return code


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=os.getenv('LIC_AUTH_DATABASE'))
    parser.add_argument('command',choices=['initialize','setup-code','reset-password','status','set-api-key',
                                           'check-api-key','clear-sign-in-limit','prepare-channel-add-state'])
    parser.add_argument('--stdin',action='store_true',help='Read a bounded key from installer stdin; never echo it.')
    parser.add_argument('--legacy-transition',action='store_true',help='Explicit verified legacy update only; never used at runtime.')
    args=parser.parse_args()
    if args.legacy_transition and args.command!='prepare-channel-add-state': parser.error('--legacy-transition is only for prepare-channel-add-state')
    if args.stdin and args.command!='set-api-key': parser.error('--stdin is only for set-api-key')
    if args.database is None: parser.error('auth database path required')
    try:
        if args.command=='initialize':
            initialize(args.database)
            if os.getenv('LIC_SETUP_DIRECTORY'):
                from self_hosted_configuration import ConfigurationStore
                ConfigurationStore.initialize(os.environ['LIC_SETUP_DIRECTORY'])
            print(json.dumps({'status':'owner_store_created','credentials_printed':False}))
            return 0
        if args.command=='prepare-channel-add-state':
            prepare_channel_add_state(args.database, legacy_transition=args.legacy_transition)
            print(json.dumps({'status':'channel_add_state_ready'}))
            return 0
        repository=OwnerAuthRepository(args.database)
        if args.command=='status':
            print(json.dumps({'configured':repository.configured(),
                              'sign_in_limited':repository.sign_in_limited(int(time.time()))}))
            return 0
        if args.command=='clear-sign-in-limit':
            # Prints no secret; the password and signed-in browsers are kept.
            repository.clear_sign_in_limit()
            print(json.dumps({'status':'sign_in_limit_cleared','password_changed':False}))
            return 0
        if args.command=='set-api-key':
            from self_hosted_configuration import ConfigurationStore
            from self_hosted_settings import valid_api_key,SettingsError
            if args.stdin:
                key=sys.stdin.readline(202).rstrip('\r\n')
                if sys.stdin.read(1): raise AuthStoreError('invalid_key_input')
            else:
                if not sys.stdin.isatty(): raise AuthStoreError('interactive_terminal_required')
                with warnings.catch_warnings():
                    warnings.simplefilter('error',getpass.GetPassWarning)
                    key=getpass.getpass('YouTube API key (hidden): ')
            if not valid_api_key(key): raise AuthStoreError('invalid_key_format')
            store=ConfigurationStore(os.environ['LIC_SETUP_DIRECTORY'])
            value=store.read()
            def change(v):
                if v['prepared']:
                    # A key change is not a new setup: channels and the category reference stay, and the
                    # current key stays in use until check-api-key hears YouTube accept the new one.
                    v['pending_api_key']=key
                else:
                    v.update(api_key=key,prepared=False,phase='saved',error=None)
            store.update(value['revision'],change)
            print(json.dumps({'status':'key_saved','provider_requests':0}))
            return 0
        if args.command=='check-api-key':
            # This standalone command joins the started backend's existing gate
            # for one request (1 quota unit). A refused key is not kept: a running
            # installation goes on with its previous key, and a new one asks for a
            # key again instead of failing during preparation. A key that could not
            # be checked is kept, as the owner asked for it.
            from self_hosted_configuration import ConfigurationStore
            from self_hosted_onboarding import check_api_key
            store=ConfigurationStore(os.environ['LIC_SETUP_DIRECTORY'])
            value=store.read()
            pending=value.get('pending_api_key')
            if not (pending or value['api_key']): raise AuthStoreError('api_key_required')
            from provider_runtime import start_command, stop_runtime, request_scope
            provider=start_command({**os.environ, 'LIC_AUTH_DATABASE':str(args.database)})
            try:
                from core import process_database_resources
                with process_database_resources(), request_scope('interactive'):
                    code=check_api_key(pending or value['api_key'])
            finally:
                stop_runtime(provider)
            refused=code in ('provider_key_rejected','provider_access_denied')
            if pending:
                def change(v):
                    del v['pending_api_key']
                    if not refused: v.update(api_key=pending,error=None)
                store.update(value['revision'],change)
            elif refused:
                store.update(value['revision'],lambda v:v.update(api_key=None,prepared=False,phase='needs_key',error=code))
            # A key YouTube accepted clears an earlier refusal, so stopped collection resumes by itself.
            print(json.dumps({'status':code or 'key_accepted','provider_requests':1,'key_kept':not refused,
                              'running':bool(pending),'previous_key_in_use':bool(pending) and refused,
                              'previous_key_refused':bool(pending) and refused and value['error'] in
                              ('provider_key_rejected','provider_access_denied')}))
            return 0
        # Never emit a bootstrap credential to redirected stdout or install logs.
        if not sys.stdin.isatty(): raise AuthStoreError('interactive_terminal_required')
        if args.command=='setup-code':
            with open('CONOUT$' if os.name=='nt' else '/dev/tty','w',encoding='utf8') as tty:
                if not tty.isatty(): raise AuthStoreError('interactive_terminal_required')
                code=setup_code(repository)
                tty.write('One-time setup code (15 minutes): '+code+'\n')
                tty.write('Expired or lost? In the installation folder run: sudo python3 manage.py setup-code\n')
                tty.flush()
        else:
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                password=getpass.getpass('New owner password (hidden): ')
                repeat=getpass.getpass('Repeat password (hidden): ')
            if not valid_password(password) or password!=repeat:
                raise AuthStoreError('passwords_must_match_and_have_15_to_128_characters')
            repository.reset_password(PASSWORD_HASHER.hash(password))
            print(json.dumps({'status':'password_changed','all_sessions_revoked':True}))
        return 0
    except (OSError,ValueError,KeyError,AuthStoreError,ChannelAddAdmissionUnavailable,AdmissionUnavailable,getpass.GetPassWarning) as error:
        if (args.command=='setup-code' and isinstance(error,AuthStoreError)
                and str(error)=='owner_already_created'):
            print('Access setup is already complete. No new setup code was issued. '
                  'Open /auth to use the access mode already selected.',file=sys.stderr)
        else:
            print('Owner operation refused; verify the private path, account state and terminal.',file=sys.stderr)
        return 1

if __name__=='__main__': raise SystemExit(main())
