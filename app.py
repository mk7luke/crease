#!/usr/bin/env python3
"""Crease: preview a public URL on iPhone Duo cover vs inner viewports."""
from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

PUBLIC_HOST = os.environ.get("CREASE_PUBLIC_HOST", "https://crease.hermz.xyz")
BIND_HOST = os.environ.get("CREASE_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("CREASE_PORT", "8880"))

MAX_BODY = 1024 * 1024
FETCH_TIMEOUT = 10
MAX_REDIRECTS = 4
RATE_WINDOW = 3600
RATE_MAX = 20
ALLOWED_PORTS = {80, 443}

# Simulator / FoldHunt (Oct 2026): CSS points, not pixels.
COVER = {"w": 466, "h": 678, "label": "Cover", "size": "5.4 in", "ppi": 460}
INNER_OPEN = {"w": 951, "h": 669, "label": "Inner open", "size": "7.6 in", "ppi": 430}
INNER_UPRIGHT = {"w": 669, "h": 951, "label": "Inner upright", "size": "7.6 in", "ppi": 430}
CREASE_PT = 40

_BLOCKED_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("2001:db8::/32"),
]

_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google.internal.",
}

_page_cache: dict[str, tuple[float, str]] = {}
_rate: dict[str, list[float]] = {}
_fetch_cache: dict[str, tuple[float, dict[str, Any]]] = {}
FETCH_CACHE_TTL = 45.0

_HEAD_RE = re.compile(r"<head\b[^>]*>", re.I)
_HTML_RE = re.compile(r"<html\b[^>]*>", re.I)
_BASE_RE = re.compile(r"<base\b[^>]*>", re.I)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    if ip.is_unspecified or getattr(ip, "is_site_local", False):
        return True
    for net in _BLOCKED_NETS:
        if ip in net:
            return True
    return False


def _host_blocked(host: str) -> bool:
    h = host.strip(".").lower()
    if h in _BLOCKED_HOSTS:
        return True
    if h.endswith(".local") or h.endswith(".internal") or h.endswith(".localhost"):
        return True
    if h.endswith(".arpa"):
        return True
    return False


def _assert_public_url(raw: str) -> tuple[str, str, int]:
    if not raw or not isinstance(raw, str):
        raise HTTPException(400, "need a url")
    raw = raw.strip()
    if len(raw) > 2048:
        raise HTTPException(400, "url too long")
    if "\n" in raw or "\r" in raw or "\x00" in raw:
        raise HTTPException(400, "bad url")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "only http and https")
    if parsed.username or parsed.password:
        raise HTTPException(400, "no credentials in url")
    host = parsed.hostname
    if not host:
        raise HTTPException(400, "no host")
    if _host_blocked(host):
        raise HTTPException(400, "that host is not allowed")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal and _ip_blocked(ipaddress.ip_address(host)):
        raise HTTPException(400, "private addresses are blocked")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port not in ALLOWED_PORTS:
        raise HTTPException(400, "only ports 80 and 443")
    return raw, host, port


def _resolve_public(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise HTTPException(400, f"dns failed: {e}") from e
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip = ipaddress.ip_address(sockaddr[0])
        if _ip_blocked(ip):
            raise HTTPException(400, "host resolves to a private address")
        ips.append(str(ip))
    if not ips:
        raise HTTPException(400, "no addresses")
    return ips


def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "0.0.0.0"


def _rate_ok(ip: str) -> bool:
    now = time.time()
    arr = _rate.setdefault(ip, [])
    arr[:] = [t for t in arr if now - t < RATE_WINDOW]
    if len(arr) >= RATE_MAX:
        return False
    arr.append(now)
    return True


def _fetch(url: str) -> dict[str, Any]:
    current = url
    hops: list[str] = []
    opener = urllib.request.build_opener(NoRedirect)
    last_status = 0
    last_headers: dict[str, str] = {}
    body = b""
    final = current
    for _ in range(MAX_REDIRECTS + 1):
        _assert_public_url(current)
        host = urlparse(current).hostname or ""
        _resolve_public(host)
        req = urllib.request.Request(
            current,
            method="GET",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 27_1 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.1 Mobile/15E148 Safari/604.1"
                ),
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en",
            },
        )
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                last_status = getattr(resp, "status", 200) or 200
                last_headers = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.read(MAX_BODY + 1)
                final = resp.geturl() or current
                break
        except urllib.error.HTTPError as e:
            last_status = e.code
            last_headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            loc = e.headers.get("Location") if e.headers else None
            if last_status in {301, 302, 303, 307, 308} and loc:
                nxt = urljoin(current, loc)
                hops.append(nxt)
                current = nxt
                continue
            body = e.read(MAX_BODY + 1) if e.fp else b""
            final = current
            break
        except TimeoutError as e:
            raise HTTPException(504, "fetch timed out") from e
        except urllib.error.URLError as e:
            raise HTTPException(502, f"fetch failed: {e.reason}") from e
    else:
        raise HTTPException(400, "too many redirects")
    truncated = len(body) > MAX_BODY
    if truncated:
        body = body[:MAX_BODY]
    return {
        "url": final,
        "requested": url,
        "status": last_status,
        "headers": last_headers,
        "body": body,
        "truncated": truncated,
        "redirects": hops,
    }


def _fetch_cached(url: str) -> dict[str, Any]:
    now = time.time()
    hit = _fetch_cache.get(url)
    if hit and now - hit[0] < FETCH_CACHE_TTL:
        return hit[1]
    data = _fetch(url)
    _fetch_cache[url] = (now, data)
    if len(_fetch_cache) > 64:
        cutoff = now - FETCH_CACHE_TTL
        for key, (ts, _) in list(_fetch_cache.items()):
            if ts < cutoff:
                _fetch_cache.pop(key, None)
    return data


def _charset(headers: dict[str, str], body: bytes) -> str:
    ctype = headers.get("content-type", "")
    m = re.search(r"charset=([A-Za-z0-9._-]+)", ctype, re.I)
    if m:
        return m.group(1)
    head = body[:800].decode("ascii", "ignore").lower()
    m = re.search(r"charset=['\"]?([a-z0-9._-]+)", head)
    if m:
        return m.group(1)
    return "utf-8"


def _base_href(final_url: str) -> str:
    parsed = urlparse(final_url)
    path = parsed.path or "/"
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _inject_base(html: str, href: str) -> str:
    tag = f'<base href="{html_lib.escape(href, quote=True)}">'
    html = _BASE_RE.sub("", html)
    if _HEAD_RE.search(html):
        return _HEAD_RE.sub(lambda m: m.group(0) + tag, html, count=1)
    if _HTML_RE.search(html):
        return _HTML_RE.sub(lambda m: m.group(0) + f"<head>{tag}</head>", html, count=1)
    return f"<head>{tag}</head>" + html


def _asset_version(*paths: Path) -> str:
    m = 0.0
    for p in paths:
        try:
            m = max(m, p.stat().st_mtime)
        except OSError:
            pass
    return str(int(m))


def _render_html(name: str) -> str:
    html_path = STATIC / name
    css_path = STATIC / "style.css"
    js_path = STATIC / "home.js"
    stamp = max(
        html_path.stat().st_mtime,
        css_path.stat().st_mtime if css_path.exists() else 0.0,
        js_path.stat().st_mtime if js_path.exists() else 0.0,
    )
    cached = _page_cache.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    html = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    html = re.sub(
        r"""[ \t]*<link[^>]+href=["']/static/style\.css[^"']*["'][^>]*>\s*""",
        "",
        html,
        flags=re.I,
    )
    injection = f'<style id="crease-css">\n{css}\n</style>\n'
    if re.search(r"</head>", html, flags=re.I):
        html = re.sub(r"</head>", injection + "</head>", html, count=1, flags=re.I)
    else:
        html = injection + html
    v = _asset_version(js_path, css_path)
    html = re.sub(
        r"""src=(["'])/static/home\.js(?:\?[^"']*)?\1""",
        rf"src=\1/static/home.js?v={v}\1",
        html,
    )
    _page_cache[name] = (stamp, html)
    return html


app = FastAPI(title="Crease", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def _headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    path = request.url.path
    if path.startswith("/frame"):
        if "x-frame-options" in resp.headers:
            del resp.headers["x-frame-options"]
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    else:
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-src 'self'; "
            "base-uri 'none'; "
            "form-action 'self'"
        )
    if path in {"/", "/index.html"}:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "crease",
        "cover": COVER,
        "inner_open": INNER_OPEN,
        "inner_upright": INNER_UPRIGHT,
        "crease_pt": CREASE_PT,
    }


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(_render_html("index.html"))


@app.get("/robots.txt")
def robots() -> Response:
    return Response(
        "User-agent: *\nAllow: /\nDisallow: /frame\nDisallow: /api/\n",
        media_type="text/plain",
    )


@app.get("/api/spec")
def spec() -> JSONResponse:
    return JSONResponse(
        {
            "cover": COVER,
            "inner_open": INNER_OPEN,
            "inner_upright": INNER_UPRIGHT,
            "crease_pt": CREASE_PT,
            "note": "CSS points from the iPhone Duo simulator profile. Crease is a 40-point band, not half the width.",
        }
    )


@app.post("/api/preview")
async def preview(request: Request) -> JSONResponse:
    ip = _client_ip(request)
    if not _rate_ok(ip):
        raise HTTPException(429, "rate limit: 20 previews per hour")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(400, "json body required") from e
    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str):
        raise HTTPException(400, "need {url}")
    raw, _, _ = _assert_public_url(url)
    fetched = await asyncio.to_thread(_fetch_cached, raw)
    ctype = fetched["headers"].get("content-type", "")
    is_html = "html" in ctype.lower() or fetched["body"][:64].lstrip().lower().startswith(
        (b"<!doctype", b"<html")
    )
    frame = f"/frame?url={quote(fetched['url'], safe='')}"
    return JSONResponse(
        {
            "ok": True,
            "requested": fetched["requested"],
            "final_url": fetched["url"],
            "status": fetched["status"],
            "content_type": ctype,
            "bytes": len(fetched["body"]),
            "truncated": fetched["truncated"],
            "redirects": fetched["redirects"],
            "html": bool(is_html),
            "frame": frame,
            "cover": COVER,
            "inner_open": INNER_OPEN,
            "inner_upright": INNER_UPRIGHT,
            "crease_pt": CREASE_PT,
        }
    )


@app.get("/frame")
def frame(url: str) -> Response:
    raw, _, _ = _assert_public_url(url)
    fetched = _fetch_cached(raw)
    ctype = fetched["headers"].get("content-type", "text/html")
    status = fetched["status"] or 200
    if status >= 400 and not fetched["body"]:
        raise HTTPException(status, f"upstream {status}")
    is_html = "html" in ctype.lower() or fetched["body"][:64].lstrip().lower().startswith(
        (b"<!doctype", b"<html")
    )
    if not is_html:
        escaped = html_lib.escape(fetched["url"])
        body = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Not HTML</title></head><body style='font:16px sans-serif;padding:24px'>"
            f"<p>This URL is not HTML ({html_lib.escape(ctype)}).</p>"
            f"<p><code>{escaped}</code></p></body></html>"
        )
        return HTMLResponse(body)
    try:
        text = fetched["body"].decode(_charset(fetched["headers"], fetched["body"]), "replace")
    except LookupError:
        text = fetched["body"].decode("utf-8", "replace")
    text = _inject_base(text, _base_href(fetched["url"]))
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return HTMLResponse(text, headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=BIND_HOST, port=BIND_PORT)
