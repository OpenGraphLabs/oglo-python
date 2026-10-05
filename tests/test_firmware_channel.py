"""The runtime firmware channel: what it accepts, and everything it must refuse.

The channel exists so a new firmware reaches an installed SDK without anyone
being asked to reinstall. That means untrusted bytes now decide which image gets
written to hardware, so each test below is a way the channel could be wrong.
Served over a real loopback HTTP server, not a mocked urlopen, because the thing
being tested is what happens to bytes that arrived from somewhere else.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest

from oglo import _firmware_package as pkg


BUNDLE = Path(pkg.__file__).parent / 'firmware_bundle'


def serve(files: dict):
    """Serve a dict of {name: bytes} on loopback and return its base URL."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.lstrip('/')
            if name not in files:
                self.send_error(404)
                return
            body = files[name]
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{server.server_port}', server


@pytest.fixture
def channel(tmp_path, monkeypatch):
    """Isolate the on-disk cache so a test never sees another test's state."""
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path / 'state'))

    def configure(files, *, version=None):
        url, server = serve(files)
        monkeypatch.setenv('OGLO_FIRMWARE_CHANNEL_URL', url)
        import importlib
        import oglo._firmware_channel as channel_module
        importlib.reload(channel_module)
        return channel_module, server

    return configure


def real_bundle_files(version: str) -> dict:
    """The genuine signed bundle, optionally relabelled to a different version."""
    image = (BUNDLE / 'application.bin').read_bytes()
    manifest = (BUNDLE / 'manifest.txt').read_bytes()
    signature = (BUNDLE / 'signature.der').read_bytes()
    if version != pkg.VERSION:
        manifest = manifest.replace(f'version={pkg.VERSION}\n'.encode(),
                                    f'version={version}\n'.encode())
    pointer = json.dumps({'schema': 1, 'part': pkg.PART, 'hw_rev': pkg.HARDWARE,
                          'key_id': pkg.KEY_ID, 'version': version,
                          'size': len(image),
                          'file_sha256': hashlib.sha256(image).hexdigest(),
                          'running_sha256': image[-32:].hex()}).encode()
    return {'current.json': pointer, 'application.bin': image,
            'manifest.txt': manifest, 'signature.der': signature}


def bumped(version: str) -> str:
    major, minor, patch = (int(p) for p in version.split('.'))
    return f'{major}.{minor}.{patch + 1}'


def test_the_channel_is_consulted_once_per_process(channel):
    """A capture loop must not turn into a stream of requests."""
    files = real_bundle_files(pkg.VERSION)
    hits = []

    class Counting(dict):
        def __contains__(self, key):
            hits.append(key)
            return dict.__contains__(self, key)

    module, _ = channel(Counting(files))
    module.fetch_current()
    first = len(hits)
    for _ in range(5):
        module.fetch_current()
    assert len(hits) == first
    module.reset_cache()
    module.fetch_current()
    assert len(hits) > first


def test_offline_falls_back_to_the_wheel(tmp_path, monkeypatch):
    """No network must behave exactly as it did before the channel existed."""
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('OGLO_FIRMWARE_CHANNEL_URL', 'http://127.0.0.1:1/nothing')
    monkeypatch.setenv('OGLO_FIRMWARE_CHANNEL_TIMEOUT', '1')
    import importlib
    import oglo._firmware_channel as channel_module
    importlib.reload(channel_module)
    assert channel_module.fetch_current() is None
    policy = pkg.current_policy()
    assert policy.bundle == pkg.BUNDLE_DIR
    assert pkg.VERSION in policy.policy_id


def test_a_channel_offering_the_same_version_is_not_downloaded(channel):
    """Nothing newer than the wheel means nothing to do, not a redundant write."""
    module, _ = channel(real_bundle_files(pkg.VERSION))
    assert module.fetch_current() is None


def test_a_channel_offering_an_older_version_never_downgrades(channel):
    module, _ = channel(real_bundle_files('0.0.1'))
    assert module.fetch_current() is None


def test_a_relabelled_manifest_is_refused(channel):
    """The signature covers the manifest, so renaming the version breaks it.

    This is the whole reason the transport does not have to be trusted: a
    channel cannot invent a newer release, only serve one that was signed.
    """
    pytest.importorskip('cryptography')
    module, _ = channel(real_bundle_files(bumped(pkg.VERSION)))
    assert module.fetch_current() is None
    assert module.known_releases() == {}


def test_a_tampered_image_is_refused(channel):
    pytest.importorskip('cryptography')
    files = real_bundle_files(bumped(pkg.VERSION))
    files['application.bin'] = files['application.bin'][:-1] + b'\x00'
    module, _ = channel(files)
    assert module.fetch_current() is None


def test_a_pointer_that_lies_about_the_version_is_refused(channel):
    """current.json is a hint; the signed manifest is the authority."""
    pytest.importorskip('cryptography')
    files = real_bundle_files(pkg.VERSION)
    pointer = json.loads(files['current.json'])
    pointer['version'] = bumped(pkg.VERSION)
    files['current.json'] = json.dumps(pointer).encode()
    module, _ = channel(files)
    assert module.fetch_current() is None


def test_a_missing_pointer_is_not_an_error(channel):
    module, _ = channel({'application.bin': b'x'})
    assert module.fetch_current() is None


def test_an_oversized_response_is_refused(channel):
    module, _ = channel({'current.json': b'{' + b' ' * 8192})
    assert module.fetch_current() is None


def test_accepted_sources_cover_the_wheel_and_every_verified_release(tmp_path, monkeypatch):
    """A glove on the previous release must still be a legal starting point.

    Without this, the first firmware after an SDK release would refuse every
    glove that SDK had already updated, which is exactly the trap that makes a
    rollout need another round of customer contact.
    """
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path / 'state'))
    import importlib
    import oglo._firmware_channel as channel_module
    importlib.reload(channel_module)
    sources = pkg.accepted_sources()
    for version, running in pkg.FROM_IMAGES.items():
        assert sources[version] == running
    assert sources[pkg.VERSION] == pkg.RUNNING_SHA

    channel_module.remember_release('9.9.9', 'a' * 64)
    assert pkg.accepted_sources()['9.9.9'] == 'a' * 64
    # The floor is never widened away by a cache that learned something new.
    assert pkg.accepted_sources()[pkg.VERSION] == pkg.RUNNING_SHA
    # A target is included so re-running an update is a no-op, not a refusal.
    assert pkg.accepted_sources(('7.7.7', 'b' * 64))['7.7.7'] == 'b' * 64


def test_a_corrupt_cache_cannot_widen_the_accepted_sources(tmp_path, monkeypatch):
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path / 'state'))
    import importlib
    import oglo._firmware_channel as channel_module
    importlib.reload(channel_module)
    (channel_module.channel_root() / 'known_releases.json').write_text('{"schema": 99}')
    sources = pkg.accepted_sources()
    assert sources[pkg.VERSION] == pkg.RUNNING_SHA
    assert all(len(v) == 64 for v in sources.values())


def test_the_published_pointer_matches_the_bundle_it_describes():
    """What a publisher uploads must agree with the bundle, field for field."""
    pytest.importorskip('cryptography')
    from oglo import _firmware_channel as channel_module
    pointer = json.loads(channel_module.pointer_for(BUNDLE))
    bundle = pkg.load_bundle(BUNDLE)
    assert pointer == {'schema': 1, 'part': pkg.PART, 'hw_rev': pkg.HARDWARE,
                       'key_id': pkg.KEY_ID, 'version': bundle.version,
                       'size': len(bundle.image),
                       'file_sha256': bundle.file_sha256,
                       'running_sha256': bundle.running_sha256}


def test_the_wheel_bundle_still_matches_the_pinned_constants():
    """Loosening load_bundle must not loosen what ships inside the wheel."""
    pytest.importorskip('cryptography')
    bundle = pkg.load_bundle(BUNDLE, expect=(pkg.VERSION, pkg.FILE_SHA, pkg.RUNNING_SHA))
    assert bundle.version == pkg.VERSION
    with pytest.raises(pkg.FirmwareError, match='not the release this SDK pins'):
        pkg.load_bundle(BUNDLE, expect=('0.0.0', pkg.FILE_SHA, pkg.RUNNING_SHA))


def test_a_manifest_for_another_product_or_key_is_refused():
    image = (BUNDLE / 'application.bin').read_bytes()
    good = (BUNDLE / 'manifest.txt').read_bytes()
    for bad in (good.replace(pkg.PART.encode(), b'OTHER-PART'),
                good.replace(pkg.HARDWARE.encode(), b'OTHER_HW'),
                good.replace(pkg.KEY_ID.encode(), b'other-key')):
        with pytest.raises(ValueError, match='another product or signing key'):
            pkg.parse_manifest(bad, len(image))
    with pytest.raises(ValueError, match='size does not match'):
        pkg.parse_manifest(good, len(image) + 1)
    with pytest.raises(ValueError, match='noncanonical manifest shape'):
        pkg.parse_manifest(good + b'extra=1\n', len(image))


def signed_release(version: str, key):
    """A synthetic but structurally real signed release, for the accept path.

    Signed with a test key rather than the production one, because the point is
    to prove that a correctly signed NEWER release is actually taken up. Every
    other test here proves the refusals, and a feature that only refuses would
    pass all of them while never updating a single glove.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    body = b"\xa5" * 4096
    image = body + hashlib.sha256(body).digest()
    manifest = (f"OGLO-FW-MANIFEST-V1\npart={pkg.PART}\nhw_rev={pkg.HARDWARE}\n"
                f"version={version}\nsize={len(image)}\n"
                f"sha256={hashlib.sha256(image).hexdigest()}\n"
                f"key_id={pkg.KEY_ID}\n").encode("ascii")
    signature = key.sign(manifest, ec.ECDSA(hashes.SHA256()))
    pointer = json.dumps({"schema": 1, "part": pkg.PART, "hw_rev": pkg.HARDWARE,
                          "key_id": pkg.KEY_ID, "version": version,
                          "size": len(image),
                          "file_sha256": hashlib.sha256(image).hexdigest(),
                          "running_sha256": image[-32:].hex()}).encode()
    return {"current.json": pointer, "application.bin": image,
            "manifest.txt": manifest, "signature.der": signature}, image


def test_a_newer_signed_release_is_fetched_cached_and_used(channel, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(pkg, "PUBLIC_KEY", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    version = bumped(pkg.VERSION)
    files, image = signed_release(version, key)
    module, server = channel(files)

    directory = module.fetch_current()
    assert directory is not None and directory.name == version
    assert (directory / "application.bin").read_bytes() == image

    # It is remembered, so a glove left on it stays a legal source later.
    assert module.known_releases()[version] == image[-32:].hex()
    assert pkg.accepted_sources()[version] == image[-32:].hex()

    # The policy now targets the fetched release, not the wheel.
    policy = pkg.current_policy()
    assert policy.bundle == directory and version in policy.policy_id

    # With the server stopped, the cache keeps serving it. A lab that fetched
    # once must not lose the release the next time it is offline.
    server.shutdown()
    module.reset_cache()
    assert module.fetch_current() == directory


LIVE_URL = 'https://github.com/OpenGraphLabs/oglo-python/releases/download/firmware-current'


def test_the_live_channel_is_not_behind_this_wheel():
    """Publishing the channel is a release step, so a missed one must show up.

    Skips without network, because an offline developer is not a defect. In CI,
    which has network, this is the guard that turns "someone forgot to run
    tools/publish_firmware_channel.py" into a failure instead of a surprise
    months later when a customer is asked to reinstall again.
    """
    import urllib.error
    import urllib.request
    try:
        request = urllib.request.Request(
            f'{LIVE_URL}/current.json',
            headers={'User-Agent': 'oglo-sdk-channel-check/1'})
        with urllib.request.urlopen(request, timeout=20) as response:
            pointer = json.loads(response.read(4096).decode('utf-8'))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        pytest.skip('firmware channel unreachable from here')
    assert pointer['part'] == pkg.PART
    assert pointer['hw_rev'] == pkg.HARDWARE
    assert pointer['key_id'] == pkg.KEY_ID
    published = tuple(int(p) for p in pointer['version'].split('.'))
    bundled = tuple(int(p) for p in pkg.VERSION.split('.'))
    assert published >= bundled, (
        f'the channel serves {pointer["version"]} but this wheel carries '
        f'{pkg.VERSION}; run tools/publish_firmware_channel.py')
