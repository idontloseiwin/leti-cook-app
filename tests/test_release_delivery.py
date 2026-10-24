"""One delivery chain: loopback GitHub HTTP -> signature -> fresh installation.

Only Docker/host effects are mocked. HTTP bytes, OpenSSL verification, captured
release files, installer sequencing and installation receipts are real.
"""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from ops import release_download, release_install
from tests import test_release_download as download_fixture
from tests import test_release_install as install_fixture


class ReleaseDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_fixture.ReleaseInstallTests.setUpClass()

    def test_downloaded_release_drives_complete_captured_installation(self):
        host = install_fixture.ReleaseInstallTests(
            "test_signed_amd64_pulls_exact_digests_pins_ids_and_preserves_captured_files")
        host.setUp()
        self.addCleanup(host.doCleanups)
        fixture = host.fixture
        base = "/repos/example-owner/example-app/releases"
        assets, payloads = [], {}
        for ident, path in enumerate(sorted(fixture.directory.iterdir()), 10):
            data = path.read_bytes()
            assets.append({"id": ident, "name": path.name, "state": "uploaded", "size": len(data)})
            payloads[base + "/assets/" + str(ident)] = data
        release = download_fixture.release(assets=assets)
        payloads[base + "?per_page=100&page=1"] = json.dumps([release]).encode()
        payloads[base + "/7"] = json.dumps(release).encode()
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Accept")))
                data = payloads[self.path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def loopback_connection(hostname, *, timeout):
            self.assertEqual(hostname, "api.github.com")
            return http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)

        try:
            # Exercise GitHubTransport's real bounded HTTP reading and the real
            # fetch/signature path; only the TLS socket is redirected locally.
            with patch.object(release_download.http.client, "HTTPSConnection", side_effect=loopback_connection):
                downloaded = release_download.fetch_release(
                    source=fixture.source, public_key_path=fixture.public_file,
                    transport=release_download.GitHubTransport(), openssl=fixture.openssl)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(seen), 6)
        self.assertEqual({path for path, accept in seen if accept == release_download.BINARY_ACCEPT},
                         {base + "/assets/" + str(asset["id"]) for asset in assets})
        self.assertEqual(downloaded, host.verified)
        self.assertIsNot(downloaded, host.verified)
        host.verified = downloaded
        # Both the original HTTP source and archive are unavailable/replaced.
        # Installation must consume exactly the downloader's immutable object.
        payloads.clear()
        with patch.object(release_install, "install_verified_release",
                          wraps=release_install.install_verified_release) as create:
            result = host.lifecycle(architecture="x86_64")
        self.assertIs(create.call_args.args[0], downloaded)
        for name, data in downloaded.files.items():
            self.assertEqual((host.destination / name).read_bytes(), data, name)
        commands = [args for args, _ in host.calls]
        pulls = [args for args in commands if "pull" in args and "up" not in args and "run" not in args]
        self.assertEqual([args[-1] for args in pulls], [
            downloaded.manifest["images"][service]["repository"] + "@" + downloaded.manifest["images"][service]["digest"]
            for service in ("db", "backend")])
        self.assertFalse(any("build" in args for args in commands))
        self.assertTrue(any("up" in args and "backend" in args for args in commands))
        for name in ("installation.json", "installed.json"):
            receipt = json.loads((host.destination / name).read_bytes())
            self.assertEqual(receipt["source"], "release")
            self.assertEqual(receipt["version"], downloaded.manifest["version"])
            self.assertEqual(receipt["project"], result["project"])
        override = json.loads((host.destination / "runtime.compose.json").read_bytes())
        self.assertEqual({name: value["image"] for name, value in override["services"].items()},
                         {"db": "sha256:" + "a" * 64, "backend": "sha256:" + "b" * 64})
        self.assertEqual(result["source"], "release")
        self.assertEqual(result["provider_requests"], 0)


if __name__ == "__main__":
    unittest.main()
