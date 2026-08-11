#!/usr/bin/env python3
"""Driver: start the real live server, run the live recording, stop server.

Runs as a single foreground process (no shell background jobs) so it completes
cleanly and the server is always terminated.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

KEY = os.environ["K2"]
ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = open("/tmp/live_server.log", "w", encoding="utf-8")


def health_ok() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:3000/api/version", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    env = {**os.environ, "K2": KEY}
    srv = subprocess.Popen(
        [sys.executable, "-u", "tests/boot_live_server.py"],
        cwd=str(ROOT), env=env, stdout=LOG, stderr=subprocess.STDOUT,
    )
    try:
        ok = False
        for _ in range(45):
            if health_ok():
                ok = True
                break
            time.sleep(1)
        if not ok:
            print("SERVER FAILED TO BOOT")
            LOG.flush()
            print(open("/tmp/live_server.log", encoding="utf-8").read()[-2000:])
            return
        print("server ready", flush=True)
        # run live recording
        rec = subprocess.run(
            [sys.executable, "-u", "tests/record_live.py"], cwd=str(ROOT),
            env=env, capture_output=True, text=True, timeout=260,
        )
        print(rec.stdout[-1500:], flush=True)
        if rec.returncode != 0:
            print("REC ERR:", rec.stderr[-1200:], flush=True)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:  # noqa: BLE001
            srv.kill()
        LOG.close()
    print("done", flush=True)


if __name__ == "__main__":
    main()
