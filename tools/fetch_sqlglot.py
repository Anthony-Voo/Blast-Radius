"""Build step: fetch sqlglot at the pinned commit from GitHub and unpack it."""
import io
import os
import sys
import tarfile
import time
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
dest = sys.argv[1]

data = None
for attempt in range(5):
    try:
        with urllib.request.urlopen(URL, timeout=120) as r:
            data = r.read()
        break
    except Exception as e:  # noqa: BLE001
        print("download failed:", repr(e), file=sys.stderr)
        time.sleep(3 * (attempt + 1))
if data is None:
    sys.exit("could not download sqlglot")

os.makedirs(dest, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
    members = []
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]
        members.append(m)
    tar.extractall(dest, members=members, filter="data")
assert os.path.isfile(os.path.join(dest, "sqlglot", "parser.py"))
assert os.path.isfile(os.path.join(dest, "tests", "test_parser.py"))
print("sqlglot unpacked into", dest)
