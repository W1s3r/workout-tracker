"""Shared stdlib HTTP helpers (urllib + ssl). No `requests` dependency.

Supports: custom CA bundles, PKI client certificates, disabling verification
(not recommended), bypassing environment proxies, cookies and extra headers.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, Optional, Tuple


class HTTPError(Exception):
    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


def make_ssl_context(verify: bool = True, ca_bundle: Optional[str] = None,
                     client_cert: Optional[str] = None, client_key: Optional[str] = None) -> ssl.SSLContext:
    if verify:
        ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if client_cert:
        ctx.load_cert_chain(client_cert, keyfile=client_key or None)
    return ctx


def build_opener(cfg: Dict[str, Any]) -> urllib.request.OpenerDirector:
    ctx = make_ssl_context(cfg.get("verify_ssl", True), cfg.get("ca_bundle"),
                           cfg.get("client_cert"), cfg.get("client_key"))
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if not cfg.get("use_env_proxy", False):
        handlers.append(urllib.request.ProxyHandler({}))
    if cfg.get("basic_auth"):
        user, pw = cfg["basic_auth"]
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, cfg.get("auth_url") or "", user, pw)
        handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
    return urllib.request.build_opener(*handlers)


# Web-access settings (config["web"]); set by configure_web() at startup.
WEB: Dict[str, Any] = {"via": "auto"}
WEB_HOME = __import__("pathlib").Path.home() / ".hsa"


def configure_web(config) -> None:
    global WEB_HOME
    WEB.clear()
    WEB.update(config.get("web") or {})
    WEB_HOME = config.home


def request(url: str, cfg: Dict[str, Any], method: str = "GET", data: Optional[bytes] = None,
            headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
            stream: bool = False, max_bytes: Optional[int] = None):
    """Perform a request. Returns (status, headers, body_bytes) or, if stream=True,
    the raw response object (caller must close).

    GETs made for web content (search sources, page fetching: cfg["_web"] is set)
    go through the configured web-access method (see hsa/webaccess.py)."""
    if method == "GET" and not stream and data is None:
        if cfg.get("http_command"):
            return _command_get(url, cfg, timeout, max_bytes)
        via = cfg.get("via") or (WEB.get("via", "auto") if cfg.get("_web") else "python")
        if via != "python":
            from hsa import webaccess
            if headers:
                cfg = dict(cfg, headers=dict(cfg.get("headers") or {}, **headers))
            st, hd, body = webaccess.web_get(url, cfg, timeout or cfg.get("timeout", 60), max_bytes, via)
            if st >= 400:
                raise HTTPError("HTTP %s from %s" % (st, url), st, body[:2000].decode("utf-8", "replace"))
            return st, hd, body
    return _urllib_request(url, cfg, method, data, headers, timeout, stream, max_bytes)


def _urllib_request(url, cfg, method="GET", data=None, headers=None, timeout=None, stream=False, max_bytes=None):
    hdrs = dict(cfg.get("headers") or {})
    if cfg.get("cookie"):
        hdrs["Cookie"] = cfg["cookie"]
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    opener = build_opener(cfg)
    try:
        resp = opener.open(req, timeout=timeout or cfg.get("timeout", 60))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:4000]
        except Exception:
            pass
        raise HTTPError("HTTP %s from %s: %s" % (e.code, url, body[:500]), e.code, body)
    except urllib.error.URLError as e:
        raise HTTPError("Could not reach %s: %s" % (url, e.reason))
    if stream:
        return resp
    with resp:
        body = resp.read(max_bytes + 1 if max_bytes else -1)
        return resp.status, dict(resp.headers), body


def _command_get(url: str, cfg: Dict[str, Any], timeout=None, max_bytes=None):
    """GET via an external command - for sites needing Windows integrated auth
    (Kerberos/NTLM) or smart-card PKI that Python's stdlib can't do. Example:
      "http_command": "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/win_fetch.ps1 -Url {url} -Out {out}"
    The command must write the response body to {out}."""
    import os
    import shlex
    import subprocess
    import tempfile
    fd, out = tempfile.mkstemp(prefix="hsa_get_")
    os.close(fd)
    try:
        cmd = cfg["http_command"]
        argv = [a.replace("{url}", url).replace("{out}", out) for a in shlex.split(cmd, posix=os.name != "nt")]
        argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in argv]
        r = subprocess.run(argv, capture_output=True, timeout=timeout or cfg.get("timeout", 60))
        if r.returncode != 0:
            raise HTTPError("http_command failed for %s: %s" % (url, r.stderr.decode("utf-8", "replace")[:500]))
        with open(out, "rb") as f:
            body = f.read(max_bytes + 1 if max_bytes else -1)
        ctype = "text/html" if body.lstrip()[:1] == b"<" else ""
        return 200, {"Content-Type": ctype}, body
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def post_json(url: str, payload: Dict[str, Any], cfg: Dict[str, Any], headers=None,
              timeout=None, stream=False):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    data = json.dumps(payload).encode("utf-8")
    return request(url, cfg, "POST", data, h, timeout, stream)


def get_json(url: str, cfg: Dict[str, Any], headers=None, timeout=None) -> Any:
    h = {"Accept": "application/json"}
    h.update(headers or {})
    status, _, body = request(url, cfg, "GET", None, h, timeout)
    return json.loads(body.decode("utf-8", "replace"))


def iter_sse(resp) -> Iterator[str]:
    """Yield `data:` payloads from a Server-Sent-Events response."""
    buf = b""
    while True:
        chunk = resp.readline()
        if not chunk:
            break
        line = chunk.strip()
        if not line:
            continue
        if line.startswith(b"data:"):
            yield line[5:].strip().decode("utf-8", "replace")
    if buf:
        yield buf.decode("utf-8", "replace")


def with_retries(fn, retries: int = 3, base_delay: float = 2.0, on_retry=None):
    last = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except HTTPError as e:
            last = e
            retriable = e.status in (0, 408, 409, 429, 500, 502, 503, 504)
            if not retriable or attempt == retries:
                raise
        except (TimeoutError, ConnectionError, OSError) as e:
            last = e
            if attempt == retries:
                raise HTTPError(str(e))
        delay = base_delay * (2 ** attempt)
        if on_retry:
            on_retry(attempt + 1, delay, last)
        time.sleep(delay)
    raise last  # pragma: no cover


def quote(s: str) -> str:
    return urllib.parse.quote_plus(s)
