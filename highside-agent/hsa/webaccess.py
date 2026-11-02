"""Getting internal web pages programmatically - several methods, from simple to
"drive a real browser". Pick one in config ("web": {"via": ...}) or let
"auto" try them in order and remember what works per host.

  python      urllib. Works when the site needs no login (or a cookie/cert you configure).
  curl        curl.exe (built into Windows 10+). --negotiate sends your Windows login
              (Kerberos/NTLM) and it trusts the Windows certificate store.
  powershell  Invoke-WebRequest -UseDefaultCredentials (Windows login).
  headless    A private invisible Edge/Chrome (driven over DevTools): runs the page's JavaScript and returns the
              rendered HTML (for search pages that build results with JS). Uses Windows login
              for intranet sites; smart-card prompts are not possible headless.
  cdp         Attach to a real Edge/Chrome window over the DevTools protocol. Start it with
              `python -m hsa browser`, log in once (smart card, SSO, whatever), keep it open
              (minimized), and HSA loads pages in background tabs of that logged-in browser.
              This works for essentially anything you can open by hand.

`python -m hsa webtest URL` tries every method against a URL and tells you which work.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hsa import net

METHODS = ["python", "curl", "powershell", "headless", "cdp"]
AUTO_ORDER = ["python", "curl", "powershell", "cdp", "headless"]

Result = Tuple[int, Dict[str, str], bytes]


# ---------------------------------------------------------------- helpers -----
def settings() -> Dict[str, Any]:
    return net.WEB


def find_browser() -> Optional[str]:
    p = settings().get("browser_path") or os.environ.get("HSA_BROWSER")
    if p and os.path.exists(p):
        return p
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                      os.path.join(base, r"Google\Chrome\Application\chrome.exe")]
    else:
        for n in ("microsoft-edge", "msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
            w = shutil.which(n)
            if w:
                cands.append(w)
        cands += sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome"), reverse=True)
        cands += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for c in cands:
        if c and os.path.exists(str(c)):
            return str(c)
    return None


def profile_dir(kind: str = "cdp") -> str:
    base = settings().get("profile_dir") or str(net.WEB_HOME / "browser-profile")
    d = base if kind == "cdp" else base + "-" + kind
    os.makedirs(d, exist_ok=True)
    return d


_LOGIN_TITLE = re.compile(r"<title>[^<]*(log\s?in|sign\s?in|logon|authenticat|single sign|sso|access denied|"
                          r"unauthori[sz]ed|forbidden)[^<]*</title>", re.I)


def classify(body: bytes, status: int = 200, final_url: str = "", rendered: bool = False) -> str:
    """'' if the page looks like real content, else a short problem description.
    rendered=True: the HTML came from a real browser, so JavaScript already ran."""
    if status >= 400:
        return "HTTP %d" % status
    if not body:
        return "empty response"
    head = body[:200000].decode("utf-8", "replace")
    if re.search(r"/(login|logon|signin|sso|auth)\b", final_url or "", re.I):
        return "redirected to a login page"
    is_html = bool(re.search(r"<html|<!doctype html", head[:2000], re.I))
    if not is_html:
        return ""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", head, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    if _LOGIN_TITLE.search(head) or (re.search(r"type=[\"']?password", head, re.I) and len(text) < 4000):
        return "looks like a login page"
    if not rendered and len(text) < 200 and head.lower().count("<script") >= 1:
        return "little text, page probably builds content with JavaScript"
    return ""


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _remembered() -> Dict[str, str]:
    try:
        return json.loads((net.WEB_HOME / "web_methods.json").read_text())
    except Exception:
        return {}


def _remember(host: str, method: str):
    d = _remembered()
    if d.get(host) != method:
        d[host] = method
        try:
            net.WEB_HOME.mkdir(parents=True, exist_ok=True)
            (net.WEB_HOME / "web_methods.json").write_text(json.dumps(d, indent=1))
        except OSError:
            pass


def forget_methods():
    try:
        (net.WEB_HOME / "web_methods.json").unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- methods -----
def fetch_python(url, cfg, timeout=None, max_bytes=None) -> Result:
    try:
        return net._urllib_request(url, cfg, "GET", None, None, timeout, False, max_bytes)
    except net.HTTPError as e:
        if e.status:
            return e.status, {}, (e.body or "").encode("utf-8")
        raise


def fetch_curl(url, cfg, timeout=None, max_bytes=None) -> Result:
    exe = shutil.which("curl.exe") or shutil.which("curl")
    if not exe:
        raise net.HTTPError("curl not found")
    fd, out = tempfile.mkstemp(prefix="hsa_curl_")
    os.close(fd)
    try:
        cmd = [exe, "-sS", "-L", "--negotiate", "-u", ":", "-o", out, "--max-time", str(int(timeout or 60)),
               "-w", "%{http_code}|%{content_type}|%{url_effective}"]
        if cfg.get("verify_ssl") is False:
            cmd.append("-k")
        if cfg.get("ca_bundle"):
            cmd += ["--cacert", cfg["ca_bundle"]]
        if cfg.get("client_cert"):
            cmd += ["--cert", cfg["client_cert"]] + (["--key", cfg["client_key"]] if cfg.get("client_key") else [])
        if cfg.get("cookie"):
            cmd += ["-b", cfg["cookie"]]
        for k, v in (cfg.get("headers") or {}).items():
            cmd += ["-H", "%s: %s" % (k, v)]
        if not cfg.get("use_env_proxy", False):
            cmd += ["--noproxy", "*"]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, timeout=(timeout or 60) + 10)
        info = r.stdout.decode("utf-8", "replace").strip().split("|")
        if r.returncode != 0:
            raise net.HTTPError("curl failed (%d): %s" % (r.returncode, r.stderr.decode("utf-8", "replace")[:300]))
        status = int(info[0]) if info and info[0].isdigit() else 0
        with open(out, "rb") as f:
            body = f.read(max_bytes + 1 if max_bytes else -1)
        return status, {"Content-Type": info[1] if len(info) > 1 else "",
                        "X-Final-Url": info[2] if len(info) > 2 else url}, body
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


_PS = ("$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; "
       "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}; "
       "$r = Invoke-WebRequest -Uri $env:HSA_URL -OutFile $env:HSA_OUT -PassThru -UseDefaultCredentials "
       "-UseBasicParsing -TimeoutSec $env:HSA_TIMEOUT; "
       "Write-Output ([string]$r.StatusCode + '|' + [string]$r.Headers['Content-Type'])")


def fetch_powershell(url, cfg, timeout=None, max_bytes=None) -> Result:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise net.HTTPError("PowerShell not found")
    fd, out = tempfile.mkstemp(prefix="hsa_ps_")
    os.close(fd)
    try:
        env = dict(os.environ, HSA_URL=url, HSA_OUT=out, HSA_TIMEOUT=str(int(timeout or 60)))
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", _PS], capture_output=True, env=env,
                           timeout=(timeout or 60) + 20)
        if r.returncode != 0:
            raise net.HTTPError("PowerShell fetch failed: %s" % r.stderr.decode("utf-8", "replace")[:400])
        info = r.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        parts = info[0].split("|")
        with open(out, "rb") as f:
            body = f.read(max_bytes + 1 if max_bytes else -1)
        st = int(parts[0]) if parts[0].isdigit() else 200
        return st, {"Content-Type": parts[1] if len(parts) > 1 else ""}, body
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _browser_flags() -> List[str]:
    allow = settings().get("auth_server_allowlist") or "*"
    return ["--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--auth-server-allowlist=%s" % allow, "--auth-negotiate-delegate-allowlist=%s" % allow]


_HEADLESS: Dict[str, Any] = {}


def fetch_headless(url, cfg, timeout=None, max_bytes=None) -> Result:
    """A private, invisible browser (started on first use, reused, closed at exit).
    Runs JavaScript; sends your Windows login to intranet hosts; can't do
    interactive smart-card prompts (use 'cdp' with `hsa browser` for that)."""
    if not _HEADLESS or _HEADLESS["proc"].poll() is not None:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        cdp = CDP(port)
        proc = cdp.launch(headless=True, profile="headless", ignore_cert_errors=cfg.get("verify_ssl") is False)
        _HEADLESS.update(cdp=cdp, proc=proc)
        import atexit
        atexit.register(_stop_headless)
    return _HEADLESS["cdp"].get(url, timeout or 60, max_bytes, auto_launch=False)


def _stop_headless():
    p = _HEADLESS.get("proc")
    if p is not None and p.poll() is None:
        p.kill()


# ------------------------------------------------- minimal WebSocket client -----
class WebSocket:
    """Just enough RFC 6455 for the DevTools protocol (text frames, client masking)."""

    def __init__(self, url: str, timeout: float = 30):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (path, u.hostname, u.port or 80, key)
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            resp += chunk
        head, self._buf = resp.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("websocket handshake rejected: %s" % head[:200])

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str):
        data = text.encode("utf-8")
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(hdr) + masked)

    def recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x9:  # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + b"\0\0\0\0" + payload)
                continue
            if op == 0x8:
                raise ConnectionError("websocket closed by browser")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + b"\0\0\0\0")
        except OSError:
            pass
        self.sock.close()


# --------------------------------------------------------- DevTools (CDP) -----
class CDP:
    def __init__(self, port: Optional[int] = None):
        self.port = int(port or settings().get("cdp_port", 9222))
        self.base = "http://127.0.0.1:%d" % self.port
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _http(self, path: str, method: str = "GET", timeout: float = 5) -> Any:
        req = urllib.request.Request(self.base + path, method=method)
        with self._opener.open(req, timeout=timeout) as r:
            data = r.read()
        try:
            return json.loads(data)
        except ValueError:
            return data.decode("utf-8", "replace")

    def available(self) -> bool:
        try:
            return "Browser" in self._http("/json/version", timeout=2)
        except Exception:
            return False

    def launch(self, headless: bool = False, url: str = "about:blank", profile: str = "cdp",
               ignore_cert_errors: bool = False) -> subprocess.Popen:
        exe = find_browser()
        if not exe:
            raise net.HTTPError("No Edge/Chrome found (set web.browser_path)")
        cmd = [exe, "--remote-debugging-port=%d" % self.port, "--remote-allow-origins=*",
               "--user-data-dir=%s" % profile_dir(profile)] + _browser_flags()
        if headless:
            cmd += ["--headless=new", "--disable-gpu"]
        if ignore_cert_errors:
            cmd.append("--ignore-certificate-errors")
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.append("--no-sandbox")
        cmd.append(url)
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        for _ in range(60):
            if self.available():
                return proc
            time.sleep(0.25)
        raise net.HTTPError("browser started but DevTools port %d did not open" % self.port)

    def ensure(self, auto_launch: Optional[bool] = None) -> None:
        if self.available():
            return
        if settings().get("auto_launch", True) if auto_launch is None else auto_launch:
            proc = self.launch(headless=True)
            import atexit
            atexit.register(lambda: proc.poll() is None and proc.kill())
            return
        raise net.HTTPError("No browser is listening on DevTools port %d. Run: python -m hsa browser" % self.port)

    def get(self, url: str, timeout: float = 60, max_bytes: Optional[int] = None,
            auto_launch: Optional[bool] = None) -> Result:
        self.ensure(auto_launch)
        try:
            tab = self._http("/json/new?about:blank", method="PUT")
        except Exception:
            tab = self._http("/json/new?about:blank")
        ws = WebSocket(tab["webSocketDebuggerUrl"], timeout=timeout)
        session = _CDPSession(ws)
        try:
            session.call("Page.enable")
            session.call("Network.enable")
            nav = session.call("Page.navigate", {"url": url})
            if nav.get("errorText"):
                raise net.HTTPError("browser could not open %s: %s" % (url, nav["errorText"]))
            frame = nav.get("frameId")
            status, mime = 200, "text/html"
            deadline = time.time() + timeout
            loaded = False
            while time.time() < deadline and not loaded:
                ev = session.next_event(deadline - time.time())
                if ev is None:
                    break
                m, p = ev.get("method"), ev.get("params", {})
                if m == "Network.responseReceived" and p.get("type") == "Document" and p.get("frameId") == frame:
                    status = int(p["response"].get("status", 200))
                    mime = p["response"].get("mimeType", mime)
                elif m in ("Page.loadEventFired", "Page.frameStoppedLoading"):
                    loaded = True
            if "html" not in mime and "xml" not in mime and "text/plain" not in mime:
                body = self._download_in_page(session, url)
                return status, {"Content-Type": mime}, body[: (max_bytes or len(body))]
            self._settle(session, float(settings().get("render_wait", 3)))
            final = session.eval("location.href") or url
            html = session.eval("document.documentElement.outerHTML") or ""
            body = html.encode("utf-8")
            return status, {"Content-Type": "text/html; charset=utf-8", "X-Final-Url": final}, \
                body[: (max_bytes or len(body))]
        finally:
            ws.close()
            try:
                self._http("/json/close/%s" % tab["id"])
            except Exception:
                pass

    @staticmethod
    def _settle(session, max_wait: float):
        """Wait until the page text stops changing (JS-rendered results)."""
        last, stable, t0 = -1, 0, time.time()
        while time.time() - t0 < max_wait:
            n = session.eval("document.body ? document.body.innerText.length : 0") or 0
            stable = stable + 1 if n == last and n > 0 else 0
            if stable >= 2:
                return
            last = n
            time.sleep(0.4)

    @staticmethod
    def _download_in_page(session, url) -> bytes:
        js = ("(async () => { const r = await fetch(%s, {credentials: 'include'}); "
              "const b = new Uint8Array(await r.arrayBuffer()); let s = ''; "
              "for (let i = 0; i < b.length; i += 32768) { s += String.fromCharCode.apply(null, b.subarray(i, i + 32768)); } "
              "return btoa(s); })()") % json.dumps(url)
        b64 = session.eval(js, await_promise=True)
        return base64.b64decode(b64 or "")


class _CDPSession:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.n = 0
        self.events: List[Dict[str, Any]] = []

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise net.HTTPError("CDP %s failed: %s" % (method, msg["error"].get("message")))
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def next_event(self, timeout: float) -> Optional[Dict[str, Any]]:
        if self.events:
            return self.events.pop(0)
        self.ws.sock.settimeout(max(0.1, timeout))
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if "method" in msg:
                    return msg
        except (socket.timeout, TimeoutError):
            return None

    def eval(self, expr: str, await_promise: bool = False) -> Any:
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_promise})
        if r.get("exceptionDetails"):
            raise net.HTTPError("page script failed: %s" % r["exceptionDetails"].get("text"))
        return (r.get("result") or {}).get("value")


def fetch_cdp(url, cfg, timeout=None, max_bytes=None) -> Result:
    return CDP().get(url, timeout or 60, max_bytes)


FETCHERS = {"python": fetch_python, "curl": fetch_curl, "powershell": fetch_powershell,
            "headless": fetch_headless, "cdp": fetch_cdp}


def available_methods() -> List[str]:
    out = ["python"]
    if shutil.which("curl.exe") or shutil.which("curl"):
        out.append("curl")
    if shutil.which("powershell") or shutil.which("pwsh"):
        out.append("powershell")
    if find_browser():
        out += ["cdp", "headless"]
    elif CDP().available():
        out.append("cdp")
    return out


def web_get(url: str, cfg: Dict[str, Any], timeout=None, max_bytes=None, via: str = "auto") -> Result:
    """GET a URL with the configured method, or try methods in order ('auto')."""
    if via in FETCHERS:
        return FETCHERS[via](url, cfg, timeout, max_bytes)
    host = _host(url)
    avail = available_methods()
    order = [m for m in (settings().get("auto_order") or AUTO_ORDER) if m in avail]
    known = _remembered().get(host)
    if known in order:
        order.remove(known)
        order.insert(0, known)
    problems = []
    fallback: Optional[Result] = None
    for m in order:
        try:
            res = FETCHERS[m](url, cfg, timeout, max_bytes)
        except (net.HTTPError, OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
            problems.append("%s: %s" % (m, str(e)[:150]))
            continue
        if res[0] in (404, 410):
            return res  # the page really doesn't exist - other methods won't help
        issue = classify(res[2], res[0], res[1].get("X-Final-Url", ""), rendered=m in ("cdp", "headless"))
        if not issue:
            _remember(host, m)
            return res
        problems.append("%s: %s" % (m, issue))
        if fallback is None and res[0] < 400 and "JavaScript" in issue:
            fallback = res
    if fallback is not None:
        return fallback  # a genuinely script-heavy page with little text - best effort
    hint = ""
    if any("login" in p for p in problems):
        hint = ("\nThe site wants you to log in. Run `python -m hsa browser`, log in in that window, keep it open, "
                "and set \"web\": {\"via\": \"cdp\"} (or leave \"auto\").")
    raise net.HTTPError("All web access methods failed for %s:\n  %s%s" % (url, "\n  ".join(problems), hint))


# ---------------------------------------------------------------- webtest -----
def webtest(url: str, query: Optional[str] = None, methods: Optional[List[str]] = None, cfg=None) -> None:
    from hsa.extract import html_to_text
    from hsa.research.sources import HtmlSource
    cfg = dict(cfg or {})
    target = url.replace("{query}", urllib.parse.quote_plus(query or "test")).replace("{page}", "1")
    print("Testing web access to: %s" % target)
    print("Browser: %s" % (find_browser() or "not found"))
    print("-" * 78)
    working = []
    bodies = {}
    for m in methods or METHODS:
        t0 = time.time()
        try:
            st, hdr, body = FETCHERS[m](target, cfg, 60, 15000000)
        except Exception as e:
            print("%-11s FAIL  %s" % (m, str(e).splitlines()[0][:150]))
            continue
        issue = classify(body, st, hdr.get("X-Final-Url", ""), rendered=m in ("cdp", "headless"))
        title, text, links = html_to_text(body.decode("utf-8", "replace"), target)
        print("%-11s %s  HTTP %s  %7d bytes  %4.1fs  title=%r  text=%d chars  links=%d%s" % (
            m, "OK  " if not issue else "WARN", st, len(body), time.time() - t0, title[:40], len(text), len(links),
            ("  <- " + issue) if issue else ""))
        if not issue:
            working.append(m)
            bodies[m] = body
    print("-" * 78)
    if not working:
        print("No method returned real content. Next steps:")
        print("  1. Run `python -m hsa browser`, log in to the site in that window, keep it open,")
        print("     then re-run this test - the 'cdp' method uses that logged-in browser.")
        print("  2. Check web.browser_path, and that the URL opens in a normal browser.")
        return
    best = working[0]
    print("Working methods: %s.  Recommended:  \"web\": {\"via\": \"%s\"}" % (", ".join(working), best))
    if "{query}" in url:
        src = HtmlSource("test", {"url": url, "_web": True, "via": best})
        try:
            res = src.search(query or "test")
        except Exception as e:
            print("Result extraction failed: %s" % e)
            return
        print("\nExtracted %d candidate results (heuristic). First 10:" % len(res))
        for r in res[:10]:
            print("  - %s\n    %s" % (r.title[:80], r.url[:120]))
        groups: Dict[str, int] = {}
        for r in res:
            p = urllib.parse.urlparse(r.url)
            key = p.netloc + "/" + (p.path.strip("/").split("/")[0] if p.path.strip("/") else "")
            groups[key] = groups.get(key, 0) + 1
        print("\nResult URLs grouped by host/first path segment:")
        for k, n in sorted(groups.items(), key=lambda x: -x[1])[:8]:
            print("  %3d  %s" % (n, k))
        print("If real results share a pattern, set \"include_url_regex\" on the source (e.g. the busiest group above)\n"
              "so navigation/ads/facet links are ignored.")
