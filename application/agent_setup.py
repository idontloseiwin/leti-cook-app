"""Owner downloads for local MCP clients. Packages contain a server address and no credentials."""
from io import BytesIO
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

_LOCAL_ORIGIN = re.compile(r'https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?\Z')


def download(client, origin):
    match = _LOCAL_ORIGIN.fullmatch(origin)
    if not match or (match[1] and not 1 <= int(match[1]) <= 65535):
        raise ValueError('local_installation_address_required')
    endpoint = origin + '/mcp'
    if client == 'claude-desktop':
        manifest = {
            'manifest_version': '0.3', 'name': 'leti-cook', 'display_name': 'Leti Cook',
            'version': '1.0.0', 'author': {'name': 'Leti Cook'},
            'description': 'Read the library on your local Leti Cook installation.',
            'user_config': {'agent_key': {'type': 'string', 'title': 'Agent key', 'required': True,
                                          'sensitive': True, 'description': 'Paste a read-only key from Leti Cook → Agents.'}},
            'server': {'type': 'node', 'entry_point': 'server/bridge.cjs',
                       'mcp_config': {'command': 'node', 'args': ['${__dirname}/server/bridge.cjs'],
                                      'env': {'LETI_MCP_URL': endpoint, 'LETI_AGENT_KEY': '${user_config.agent_key}'}}},
        }
        files = {'manifest.json': json.dumps(manifest, indent=2).encode(),
                 'server/bridge.cjs': Path(__file__).with_name('claude_desktop_bridge.cjs').read_bytes()}
        name = 'leti-cook.mcpb'
    elif client == 'claude-code':
        config = {'mcpServers': {'leti-cook': {'type': 'http', 'url': endpoint}}}
        files = {'Leti Cook/.mcp.json': (json.dumps(config, indent=2) + '\n').encode()}
        name = 'leti-cook-claude-code.zip'
    elif client == 'codex':
        config = '[mcp_servers.leti-cook]\nurl = ' + json.dumps(endpoint) + '\n'
        files = {'Leti Cook/.codex/config.toml': config.encode()}
        name = 'leti-cook-codex.zip'
    else:
        raise KeyError(client)

    body = BytesIO()
    if client in ('codex', 'claude-code'):
        launch = 'codex app .' if client == 'codex' else 'claude'
        # These shortcuts only open the extracted project with an installed client.
        files['Leti Cook/Open on Windows.cmd'] = (
            '@echo off\r\nsetlocal\r\npushd "%~dp0" || exit /b 1\r\n'
            + 'call ' + launch + '\r\nif errorlevel 1 pause\r\npopd\r\n').encode()
        files['Leti Cook/Open on macOS.command'] = (
            '#!/bin/sh\ncd -P -- "$(dirname -- "$0")" || exit 1\nexec ' + launch + '\n').encode()
    with ZipFile(body, 'w') as archive:
        for path, content in files.items():
            info = ZipInfo(path)
            info.create_system = 3
            info.external_attr = (0o100755 if path.endswith('.command') else 0o100644) << 16
            archive.writestr(info, content, compress_type=ZIP_DEFLATED)
    return name, body.getvalue()
