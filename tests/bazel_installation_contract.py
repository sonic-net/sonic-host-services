"""Check installation interfaces against the original source packaging inputs."""
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import unittest
import zlib

PYTHON, DATA, RUNTIME, DEBUG, SOURCE = sys.argv[1:]
sys.argv = sys.argv[:1]


def archive(path):
    with tarfile.open(path, 'r:') as stream:
        return {m.name.removeprefix('./'): (m, stream.extractfile(m).read())
                for m in stream if m.isfile()}


def sections(data):
    assert data[:6] == b'\x7fELF\x02\x01'
    offset = struct.unpack_from('<Q', data, 40)[0]
    size, count, names_index = struct.unpack_from('<HHH', data, 58)
    entries = [struct.unpack_from('<IIQQQQIIQQ', data, offset + size*i) for i in range(count)]
    strings = entries[names_index]
    names = data[strings[4]:strings[4]+strings[5]]
    return {names[e[0]:].split(b'\0', 1)[0].decode(): data[e[4]:e[4]+e[5]] for e in entries}


class InstallationTest(unittest.TestCase):
    def test_python_installation_and_record(self):
        payload = archive(PYTHON)
        source = Path(SOURCE).parent
        site = 'usr/local/lib/python3.13/dist-packages/'
        for package in ('host_modules', 'utils'):
            for path in (source/package).glob('*.py'):
                self.assertEqual(payload[site+package+'/'+path.name][1], path.read_bytes())
        setup = (source/'setup.py').read_text()
        import ast
        calls = [n for n in ast.walk(ast.parse(setup)) if isinstance(n, ast.Call)]
        setup_call = next(n for n in calls if getattr(n.func, 'id', '') == 'setup')
        scripts = ast.literal_eval(next(k.value for k in setup_call.keywords if k.arg == 'scripts'))
        for name in scripts:
            raw = (source/name).read_bytes()
            expected = b'#!/usr/bin/python3\n'+raw.split(b'\n', 1)[1] if raw.startswith(b'#!') else raw
            member, installed = payload['usr/local/bin/'+Path(name).name]
            self.assertEqual(installed, expected)
            self.assertEqual(member.mode, 0o755)
        record = next(v[1] for k,v in payload.items() if k.endswith('.dist-info/RECORD'))
        for path, digest, size in csv.reader(io.StringIO(record.decode())):
            if not digest:
                continue
            import posixpath
            name = posixpath.normpath(site+path)
            raw = payload[name][1]
            self.assertEqual(int(size), len(raw))
            self.assertEqual(digest, 'sha256='+base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode())

    def test_original_data_payload(self):
        source = Path(SOURCE).parent
        payload = archive(DATA)
        expected = {}
        for path in (source/'data/templates').glob('*.j2'):
            expected['usr/share/sonic/templates/'+path.name] = path
        expected['etc/dbus-1/system.d/org.sonic.hostservice.conf'] = source/'data/org.sonic.hostservice.conf'
        for path in (source/'data/debian').glob('sonic-host-services-data.*'):
            if path.suffix in ('.service', '.timer'):
                expected['usr/lib/systemd/system/'+path.name.removeprefix('sonic-host-services-data.')] = path
        self.assertEqual(set(payload), set(expected))
        for name, path in expected.items():
            member, raw = payload[name]
            self.assertEqual(raw, path.read_bytes())
            self.assertEqual((member.mode, member.uid, member.gid), (0o644, 0, 0))

    def test_native_runtime_and_split_debug(self):
        runtime = archive(RUNTIME)
        self.assertEqual(set(runtime), {'usr/bin/procdockerstatsd-rs'})
        member, raw = runtime['usr/bin/procdockerstatsd-rs']
        self.assertEqual(member.mode, 0o755)
        self.assertEqual(struct.unpack_from('<H', raw, 18)[0], 62)
        elf = sections(raw)
        self.assertNotIn('.debug_info', elf)
        debug = archive(DEBUG)
        self.assertEqual(len(debug), 1)
        debug_name, (_, debug_raw) = next(iter(debug.items()))
        self.assertIn('.debug_info', sections(debug_raw))
        link = elf['.gnu_debuglink']
        self.assertEqual(link.split(b'\0', 1)[0].decode(), Path(debug_name).name)
        self.assertEqual(struct.unpack_from('<I', link, len(link)-4)[0], zlib.crc32(debug_raw))


if __name__ == '__main__':
    unittest.main()
