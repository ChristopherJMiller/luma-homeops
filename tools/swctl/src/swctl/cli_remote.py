"""Telnet client for the TEG-30284 CLI, run on the router under `python3 -`.
Whitelisted commands only, and `logout` always: a session closed without it
leaks one of the switch's ~4 CLI slots for ~30 minutes. Router.cli prepends
PASSWORD, COMMANDS and HOST_OVERRIDE as constants (never argv)."""
import re
import socket
import time

HOST, PORT = "192.168.99.2", 23
ALLOWED = {"save", "show switch", "show ipif"}
PAGER = re.compile(r"CTRL\+C.*?Next Entry\s+a All\s*", re.S)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROMPT = re.compile(rb"[\w\-.]*[#>]\s*$")


def allowed(cmd: str) -> bool:
    return cmd in ALLOWED


def clean(text: str) -> str:
    return PAGER.sub("", ANSI.sub("", text)).replace("\r", "").strip()


def session(host, password, commands):
    s = socket.create_connection((host, PORT), timeout=10)
    s.settimeout(0.3)

    def read(wait, until=None):
        buf, end = b"", time.time() + wait
        while time.time() < end:
            try:
                d = s.recv(4096)
            except socket.timeout:
                continue
            if not d:
                break
            out, i = b"", 0
            while i < len(d):                    # refuse every telnet option
                if d[i] == 255 and i + 2 < len(d) and d[i + 1] in (251, 252, 253, 254):
                    s.sendall(bytes([255, 254 if d[i + 1] in (251, 252) else 252, d[i + 2]]))
                    i += 3
                else:
                    out += d[i:i + 1]
                    i += 1
            buf += out
            if b"Next Page" in buf[-120:]:
                s.sendall(b"a")
            if re.search(rb"\(y/n\)|\[y/n\]", buf[-80:], re.I):
                s.sendall(b"y\r\n")
            if until is not None and until.search(buf):
                break
        return buf.decode(errors="replace")

    banner = read(5, re.compile(rb"(sername|ogin)\s*:\s*$"))
    if "Exceeding Max Cli Sessions" in banner:
        s.close()
        raise SystemExit("switch CLI: every session slot is taken (leaked sessions expire after ~30 min)")
    s.sendall(b"admin\r\n")
    read(4, re.compile(rb"assword\s*:\s*$"))
    s.sendall(password.encode() + b"\r\n")
    after = read(5, PROMPT)
    if not PROMPT.search(after.encode()) or re.search(r"(?i)fail|denied|invalid|incorrect", after):
        s.close()
        raise SystemExit("switch CLI: login failed")
    try:
        for c in commands:
            if not allowed(c):
                print(f"refused (not whitelisted): {c!r}")
                continue
            s.sendall(c.encode() + b"\r\n")
            print(f"== {c}\n{clean(read(40 if c == 'save' else 8, PROMPT))}")
    finally:
        s.sendall(b"logout\r\n")
        time.sleep(1.0)
        s.close()


if __name__ == "__main__":
    session(globals().get("HOST_OVERRIDE", HOST), PASSWORD, COMMANDS)  # noqa: F821 (prepended by Router.cli)
