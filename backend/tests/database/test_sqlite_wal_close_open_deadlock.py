"""Regression test for the libsqlite3 WAL close/open lock-order deadlock.

libsqlite3 3.51.0 added a broken-POSIX-lock probe that runs on the
EXCLUSIVE-lock path of every WAL connection close
(sqlite3WalClose -> unixLock -> unixIsSharingShmNode). While holding the
per-inode pLockMutex it re-acquires the process-global unixBigLock,
inverting the lock order documented in os_unix.c ("unixBigLock must be
acquired first"). A concurrent sqlite3.connect() holding unixBigLock inside
findReusableFd() then blocks on pLockMutex and both threads deadlock
permanently; SiGMA's NullPool churn plus timed-out sweeps (whose cancelled
close awaits left the C-level close running) hit this in production within
hours of uptime. Fixed upstream in 3.51.2, check-in f65aa46796.

The driver below reproduces the production interleaving on purpose: it
submits a close to a worker thread and — without waiting for it — asks a
second worker to open the next connection, so both C calls start together.
The race to unixBigLock is stacked in the opener's favor (its path to the
lock is shorter), which is exactly the deadlock. The hammer runs in a child
process because a reproduced deadlock wedges sqlite for the whole process
that hit it; the child detects the stall (the fresh-connection result never
arrives) and exits non-zero instead of poisoning the pytest process.
"""

import queue
import subprocess
import sys
import threading
import time

import pytest

pytestmark = [pytest.mark.database, pytest.mark.concurrency, pytest.mark.regression]

ROUNDS = 100
STALL_SECONDS = 3.0
HARD_DEADLINE_SECONDS = 60.0

# stdlib-only on purpose: the child must exercise exactly the sqlite3 C
# paths of the interpreter it is spawned from.
_HAMMER_CHILD = """\
import queue
import sqlite3
import sys
import threading

DB = sys.argv[1]
ROUNDS = int(sys.argv[2])
STALL_SECONDS = float(sys.argv[3])

setup = sqlite3.connect(DB, timeout=5.0)
setup.execute("PRAGMA journal_mode=WAL")
setup.execute("CREATE TABLE t (v INTEGER)")
setup.commit()
setup.close()

closer_q = queue.Queue()
opener_q = queue.Queue()
fresh_q = queue.Queue()
errors = []
stop = threading.Event()


# check_same_thread=False is required: connections are opened by one thread
# and closed by another (sqlite's own serialized mode keeps the C side safe).
def closer():
    while not stop.is_set():
        conn = closer_q.get()
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error as exc:
            errors.append(repr(exc))
            stop.set()


def opener():
    while not stop.is_set():
        if opener_q.get() is None:
            return
        try:
            conn = sqlite3.connect(DB, timeout=0.05, check_same_thread=False)
            try:
                conn.execute("INSERT INTO t (v) VALUES (0)")
                conn.commit()
            except sqlite3.Error:
                conn.close()
                raise
            fresh_q.put(conn)
        except sqlite3.OperationalError:
            fresh_q.put(None)
        except sqlite3.Error as exc:
            errors.append(repr(exc))
            fresh_q.put(None)
            stop.set()


for worker in (closer, opener):
    threading.Thread(target=worker, daemon=True).start()


def open_with_shm():
    conn = sqlite3.connect(DB, timeout=0.05, check_same_thread=False)
    conn.execute("INSERT INTO t (v) VALUES (0)")
    conn.commit()
    return conn


try:
    conn = open_with_shm()
    for round_index in range(ROUNDS):
        # Submitted and abandoned: the closer thread may still be inside
        # sqlite3_close while the opener thread starts sqlite3.connect.
        closer_q.put(conn)
        opener_q.put("open")
        try:
            conn = fresh_q.get(timeout=STALL_SECONDS)
        except queue.Empty:
            print(
                f"STALL rounds={round_index}/{ROUNDS} errors={len(errors)}",
                flush=True,
            )
            sys.exit(2)
        if conn is None:
            conn = open_with_shm()
        print(f"rounds={round_index + 1}", flush=True)
    conn.close()
    print(f"DONE errors={len(errors)}", flush=True)
    sys.exit(1 if errors else 0)
finally:
    stop.set()
    closer_q.put(None)
    opener_q.put(None)
"""


def test_wal_close_open_cycles_do_not_wedge(tmp_path):
    script = tmp_path / "wal_hammer.py"
    script.write_text(_HAMMER_CHILD)
    proc = subprocess.Popen(
        [
            sys.executable, str(script), str(tmp_path / "wal_race.db"),
            str(ROUNDS), str(STALL_SECONDS),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    lines: list[str] = []
    out_q: queue.Queue = queue.Queue()

    def _pump():
        for line in proc.stdout:
            out_q.put(line.rstrip("\n"))
        out_q.put(None)

    threading.Thread(target=_pump, daemon=True).start()
    try:
        deadline = time.monotonic() + HARD_DEADLINE_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(
                    "hammer child did not finish in time; output: "
                    f"{lines[-5:]}"
                )
            try:
                line = out_q.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if line is None:
                break
            lines.append(line)
            if line.startswith("STALL"):
                pytest.fail(f"libsqlite3 open/close deadlock reproduced: {line}")
            if line.startswith("rounds="):
                # Liveness only; keep the child's stdout draining so the
                # pipe cannot back up behind a wedged worker.
                continue
        return_code = proc.wait(timeout=10)
        stderr = proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()

    assert return_code == 0, (
        f"hammer child failed rc={return_code}, output={lines[-5:]}, "
        f"stderr={stderr[-500:]}"
    )
    assert lines[-1] == "DONE errors=0", lines[-1]
