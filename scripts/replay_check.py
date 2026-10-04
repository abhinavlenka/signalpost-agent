"""Check determinism on frozen inputs: record one live run, then replay it with the network sealed.

    uv run python scripts/replay_check.py --input data/batch-rehearsal-100.txt

Three runs of the normal run command are made, each in its own process:

1. ``live``     against the network, with every HTTP response and DNS answer written to a tape;
2. ``replay``   from an empty state directory, served only from the tape;
3. ``refresh``  over the replay's state directory, served only from the tape.

The two sealed runs use different ``PYTHONHASHSEED`` values, so an output that depends on set or
dict hashing order shows up as a difference. The three outputs are then compared company by company
(see ``check_determinism.py``); the same source bytes must give the same record every time.

Pass ``--tape`` to reuse an earlier recording and skip the live run (two sealed runs, a few seconds
each). A tape is a local pickle of what this machine fetched; load only tapes you recorded yourself.
"""
from __future__ import annotations

import argparse
import email.message
import io
import os
import pickle
import runpy
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


# -- child process: one run of the agent with the network recorded or replayed --------------------
def _headers(pairs: list[tuple[str, str]]) -> email.message.Message:
    message = email.message.Message()
    for name, value in pairs:
        message[name] = value
    return message


def _play(entry: dict[str, Any]) -> Any:
    """Return, or raise, what the network gave for one request."""
    kind = entry["kind"]
    if kind == "response":
        return urllib.response.addinfourl(io.BytesIO(entry["body"]), _headers(entry["headers"]), entry["final_url"], entry["status"])
    if kind == "http_error":
        raise urllib.error.HTTPError(entry["final_url"], entry["status"], entry["reason"], _headers(entry["headers"]), io.BytesIO(entry["body"]))
    if kind == "TimeoutError":
        raise TimeoutError(entry["text"])
    if kind.startswith("Connection") or kind == "RemoteDisconnected":
        raise ConnectionError(entry["text"])
    raise urllib.error.URLError(entry["text"])


def run_child(mode: str, tape_path: Path, agent_args: list[str]) -> None:
    tape: dict[str, dict[str, Any]] = {"http": {}, "dns": {}} if mode == "record" else pickle.loads(tape_path.read_bytes())
    lock, nested = threading.Lock(), threading.local()
    position: dict[str, int] = {}
    misses: list[str] = []
    real_open, real_dns = urllib.request.OpenerDirector.open, socket.getaddrinfo

    def open_(self: Any, fullurl: Any, data: Any = None, timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT) -> Any:
        if getattr(nested, "active", False):  # a redirect hop inside a request that is already being recorded
            return real_open(self, fullurl, data, timeout)
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else str(fullurl)
        if mode == "replay":
            with lock:
                entries = tape["http"].get(url)
                if not entries:
                    misses.append(url)
                    raise urllib.error.URLError("sealed: this URL was not fetched in the recorded run")
                index = position.get(url, 0)
                position[url] = index + 1
            return _play(entries[min(index, len(entries) - 1)])  # a retried URL replays its responses in order
        nested.active = True
        try:
            with real_open(self, fullurl, data, timeout) as response:
                entry = {"kind": "response", "status": response.status, "final_url": response.geturl(), "headers": list(response.headers.items()), "body": response.read()}
        except urllib.error.HTTPError as exc:
            entry = {"kind": "http_error", "status": exc.code, "final_url": exc.geturl() or url, "reason": str(exc.reason), "headers": list((exc.headers or {}).items()), "body": exc.read()}
        except Exception as exc:  # noqa: BLE001 - whatever the network raised is what the replay must raise
            entry = {"kind": type(exc).__name__, "text": str(getattr(exc, "reason", exc))}
        finally:
            nested.active = False
        with lock:
            tape["http"].setdefault(url, []).append(entry)
        return _play(entry)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if mode == "replay":
            answer = tape["dns"].get(host)
            if answer is None:
                misses.append(f"dns:{host}")
                raise socket.gaierror(socket.EAI_NONAME, "sealed: this host was not resolved in the recorded run")
            if answer["error"]:
                raise socket.gaierror(*answer["error"])
            return answer["result"]
        try:
            result = real_dns(host, *args, **kwargs)
        except socket.gaierror as exc:
            if not getattr(nested, "active", False):
                with lock:
                    tape["dns"].setdefault(host, {"error": exc.args, "result": None})
            raise
        if not getattr(nested, "active", False):
            with lock:
                tape["dns"].setdefault(host, {"error": None, "result": result})
        return result

    urllib.request.OpenerDirector.open = open_  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    sys.path.insert(0, str(ROOT))
    sys.argv = ["signalpost", *agent_args]
    try:
        runpy.run_module("signalpost", run_name="__main__")
    finally:
        if mode == "record":
            tape_path.write_bytes(pickle.dumps(tape))
            print(f"[replay-check] recorded {sum(len(item) for item in tape['http'].values())} responses from {len(tape['http'])} URLs and {len(tape['dns'])} host lookups", flush=True)
        else:
            print(f"[replay-check] sealed run: {len(misses)} requests were not on the tape" + (f", e.g. {misses[:3]}" if misses else ""), flush=True)


# -- parent process: record, replay twice, compare -------------------------------------------------
def _run(mode: str, tape: Path, out: Path, state: Path, args: argparse.Namespace, hash_seed: str | None) -> None:
    environment = {**os.environ, **({"PYTHONHASHSEED": hash_seed} if hash_seed else {})}
    command = [sys.executable, str(Path(__file__).resolve()), "--child", mode, "--tape", str(tape), "--",
               "run", "--input", args.input, "--out", str(out), "--state", str(state), *args.agent_args]
    print(f"[replay-check] {out.name}: {mode}" + (f" (PYTHONHASHSEED={hash_seed})" if hash_seed else ""), flush=True)
    subprocess.run(command, cwd=ROOT, env=environment, check=False)  # the agent exits non-zero on a failed validation; the comparison decides


def _compare(first: Path, second: Path) -> bool:
    print(f"[replay-check] {first.name} against {second.name}", flush=True)
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "check_determinism.py"), str(first), str(second)], cwd=ROOT, check=False).returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", help="batch of organisation numbers, as given to the run command")
    parser.add_argument("--work", default=str(ROOT / "out" / "replay-check"), help="directory for the tape and the three runs (emptied first)")
    parser.add_argument("--tape", help="reuse this recording instead of making a live run")
    parser.add_argument("--child", choices=["record", "replay"], help=argparse.SUPPRESS)
    parser.add_argument("agent_args", nargs="*", help="after --, extra arguments passed to the run command")
    args = parser.parse_args()
    if args.child:
        run_child(args.child, Path(args.tape), args.agent_args)
        return
    if not args.input:
        parser.error("--input is required")
    work = Path(args.work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tape = Path(args.tape) if args.tape else work / "tape.pkl"
    pairs: list[tuple[Path, Path]] = []
    if not args.tape:
        _run("record", tape, work / "live", work / "state-live", args, None)
        pairs.append((work / "live", work / "replay"))
    _run("replay", tape, work / "replay", work / "state-sealed", args, "1")
    _run("replay", tape, work / "refresh", work / "state-sealed", args, "2")
    pairs.append((work / "replay", work / "refresh"))
    results = [_compare(first, second) for first, second in pairs]
    print("[replay-check] " + ("zero semantic differences on frozen inputs" if all(results) else "DIFFERENCES FOUND on frozen inputs"), flush=True)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
