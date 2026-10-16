"""HTTP fetches for feeds and article pages, plus reader-view extraction (trafilatura).

URLs come from feeds, so they are untrusted: only http(s) to public addresses (never
loopback, LAN or the tailnet, including after a redirect), a total time limit, and a size cap.
"""
import ipaddress
import json
import socket
import time
import urllib.request
from urllib.parse import urlparse

UA = "StudyFeed/1.0 (personal RSS reader)"
MAX_BYTES = 2_000_000           # article pages
FEED_MAX_BYTES = 10_000_000     # some full-content feeds (Dan Luu, Mercari) are bigger
TIMEOUT = 15     # seconds per connect/read
DEADLINE = 30    # seconds for the whole download


class FetchError(Exception):
    pass


def _check_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError(f"not an http(s) url: {url[:200]}")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except OSError as e:
        raise FetchError(f"cannot resolve {p.hostname}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:  # also covers 100.64.0.0/10, i.e. Tailscale
            raise FetchError(f"{p.hostname} resolves to a non-public address")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def http_get(url: str, accept: tuple[str, ...] | None = None, max_bytes: int = MAX_BYTES) -> bytes:
    """GET url. Raises FetchError when it is over max_bytes, too slow, not public, or
    (with `accept`) its Content-Type doesn't contain one of those strings."""
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    start = time.monotonic()
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if accept and not any(a in ctype for a in accept):
                raise FetchError(f"unsupported content type {ctype or '(none)'}")
            too_big = FetchError(f"page is over {max_bytes // 1_000_000} MB")
            if int(r.headers.get("Content-Length") or 0) > max_bytes:
                raise too_big
            chunks, size = [], 0
            while chunk := r.read(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise too_big
                if time.monotonic() - start > DEADLINE:
                    raise FetchError(f"download took over {DEADLINE}s")
                chunks.append(chunk)
            return b"".join(chunks)
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"{type(e).__name__}: {e}")


def reader_view(url: str) -> dict | None:
    """{"title", "text"} for the article at url, paragraphs separated by blank lines,
    or None when nothing readable could be extracted."""
    import trafilatura  # here, so the API still starts on an image built without it
    page = http_get(url, accept=("html", "xml", "text/plain"))
    raw = trafilatura.extract(page, url=url, output_format="json", with_metadata=True,
                              include_comments=False, include_images=False, include_links=False,
                              include_tables=True, deduplicate=True)
    if not raw:
        return None
    doc = json.loads(raw)
    paragraphs = [p.strip() for p in (doc.get("text") or "").splitlines() if p.strip()]
    if not paragraphs:
        return None
    return {"title": (doc.get("title") or "").strip() or None, "text": "\n\n".join(paragraphs)}
