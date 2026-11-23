"""Leti Cook MCP for stdio-only clients: a bridge to the installation's /mcp endpoint.

The installation owns the tools, instructions and every check. This program only
moves JSON-RPC lines between stdin/stdout and one loopback HTTP address, with the
agent key read from a private file. Standard library only; no packages to install.
"""
import argparse
import json
from pathlib import Path
import sys

# Run with python -I: its own folder is the only extra import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import ApiError, Transport, token_from_file  # noqa: E402


def _error(identity, text):
    return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32000, 'message': text}}


def _batch_ids(message):
    return [item['id'] for item in message if type(item) is dict and 'method' in item
            and (type(item.get('id')) is int or (type(item.get('id')) is str and item['id']))]


def bridge(transport, lines, write):
    version = None
    for line in lines:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            write({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse_error'}})
            continue
        identity = message.get('id') if type(message) is dict else None
        try:
            answer = transport.post(message, version)
        except ApiError as error:
            if type(message) is list and (identities := _batch_ids(message)):
                write([_error(item, str(error)) for item in identities])
            elif identity is not None:
                write(_error(identity, str(error)))
            else:
                sys.stderr.write('leti-cook-mcp: ' + str(error) + '\n')
                sys.stderr.flush()
            continue
        if answer is None:
            continue
        if type(answer) is list:
            write(answer)
            continue
        if type(message) is list and answer.get('id') is None and 'error' in answer:
            identities = _batch_ids(message)
            write([{**answer, 'id': item} for item in identities] if identities else answer)
            continue
        if answer.get('id') is None and identity is not None:
            answer['id'] = identity
        if type(message) is dict and message.get('method') == 'initialize':
            result = answer.get('result')
            if type(result) is dict and type(result.get('protocolVersion')) is str:
                version = result['protocolVersion']
        write(answer)


def main():
    parser = argparse.ArgumentParser(description='Leti Cook read-only MCP bridge for stdio clients')
    parser.add_argument('--base-url', required=True, help='The installation origin, e.g. http://127.0.0.1:8002')
    parser.add_argument('--token-file', required=True, help='A private file holding only the agent key')
    args = parser.parse_args()
    try:
        transport = Transport(args.base_url, args.token_file)
        token_from_file(args.token_file)  # Fail before the protocol starts, without printing values.
    except ApiError as error:
        parser.exit(2, str(error) + '\n')
    out = sys.stdout

    def write(value):
        out.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
        out.flush()

    sys.stdin.reconfigure(encoding='utf-8')
    out.reconfigure(encoding='utf-8', newline='\n')
    bridge(transport, sys.stdin, write)


if __name__ == '__main__':
    main()
