#!/usr/bin/env python3
"""
WebDAV Direct File Access for Cordée — Lane C.

Implements a minimal WebDAV provider as Flask routes under /dav/<slug>/<rel>.
Reuses agent_files security checks (_is_writable_rel, _safe_resolve, forbidden)
for every mutating method. Exposes file_tags as deadprops (aingel:tags / aingel:note)
from the per-project file_tags table.

Auth: AINGEL_DAV_TOKEN (Basic/Bearer) is the primary mechanism, used by both
the integrated mount (cordee.example/dav/<slug>/) and an optional dedicated
origin such as dav.cordee.example. There is NO loopback bypass: agent tools (bash,
claude/vibe CLIs) run on this host, so trusting 127.0.0.1 would give any task
unauthenticated read/write access to every tenant's project. Without a
configured token every request is denied (fail closed).

Forward-auth identity headers (X-Forwarded-User, X-Authentik-Username, …) are
only honoured when AINGEL_DAV_TRUST_HEADERS is enabled. They are trustworthy
only behind a proxy that both injects them and strips inbound copies; since
the Authentik outpost was removed from the request path (2026-09-17), any
client reaching the app can otherwise forge them — so they are ignored by
default. Re-enable only when such a proxy is back in front.

Routing decision: the task spec suggests a separate WSGI app on 127.0.0.1:8002
behind a dedicated dav.* host. This file instead mounts WebDAV on the **existing**
Flask app (port 8001) under /dav/. Rationale:
  - Reuses the same process, same Authentik SSO (no split-brain UFW / tunnel),
  - No extra systemd unit to keep in sync,
  - Security checks are identical (same agent_files imports),
  - If a dedicated 8002 is desired later, this module can be run standalone
    via `python agent_webdav.py` (see __main__ below) — the cloudflared line
    `dav.cordee.example -> http://localhost:8002` is documented in ops/.
  Deviation is documented in SETUP.md / READMEFIRST.md.

Supported verbs: OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, MKCOL, DELETE,
MOVE, COPY, LOCK, UNLOCK. PROPFIND returns 207 Multi-Status with standard
DAV: properties + aingel: deadprops. Depth 0/1/infinity (infinity capped to 1).

Security: every mutating verb validates the target rel with
agent_files._is_writable_rel + _safe_resolve (symlink containment, forbidden,
writable-root). GET/PROPFIND allow reading any non-forbidden file but still
block .db/.env/.git-hidden paths and enforce realpath containment.
"""
import base64
import hmac
import logging
import os
import re
import mimetypes
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import escape as _he

import agent_db as db
import agent_files

_log = logging.getLogger(__name__)


def _log_error(fmt, *args):
    """Log a full traceback for a server-side error (webdav PUT 500s were
    previously silent — nothing was ever logged)."""
    try:
        _log.error(fmt, *args)
    except Exception:
        pass


def _tombstone_has_sha(project_path, sha256):
    """True if any delete-tombstone carries this content sha256 (refuse to PUT
    a file whose content was soft-deleted — 'deleted stays deleted')."""
    try:
        import agent_db
        return agent_db.tombstone_sha_active(project_path, sha256)
    except Exception:
        return False

import agent_config
from agent_importer import slugify as _slugify

# ── Constants ────────────────────────────────────────────────────────────────
AINGEL_NS = "http://cordee-app.io/ns"
DAV_NS = "DAV:"
AUTH_HEADERS = (
    "X-Forwarded-User",
    "X-Authentik-Username",
    "X-Authentik-User",
    "X-Forwarded-Email",
    "Remote-User",
    "Cf-Access-Authenticated-User-Email",
    "X-Authentik-Email",
)
DAV_METHODS = ["OPTIONS", "PROPFIND", "PROPPATCH", "GET", "HEAD", "PUT", "MKCOL", "DELETE", "MOVE", "COPY", "LOCK", "UNLOCK"]

# Cache for PROPFIND content-type detection
_mime_inited = False

def _ensure_mime():
    global _mime_inited
    if not _mime_inited:
        mimetypes.init()
        _mime_inited = True

# ── Project slug lookup ────────────────────────────────────────────────────

def _get_project_by_slug(slug: str):
    """Return project dict for slug, or None. Slug is lowercased hyphenated name."""
    if not slug:
        return None
    slug = slug.strip().lower()
    # Fast path: central DB lookup by slug column
    try:
        conn = db.get_db()
        row = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception:
        pass
    # Fallback: scan all and compare slugified name (handles legacy rows with different slug)
    try:
        for p in db.get_projects():
            if (p.get("slug") or "").lower() == slug:
                return p
            # Also try slugify(name) match for "Example Client" -> example-client
            try:
                if _slugify(p.get("name") or "") == slug:
                    return p
            except Exception:
                continue
    except Exception:
        pass
    return None

def _get_project_by_path(path: str):
    """Return project for exact path (used in helper)."""
    try:
        return db.get_project_by_path(path) if hasattr(db, "get_project_by_path") else None
    except Exception:
        return None

def _split_dav_path(path: str):
    """Split request path after /dav/ into (slug, rel).
    Examples:
      ''                          -> (None, None)
      'my-project'                -> ('my-project', '')
      'my-project/Working Docs/a' -> ('my-project', 'Working Docs/a')
    Returns (slug, rel) where rel may be '' for project root.
    Handles Windows UNC DavWWWRoot prefix (\\host@SSL\DavWWWRoot\...) which
    some clients send as /DavWWWRoot/<slug>/...
    """
    if not path:
        return None, None
    # Strip leading/trailing slashes but keep internal ones
    path = path.strip("/")
    if not path:
        return None, None
    # Windows WebDAV UNC prefix
    if path.lower().startswith("davwwwroot/"):
        path = path[len("davwwwroot/"):].lstrip("/")
        if not path:
            return None, None
    elif path.lower() == "davwwwroot":
        return None, None
    parts = path.split("/", 1)
    slug = parts[0]
    rel = parts[1] if len(parts) > 1 else ""
    # Decode percent-encoded rel for filesystem use, but keep slug as-is (slug never has %20)
    if rel:
        rel = urllib.parse.unquote(rel)
    return slug, rel


def _eq(a: str, b: str) -> bool:
    """Constant-time string comparison (avoids token timing leaks)."""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _check_dav_token(auth_header: str) -> bool:
    """Validate Authorization against AINGEL_DAV_TOKEN (Basic password or Bearer)."""
    tok = (os.getenv("AINGEL_DAV_TOKEN") or "").strip()
    if not tok:
        return False
    a = (auth_header or "").strip()
    if not a:
        return False
    low = a.lower()
    if low.startswith("bearer "):
        return _eq(a[7:].strip(), tok)
    if low.startswith("basic "):
        try:
            decoded = base64.b64decode(a[6:].strip()).decode("utf-8", errors="ignore")
            # Accept "user:token" or bare token
            if ":" in decoded:
                _, pwd = decoded.split(":", 1)
                if _eq(pwd, tok):
                    return True
            return _eq(decoded.strip(), tok)
        except Exception:
            return False
    # Plain token without scheme (some clients)
    return _eq(a, tok)


def _trust_forward_auth_headers() -> bool:
    """True only when a trusted proxy is known to inject + sanitize identity headers.

    Default False: with the Authentik outpost removed from the request path,
    any client can forge X-Authentik-*/X-Forwarded-* and would otherwise get
    full WebDAV access. Set AINGEL_DAV_TRUST_HEADERS=1 only when such a proxy
    (which also strips inbound copies) is back in front of the app.
    """
    return (os.getenv("AINGEL_DAV_TRUST_HEADERS") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _check_auth(request) -> bool:
    """True only with a valid DAV token (or trusted forward-auth headers).

    No loopback bypass: requests from agent subprocesses on this host arrive
    from 127.0.0.1 too, so source address proves nothing. Forward-auth identity
    headers are honoured only when explicitly trusted (see
    ``_trust_forward_auth_headers``) — they are spoofable otherwise.
    """
    if _trust_forward_auth_headers():
        for h in AUTH_HEADERS:
            v = request.headers.get(h, "")
            if v and v.strip():
                return True
    auth = request.headers.get("Authorization", "")
    if not (auth and auth.strip()):
        return False
    # Fail closed: without a configured token nothing authenticates (the old
    # "any Authorization header passes" legacy mode is gone).
    if not (os.getenv("AINGEL_DAV_TOKEN") or "").strip():
        return False
    return _check_dav_token(auth)

def _forbidden_for_read(name: str) -> bool:
    """Reuse agent_files forbidden + agent_filecat HIDDEN_DIRS for reads.
    Block .db/.env/.git-hidden even on GET/PROPFIND so we never leak secrets.
    """
    # Check exact forbidden from agent_files
    try:
        if agent_files._is_forbidden_path(name):
            return True
    except Exception:
        pass
    # Also block HIDDEN_DIRS as directory names (for listing filter)
    try:
        import agent_filecat as fcat
        if name in fcat.HIDDEN_DIRS:
            return True
    except Exception:
        pass
    return False

def _safe_join_for_read(project_path: str, rel: str):
    """Resolve rel inside project_path for read operations (no writable check).
    Returns absolute path or raises ValueError if escapes or forbidden.
    """
    if not rel:
        # project root
        return os.path.realpath(project_path)
    rel = rel.strip().replace(os.sep, "/")
    if rel.startswith("/") or rel.startswith("\\"):
        raise ValueError("absolute path not allowed")
    parts = rel.split("/")
    if ".." in parts:
        raise ValueError("path traversal not allowed")
    for part in parts:
        if part and _forbidden_for_read(part):
            # Allow reading Working Documents etc — those aren't forbidden
            # Only block truly sensitive leaves
            low = part.lower()
            if low in (".env", ".env.example", "aingel.db") or low.endswith(".db") or low.startswith(".env") or low.startswith("."):
                # But don't block legitimate folder names that happen to start with dot
                # Only block if the leaf is the forbidden one
                if part.lower().endswith((".db", ".db-journal", ".db-wal", ".db-shm", ".bak")) or part.startswith("."):
                    raise ValueError(f"forbidden path component: {part}")
    # Reject symlink components
    lex_parts = rel.split("/")
    for i in range(1, len(lex_parts) + 1):
        prefix = "/".join(lex_parts[:i])
        lex_abs = os.path.join(project_path, prefix)
        if os.path.lexists(lex_abs) and os.path.islink(lex_abs):
            raise ValueError(f"symlink not allowed: {prefix}")
    lex_abs = os.path.join(project_path, rel)
    candidate_real = os.path.realpath(lex_abs)
    root_real = os.path.realpath(project_path)
    if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
        raise ValueError("path escapes project")
    # Block resolved forbidden basename even on read
    base = os.path.basename(candidate_real)
    if base and _forbidden_for_read(base):
        low = base.lower()
        if low in (".env", ".env.example", "aingel.db") or low.endswith((".db", ".db-journal", ".db-wal", ".db-shm", ".bak")) or base.startswith("."):
            # Allow .gitkeep etc? No — block hidden
            raise ValueError(f"forbidden resolved path: {base}")
    return lex_abs

def _parse_client_ts(raw):
    """Parse an ownCloud-style X-OC-Mtime value or DAV:getlastmodified text.

    Accepts integer epoch seconds, HTTP-date, or ISO-8601. Returns int epoch
    or None. Range-checked (2000-01-01 .. now+2d) so garbage can never
    teleport file dates. Pure function — no I/O.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    ts = None
    try:
        ts = int(float(s))
    except (TypeError, ValueError):
        pass
    if ts is None:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
        except Exception:
            pass
    if ts is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = int(dt.timestamp())
        except Exception:
            return None
    import time as _time
    now = int(_time.time())
    if ts < 946684800 or ts > now + 2 * 86400:  # 2000-01-01 .. now+2d
        return None
    return ts


def _apply_client_modtime(abs_path, raw):
    """Best-effort: set a file's mtime from a client-supplied timestamp.

    Used for X-OC-Mtime on PUT and getlastmodified in PROPPATCH so sync
    clients (rclone bisync: size+modtime compare) see faithful timestamps
    instead of upload-time. Never raises — a failed utime must not fail
    an otherwise successful upload. Caller guarantees writability checks.
    Returns the applied epoch or None.
    """
    try:
        ts = _parse_client_ts(raw)
        if ts is None or not os.path.isfile(abs_path):
            return None
        os.utime(abs_path, (ts, ts))
        return ts
    except Exception:
        return None


def _is_dav_alias_host(host: str) -> bool:
    """True when Host is the dedicated WebDAV origin, which serves /<slug>/
    without the /dav prefix. AINGEL_DAV_HOST names it explicitly; otherwise
    any host whose first label is "dav" (e.g. dav.cordee.example) counts."""
    host = (host or "").lower().split(":")[0]
    configured = os.environ.get("AINGEL_DAV_HOST", "").strip().lower()
    if configured:
        return host == configured
    return host.startswith("dav.")


def _href_for(slug: str, rel: str) -> str:
    """Build WebDAV href for a resource (URL-encoded, trailing slash for collections).

    On a dedicated DAV host the alias without /dav is supported (e.g. /example-client/).
    When the current request came via that alias (path not starting with /dav and
    Host is the DAV host), return alias-style hrefs so Windows' href==request-uri
    check passes (otherwise 0x80070043).
    """
    # Detect alias request: current Flask request path doesn't start with /dav
    try:
        from flask import request as _req
        if _req is not None:
            p = getattr(_req, "path", "") or ""
            host = (_req.headers.get("Host") or "").lower() if hasattr(_req, "headers") else ""
            # Alias: request like /example-client/... (not /dav/...) and Host is dav host,
            # or more generally any request not starting with /dav but first segment is a known slug
            if p and not p.startswith("/dav"):
                # Confirm this is really a DAV alias and not a random page (/ , /health)
                # — check if first segment matches a project slug to avoid breaking other routes
                alias_slug, _ = _split_dav_path(p.strip("/"))
                if alias_slug and alias_slug.lower() == slug.lower():
                    if rel:
                        enc = "/".join(urllib.parse.quote(pp, safe="") for pp in rel.split("/"))
                        return f"/{slug}/{enc}"
                    return f"/{slug}/"
                # Also handle PROPFIND on /<slug> itself where rel=="" — still alias
                if not rel and p.strip("/").lower() == slug.lower():
                    return f"/{slug}/"
                # Fallback for the dedicated DAV host: assume alias if Host matches
                if _is_dav_alias_host(host):
                    if rel:
                        enc = "/".join(urllib.parse.quote(pp, safe="") for pp in rel.split("/"))
                        return f"/{slug}/{enc}"
                    return f"/{slug}/"
    except Exception:
        pass
    if rel:
        enc = "/".join(urllib.parse.quote(p, safe="") for p in rel.split("/"))
        return f"/dav/{slug}/{enc}"
    return f"/dav/{slug}/"

def _propfind_entry(slug: str, project_path: str, rel: str, is_dir: bool, abs_path: str, tags_map: dict):
    """Build one <D:response> element for PROPFIND."""
    _ensure_mime()
    href = _href_for(slug, rel)
    # Collections must have trailing slash for many clients (macOS Finder, davfs2)
    if is_dir and not href.endswith("/"):
        href += "/"
    try:
        st = os.stat(abs_path) if os.path.exists(abs_path) else None
    except Exception:
        st = None
    # Dates in RFC1123 + ISO8601
    mtime_iso = ""
    mtime_http = ""
    if st:
        dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        mtime_iso = dt.isoformat().replace("+00:00", "Z")
        mtime_http = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    ctime_iso = mtime_iso
    if st:
        try:
            cdt = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
            ctime_iso = cdt.isoformat().replace("+00:00", "Z")
        except Exception:
            ctime_iso = mtime_iso
    size = st.st_size if st and not is_dir else 0
    # Content type
    ctype = ""
    if not is_dir and abs_path:
        ctype = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    # ETag
    etag = ""
    if st:
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
    # Tags deadprops
    tags_entry = tags_map.get(rel) if rel else None
    # Also handle tags for project root? tags_map keys are rel paths like "Working Documents/file.txt"
    tags_list = []
    note = ""
    if tags_entry:
        tags_list = tags_entry.get("tags") or []
        note = tags_entry.get("note") or ""
    tags_str = ", ".join(tags_list) if tags_list else ""
    displayname = os.path.basename(rel.rstrip("/")) if rel else slug
    if not displayname:
        displayname = slug
    # Escape XML
    def _xe(s):
        return _he(s or "", quote=True)
    resourcetype = "<D:collection/>" if is_dir else ""
    getcontentlength = f"<D:getcontentlength>{size}</D:getcontentlength>" if not is_dir else ""
    getcontenttype = f"<D:getcontenttype>{_xe(ctype)}</D:getcontenttype>" if ctype and not is_dir else ""
    getetag = f"<D:getetag>{_xe(etag)}</D:getetag>" if etag else ""
    # Build prop XML
    prop_xml = f"""<D:prop>
      <D:displayname>{_xe(displayname)}</D:displayname>
      <D:creationdate>{_xe(ctime_iso)}</D:creationdate>
      <D:getlastmodified>{_xe(mtime_http)}</D:getlastmodified>
      {getcontentlength}
      {getcontenttype}
      {getetag}
      <D:resourcetype>{resourcetype}</D:resourcetype>
      <D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>
      <aingel:tags>{_xe(tags_str)}</aingel:tags>
      <aingel:note>{_xe(note)}</aingel:note>
    </D:prop>"""
    # Also expose aingel:tags as individual elements for clients that parse them
    return f"""<D:response>
  <D:href>{_xe(href)}</D:href>
  <D:propstat>
    <D:status>HTTP/1.1 200 OK</D:status>
    {prop_xml}
  </D:propstat>
</D:response>"""

def _build_multistatus(responses):
    body = "\n".join(responses)
    return f"""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:aingel="{AINGEL_NS}">
{body}
</D:multistatus>"""

def _parse_destination(request, src_slug: str):
    """Parse Destination header into (dst_slug, dst_rel) or (None, error_response)."""
    dest = request.headers.get("Destination", "")
    if not dest:
        return None, (400, "Destination header required")
    # Destination may be absolute URL or absolute path
    try:
        parsed = urllib.parse.urlparse(dest)
        path = parsed.path if parsed.scheme else dest
    except Exception:
        path = dest
    # Path should be /dav/<slug>/<rel> or /dav/<slug>
    # Strip query/fragment
    path = path.split("?")[0].split("#")[0]
    # Remove leading /dav prefix
    path = path.strip("/")
    if path.startswith("dav/"):
        path = path[3:]
    elif path == "dav":
        path = ""
    # Now path is "<slug>" or "<slug>/rel"
    if not path:
        return None, (400, "Destination must be under /dav/<slug>/")
    dst_slug, dst_rel = _split_dav_path(path)
    if not dst_slug:
        return None, (400, "Destination missing slug")
    dst_slug = dst_slug.strip().lower()
    if dst_rel:
        dst_rel = urllib.parse.unquote(dst_rel).replace(os.sep, "/").strip("/")
    else:
        dst_rel = ""
    return (dst_slug, dst_rel), None

def register_webdav(app):
    """Register WebDAV routes on the given Flask app."""
    from flask import request as flask_request, Response, send_file, jsonify

    def _dav_auth_required():
        if not _check_auth(flask_request):
            # Return 401 with WWW-Authenticate so clients prompt
            return Response("Authentication required (Authentik forward-auth).", status=401,
                            headers={"WWW-Authenticate": 'Basic realm="Cordée Vault"'})
        return None

    def _handle_options(slug, rel):
        # Always allow OPTIONS even without project (for discovery)
        headers = {
            "DAV": "1, 2",
            "Allow": ", ".join(DAV_METHODS + ["OPTIONS"]),
            "MS-Author-Via": "DAV",
        }
        return Response("", status=200, headers=headers)

    def _handle_propfind(slug, rel):
        auth = _dav_auth_required()
        if auth:
            return auth
        project = _get_project_by_slug(slug)
        if not project:
            return Response("Project not found", status=404)
        project_path = project.get("path") or ""
        if not project_path or not os.path.isdir(project_path):
            return Response("Project path not found", status=404)
        # Determine abs_path for the requested resource
        # rel == "" means project root
        if rel:
            try:
                abs_path = _safe_join_for_read(project_path, rel)
            except ValueError as ve:
                return Response(str(ve), status=404)
        else:
            abs_path = os.path.realpath(project_path)
        # Depth handling: default infinity, cap to 1
        depth = (flask_request.headers.get("Depth") or "infinity").strip().lower()
        if depth == "infinity":
            depth = "1"
        if depth not in ("0", "1"):
            depth = "1"
        # Check that requested resource exists (except for root which always exists)
        if rel and not os.path.exists(abs_path):
            return Response("Not found", status=404)
        is_dir = os.path.isdir(abs_path) if os.path.exists(abs_path) else False
        # Load tags map
        try:
            tags_map = db.get_file_tags(project_path) or {}
        except Exception:
            tags_map = {}
        responses = []
        # Self
        responses.append(_propfind_entry(slug, project_path, rel, is_dir, abs_path, tags_map))
        # Children if Depth 1 and self is directory
        if depth == "1" and is_dir:
            try:
                entries = os.listdir(abs_path)
            except PermissionError:
                entries = []
            # Filter forbidden and sort dirs first then files
            visible = []
            for name in entries:
                # Hide broken symlink targets outside project
                full = os.path.join(abs_path, name)
                try:
                    real = os.path.realpath(full)
                    root_real = os.path.realpath(project_path)
                    if not (real == root_real or real.startswith(root_real + os.sep)):
                        continue
                except Exception:
                    continue
                # Hide .db/.env/.git etc
                if _forbidden_for_read(name):
                    # But allow visible files like READMEFIRST.md; _forbidden_for_read is strict
                    # For listing, hide truly forbidden names
                    low = name.lower()
                    if low in (".env", ".env.example", "aingel.db") or name.startswith(".") or low.endswith((".db", ".db-journal", ".db-wal", ".db-shm", ".bak")):
                        continue
                    if name in (".git", ".venv", "venv", "__pycache__", ".claude", ".vibe", "node_modules"):
                        continue
                visible.append(name)
            visible.sort(key=lambda n: (not os.path.isdir(os.path.join(abs_path, n)), n.lower()))
            for name in visible:
                child_rel = f"{rel}/{name}" if rel else name
                child_abs = os.path.join(abs_path, name)
                child_is_dir = os.path.isdir(child_abs)
                # Skip forbidden child resolved basename
                try:
                    base = os.path.basename(os.path.realpath(child_abs))
                    if base and _forbidden_for_read(base):
                        low2 = base.lower()
                        if low2.endswith((".db", ".db-journal", ".db-wal", ".db-shm", ".bak")) or base.startswith("."):
                            continue
                except Exception:
                    pass
                responses.append(_propfind_entry(slug, project_path, child_rel, child_is_dir, child_abs, tags_map))
        body = _build_multistatus(responses)
        return Response(body, status=207, mimetype="application/xml; charset=utf-8",
                        headers={"DAV": "1, 2", "MS-Author-Via": "DAV"})

    def _handle_get_head(slug, rel, is_head=False):
        auth = _dav_auth_required()
        if auth:
            return auth
        project = _get_project_by_slug(slug)
        if not project:
            return Response("Project not found", status=404)
        project_path = project.get("path") or ""
        if not project_path or not os.path.isdir(project_path):
            return Response("Project path not found", status=404)
        try:
            abs_path = _safe_join_for_read(project_path, rel) if rel else os.path.realpath(project_path)
        except ValueError as ve:
            return Response(str(ve), status=404)
        if not os.path.exists(abs_path):
            return Response("Not found", status=404)
        if os.path.isdir(abs_path):
            # Return HTML directory listing for browsers (GET on a collection).
            # WebDAV clients use PROPFIND; browsers do GET. Listing is same as PROPFIND but HTML.
            try:
                entries = os.listdir(abs_path)
            except Exception:
                entries = []
            # Filter like PROPFIND does, but keep it simple for HTML view
            visible = []
            for name in sorted(entries, key=lambda n: (not os.path.isdir(os.path.join(abs_path, n)), n.lower())):
                full = os.path.join(abs_path, name)
                try:
                    real = os.path.realpath(full)
                    root_real = os.path.realpath(project_path)
                    if not (real == root_real or real.startswith(root_real + os.sep)):
                        continue
                except Exception:
                    continue
                if _forbidden_for_read(name):
                    low = name.lower()
                    if low in (".env", ".env.example", "aingel.db") or name.startswith(".") or low.endswith((".db", ".db-journal", ".db-wal", ".db-shm", ".bak")):
                        continue
                    if name in (".git", ".venv", "venv", "__pycache__", ".claude", ".vibe", "node_modules"):
                        continue
                visible.append(name)
            # Build href base for links (ensure trailing slash)
            base_href = _href_for(slug, rel)
            if not base_href.endswith("/"):
                base_href += "/"
            items = ""
            for name in visible:
                child_rel = f"{rel}/{name}" if rel else name
                href = _href_for(slug, child_rel)
                # Add trailing slash for dirs so browser can PROPFIND them too
                child_abs = os.path.join(abs_path, name)
                if os.path.isdir(child_abs) and not href.endswith("/"):
                    href += "/"
                # Use same encoding as PROPFIND hrefs (already encoded via _href_for)
                display = _he(name, quote=True)
                is_dir = os.path.isdir(child_abs)
                icon = "&#128193; " if is_dir else "&#128196; "
                items += f'<li>{icon}<a href="{_he(href, quote=True)}">{display}</a></li>\n'
            # Parent link
            parent_link = ""
            if rel:
                parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if parent_rel:
                    parent_href = _href_for(slug, parent_rel)
                    if not parent_href.endswith("/"):
                        parent_href += "/"
                else:
                    parent_href = _href_for(slug, "")
                parent_link = f'<p><a href="{_he(parent_href, quote=True)}">&uarr; Up to parent</a> &middot; <a href="/dav/">All projects</a></p>'
            display_rel = _he(rel or slug, quote=True)
            html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{display_rel} — Cordée Vault</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem}} a{{color:#2563eb}} li{{margin:0.25rem 0}}</style></head>
<body><h1>{display_rel}</h1>{parent_link}<ul>{items or "<li><em>Empty folder</em></li>"}</ul>
<p style="color:#666;font-size:0.85em">WebDAV at <code>{_he(base_href, quote=True)}</code> — mount in OS file manager. Use PROPFIND for WebDAV clients.</p></body></html>"""
            return Response(html, mimetype="text/html")

        # Serve file
        _ensure_mime()
        ctype = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        if is_head:
            try:
                st = os.stat(abs_path)
                headers = {
                    "Content-Length": str(st.st_size),
                    "Content-Type": ctype,
                    "Last-Modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"),
                    "ETag": f'"{int(st.st_mtime)}-{st.st_size}"',
                    "DAV": "1, 2",
                }
                resp = Response("", status=200, headers=headers)
                # Prevent Werkzeug from overwriting Content-Length with 0 (body length)
                try:
                    resp.automatically_set_content_length = False
                except Exception:
                    pass
                resp.headers["Content-Length"] = str(st.st_size)
                return resp
            except Exception:
                return Response("Not found", status=404)
        try:
            return send_file(abs_path, mimetype=ctype, as_attachment=False, download_name=os.path.basename(abs_path))
        except Exception as e:
            return Response(str(e), status=500)

    def _handle_put(slug, rel):
        auth = _dav_auth_required()
        if auth:
            return auth
        project = _get_project_by_slug(slug)
        if not project:
            return Response("Project not found", status=404)
        project_path = project.get("path") or ""
        if not project_path or not os.path.isdir(project_path):
            return Response("Project path not found", status=404)
        if not rel:
            return Response("Cannot PUT a collection", status=405)
        # Validate writable
        if not agent_files._is_writable_rel(rel):
            return Response("Path must be under Working Documents and not forbidden", status=403)
        try:
            abs_path = agent_files._safe_resolve(project_path, rel)
        except ValueError as ve:
            return Response(str(ve), status=403)
        # Tombstone guard: refuse to PUT a file whose content matches a soft-
        # delete tombstone (a deleted file staying deleted). Read the body once
        # so we can hash it; sha256 match at any path blocks rename-reuploads.
        # A3: skip bodies under _TOMBSTONE_MIN_BYTES (tiny/empty files never get
        # tombstoned and must never trip the guard).
        data = None
        tmp_path = None
        try:
            data = flask_request.get_data()
            if not data and flask_request.content_length and flask_request.content_length > 0:
                data = flask_request.stream.read()
            if data is not None and len(data) >= agent_files._TOMBSTONE_MIN_BYTES:
                import hashlib as _hl
                _sha = _hl.sha256(data).hexdigest()
                if _tombstone_has_sha(project_path, _sha):
                    return Response(
                        "409 Conflict: this file was deleted and is being kept deleted "
                        "(tombstone). To bring it back deliberately, use the project "
                        "Files UI (restore) or POST /api/projects/<pid>/files/restore.",
                        status=409)
        except Exception as _tg:
            _log_error("PUT tombstone check failed for %s: %s", rel, _tg)
        # Ensure parent dirs
        existed = os.path.exists(abs_path)
        try:
            parent = os.path.dirname(abs_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            if not data and flask_request.content_length and flask_request.content_length > 0:
                data = flask_request.stream.read()
            # Atomic write to a hidden temp file beside the target, then
            # os.replace, so a failed PUT never truncates the existing file and a
            # leftover temp is a dotfile (ignored by listings/git). C5.
            import tempfile as _tf
            fd, tmp_path = _tf.mkstemp(dir=parent, prefix='.aingel-put-')
            # mkstemp creates 0600; keep the usual 0644 of uploaded files.
            os.fchmod(fd, 0o644)
            # A failed write must propagate: the outer handler unlinks the temp
            # file and returns 500, leaving the existing target untouched.
            with os.fdopen(fd, 'wb') as f:
                f.write(data or b"")
            os.replace(tmp_path, abs_path)
            tmp_path = None  # moved; nothing to clean up
        except Exception as e:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            _log_error("PUT failed for %s: %s", rel, e)
            return Response(f"Failed to write: {e}", status=500)
        # Honor ownCloud-style client modtime (rclone X-OC-Mtime) so synced
        # files keep their original timestamps. Best-effort: never fails PUT.
        try:
            oc_ts = _parse_client_ts(flask_request.headers.get("X-OC-Mtime"))
            if oc_ts is not None:
                os.utime(abs_path, (oc_ts, oc_ts))
        except Exception:
            pass
        # Mirror to SCW bucket best-effort (same helper as agent_api)
        try:
            bucket = project.get("scw_session_bucket") if project.get("scw_session_enabled") else None
            region = project.get("scw_session_region") or "fr-par"
            if bucket:
                # Reuse _mirror logic: strip writable prefix for bucket key
                norm_key = rel
                is_prefixed = any(norm_key == v or norm_key.startswith(v + "/") for v in agent_files._WRITABLE_VARIANTS)
                bucket_key = norm_key
                if is_prefixed:
                    for pref in agent_files._WRITABLE_VARIANTS:
                        if norm_key == pref:
                            bucket_key = ""
                            break
                        if norm_key.startswith(pref + "/"):
                            bucket_key = norm_key[len(pref) + 1:]
                            break
                else:
                    bucket_key = norm_key
                if bucket_key:
                    try:
                        import agent_scw_session
                        agent_scw_session.upload_file(project_path=abs_path, bucket_name=bucket, key=bucket_key, region=region)
                    except Exception:
                        pass
        except Exception:
            pass
        # Emit files_changed for SSE
        try:
            import agent_events
            agent_events.emit(project["id"], {"type": "files_changed", "rel": rel})
        except Exception:
            pass
        status = 204 if existed else 201
        return Response("", status=status, headers={"ETag": f'"{int(os.path.getmtime(abs_path))}-{os.path.getsize(abs_path)}"' if os.path.exists(abs_path) else ""})

    def _handle_mkcol(slug, rel):
        auth = _dav_auth_required()
        if auth:
            return auth
        project = _get_project_by_slug(slug)
        if not project:
            return Response("Project not found", status=404)
        project_path = project.get("path") or ""
        if not rel:
            return Response("Collection already exists", status=405)
        # Body must be empty per RFC4918; if not empty, 415
        if flask_request.get_data():
            return Response("MKCOL body must be empty", status=415)
        try:
            abs_path = agent_files.mkdir(project_path, rel)
        except ValueError as ve:
            # Distinguish already-exists vs forbidden
            msg = str(ve)
            if "already exists" in msg.lower():
                return Response(msg, status=405)
            return Response(msg, status=403 if "forbidden" in msg.lower() or "writable" in msg.lower() else 400)
        except FileExistsError as e:
            return Response(str(e), status=405)
        except Exception as e:
            return Response(str(e), status=500)
        # Check that parent exists (MKCOL requires parent to exist)
        # agent_files.mkdir already creates parents via makedirs(exist_ok=True); WebDAV spec says
        # intermediate collections must exist, but we allow auto-create for usability.
        return Response("", status=201)

    def _handle_delete(slug, rel):
        auth = _dav_auth_required()
        if auth:
            return auth
        project = _get_project_by_slug(slug)
        if not project:
            return Response("Project not found", status=404)
        project_path = project.get("path") or ""
        if not rel:
            return Response("Cannot delete project root", status=403)
        try:
            trashed = agent_files.delete(project_path, rel)
        except ValueError as ve:
            return Response(str(ve), status=403 if "writable" in str(ve).lower() or "forbidden" in str(ve).lower() else 404)
        except Exception as e:
            return Response(str(e), status=500)
        try:
            import agent_events
            agent_events.emit(project["id"], {"type": "files_changed", "rel": rel})
        except Exception:
            pass
        return Response("", status=204)

    def _handle_move_copy(slug, rel, is_move: bool):
        auth = _dav_auth_required()
        if auth:
            return auth
        project = _get_project_by_slug(slug)
        if not project:
            return Response("Project not found", status=404)
        project_path = project.get("path") or ""
        if not rel:
            return Response("Cannot move/copy project root", status=403)
        parsed, err = _parse_destination(flask_request, slug)
        if err:
            code, msg = err
            return Response(msg, status=code)
        dst_slug, dst_rel = parsed
        if dst_slug != slug:
            return Response("Cross-project MOVE/COPY not allowed", status=502)
        if not dst_rel:
            return Response("Destination cannot be project root", status=403)
        overwrite = (flask_request.headers.get("Overwrite") or "T").strip().upper()
        # Check if destination exists
        try:
            dst_abs_check = _safe_join_for_read(project_path, dst_rel) if dst_rel else os.path.realpath(project_path)
            dst_exists = os.path.exists(dst_abs_check)
        except ValueError:
            dst_exists = False
        if dst_exists and overwrite == "F":
            return Response("Destination already exists (Overwrite: F)", status=412)
        # If overwrite T and dst exists, remove dst first (for files) or fail for collections
        if dst_exists and overwrite == "T":
            try:
                # Soft-delete existing destination via agent_files.delete if under writable.
                # record_tombstone=False / remove_bucket=False: an overwrite is a
                # version REPLACEMENT, not a user deletion — it must not tombstone
                # the old bytes or delete the mirrored bucket object (the new
                # content is about to be written to the same key). A1.
                if agent_files._is_writable_rel(dst_rel):
                    try:
                        agent_files.delete(project_path, dst_rel,
                                           record_tombstone=False, remove_bucket=False)
                    except Exception:
                        # Fallback: hard remove if not soft-deletable (shouldn't happen)
                        import shutil
                        if os.path.isdir(dst_abs_check):
                            shutil.rmtree(dst_abs_check, ignore_errors=True)
                        elif os.path.isfile(dst_abs_check):
                            os.remove(dst_abs_check)
                else:
                    return Response("Destination not writable", status=403)
            except Exception as e:
                return Response(f"Failed to overwrite destination: {e}", status=500)
        try:
            if is_move:
                agent_files.move(project_path, rel, dst_rel)
            else:
                agent_files.copy(project_path, rel, dst_rel)
        except ValueError as ve:
            msg = str(ve)
            code = 403 if "writable" in msg.lower() or "forbidden" in msg.lower() or "symlink" in msg.lower() else 404 if "not found" in msg.lower() else 409 if "already exists" in msg.lower() else 400
            return Response(msg, status=code)
        except Exception as e:
            return Response(str(e), status=500)
        try:
            import agent_events
            agent_events.emit(project["id"], {"type": "files_changed", "rel": dst_rel})
        except Exception:
            pass
        # 201 if created, 204 if overwritten (but we deleted first, so always 201)
        status = 201 if not dst_exists else 204
        # Provide Location for MOVE/COPY
        headers = {"Location": _href_for(slug, dst_rel)}
        return Response("", status=status, headers=headers)

    def _handle_proppatch(slug, rel):
        auth = _dav_auth_required()
        if auth:
            return auth
        project = _get_project_by_slug(slug)
        if not project:
            return Response("Project not found", status=404)
        project_path = project.get("path") or ""
        if not rel:
            return Response("Cannot set props on collection root", status=403)
        # Validate rel is writable for tag mutation
        if not agent_files._is_writable_rel(rel):
            return Response("Path must be under Working Documents", status=403)
        # Parse XML body
        data = flask_request.get_data(as_text=True) or ""
        tags = None
        note = None
        lm_raw = None
        if data:
            try:
                # Regex extraction (handles prefixed tags like aingel:tags without needing namespace binding)
                m_tags = re.search(r"<(?:\w+:)?tags[^>]*>(.*?)</(?:\w+:)?tags>", data, re.DOTALL | re.IGNORECASE)
                if m_tags:
                    tags = [t.strip() for t in (m_tags.group(1) or "").split(",") if t.strip()]
                m_note = re.search(r"<(?:\w+:)?note[^>]*>(.*?)</(?:\w+:)?note>", data, re.DOTALL | re.IGNORECASE)
                if m_note:
                    note = (m_note.group(1) or "").strip()
                m_lm = re.search(r"<(?:\w+:)?getlastmodified[^>]*>(.*?)</(?:\w+:)?getlastmodified>", data, re.DOTALL | re.IGNORECASE)
                if m_lm:
                    lm_raw = (m_lm.group(1) or "").strip()
                # Fallback: full XML parse with namespaces
                if tags is None and note is None and lm_raw is None:
                    try:
                        root = ET.fromstring(data)
                        for el in root.iter():
                            tag = el.tag
                            if "}" in tag:
                                tag = tag.split("}", 1)[1]
                            if tag == "tags" and tags is None:
                                tags = [t.strip() for t in (el.text or "").split(",") if t.strip()]
                            elif tag == "note" and note is None:
                                note = (el.text or "").strip()
                            elif tag == "getlastmodified" and lm_raw is None:
                                lm_raw = (el.text or "").strip()
                    except Exception:
                        pass
            except Exception:
                pass
        if tags is None and note is None and lm_raw is None:
            return Response("No aingel:tags, aingel:note or getlastmodified in PROPPATCH body", status=400)
        # Apply getlastmodified (rclone bisync modtime round-trip). Independent
        # of the tags deadprops: a pure-modtime PROPPATCH must not touch them.
        lm_applied = None
        if lm_raw is not None:
            lm_ts = _parse_client_ts(lm_raw)
            if lm_ts is None:
                return Response("Unparseable getlastmodified value (epoch, HTTP-date or ISO-8601 expected)", status=400)
            try:
                lm_abs = agent_files._safe_resolve(project_path, rel)
            except ValueError as ve:
                return Response(str(ve), status=403)
            if not os.path.isfile(lm_abs):
                return Response("No such file", status=404)
            try:
                os.utime(lm_abs, (lm_ts, lm_ts))
                lm_applied = lm_ts
            except Exception as e:
                return Response(f"Failed to set mtime: {e}", status=500)
        # Fetch existing to merge if one is None
        try:
            existing = (db.get_file_tags(project_path) or {}).get(rel) or {}
            if tags is None:
                tags = existing.get("tags") or []
            if note is None:
                note = existing.get("note") or ""
        except Exception:
            if tags is None:
                tags = []
            if note is None:
                note = ""
        try:
            ok = db.set_file_tag(project_path, rel, tags or [], note or "")
            if not ok and not (tags or (note or "").strip()):
                # Was a delete — still success
                pass
        except Exception as e:
            return Response(str(e), status=500)
        # Return 207 with propstat for the patched props
        body = f"""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:aingel="{AINGEL_NS}">
  <D:response>
    <D:href>{_he(_href_for(slug, rel), quote=True)}</D:href>
    <D:propstat>
      <D:status>HTTP/1.1 200 OK</D:status>
      <D:prop>
        <aingel:tags>{_he(", ".join(tags or []), quote=True)}</aingel:tags>
        <aingel:note>{_he(note or "", quote=True)}</aingel:note>
      </D:prop>
    </D:propstat>
  </D:response>
</D:multistatus>"""
        return Response(body, status=207, mimetype="application/xml; charset=utf-8",
                        headers={"DAV": "1, 2", "MS-Author-Via": "DAV"})

    def _handle_lock_unlock(slug, rel):
        auth = _dav_auth_required()
        if auth:
            return auth
        # Minimal LOCK support: return a dummy lock token so clients proceed.
        # We don't enforce real locking (OS mounts work without it for basic ops).
        if flask_request.method == "LOCK":
            body = f"""<?xml version="1.0" encoding="utf-8" ?>
<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>
  <D:locktype><D:write/></D:locktype>
  <D:lockscope><D:exclusive/></D:lockscope>
  <D:depth>infinity</D:depth>
  <D:owner>Cordée Vault</D:owner>
  <D:timeout>Second-3600</D:timeout>
  <D:locktoken><D:href>opaquelocktoken:aingel-{slug}-vault</D:href></D:locktoken>
</D:activelock></D:lockdiscovery></D:prop>"""
            return Response(body, status=200, mimetype="application/xml; charset=utf-8",
                            headers={"Lock-Token": "<opaquelocktoken:aingel-vault>"})
        else:  # UNLOCK
            return Response("", status=204)

    @app.route("/dav", defaults={"path": ""}, methods=DAV_METHODS + ["OPTIONS"])
    @app.route("/dav/", defaults={"path": ""}, methods=DAV_METHODS + ["OPTIONS"])
    @app.route("/dav/<path:path>", methods=DAV_METHODS + ["OPTIONS"])
    def dav_handler(path):
        # Normalize path for routing
        method = flask_request.method
        slug, rel = _split_dav_path(path)
        # Root listing (/dav or /dav/) — PROPFIND lists all projects
        if slug is None:
            if method == "OPTIONS":
                return _handle_options("", "")
            if method in ("PROPFIND", "PROPPATCH"):
                auth = _dav_auth_required()
                if auth:
                    return auth
                depth = (flask_request.headers.get("Depth") or "0").strip().lower()
                if depth == "infinity":
                    depth = "1"
                try:
                    tags_map = {}
                except Exception:
                    tags_map = {}
                # Multistatus with self + each project as collection
                responses = []
                # Self
                responses.append(f"""<D:response><D:href>/dav/</D:href><D:propstat><D:status>HTTP/1.1 200 OK</D:status><D:prop><D:displayname>dav</D:displayname><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>""")
                if depth == "1":
                    try:
                        for p in db.get_projects():
                            s = p.get("slug") or _slugify(p.get("name") or "")
                            if not s:
                                continue
                            # Skip forbidden slug components
                            if _forbidden_for_read(s):
                                continue
                            # Build collection entry for project
                            href = f"/dav/{s}/"
                            responses.append(f"""<D:response><D:href>{_he(href, quote=True)}</D:href><D:propstat><D:status>HTTP/1.1 200 OK</D:status><D:prop><D:displayname>{_he(p.get('name') or s, quote=True)}</D:displayname><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>""")
                    except Exception:
                        pass
                body = _build_multistatus(responses)
                return Response(body, status=207, mimetype="application/xml; charset=utf-8",
                                headers={"DAV": "1, 2", "MS-Author-Via": "DAV"})
            if method in ("GET", "HEAD"):
                auth = _dav_auth_required()
                if auth:
                    return auth
                # HTML listing for browsers hitting /dav/
                try:
                    projects = db.get_projects()
                except Exception:
                    projects = []
                items = "\n".join(f'<li><a href="/dav/{_he(p.get("slug") or "", quote=True)}/">{_he(p.get("name") or p.get("slug") or "", quote=True)}</a></li>' for p in projects)
                html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Cordée Vault — WebDAV</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem}} a{{color:#2563eb}}</style></head>
<body><h1>Cordée Vault — WebDAV</h1><p>Mount <code>/dav/&lt;project-slug&gt;/</code> via WebDAV. Use PROPFIND to list.</p><ul>{items}</ul></body></html>"""
                return Response(html, mimetype="text/html")
            return Response("Method not allowed", status=405, headers={"Allow": ", ".join(DAV_METHODS)})

        # From here slug is set. Validate slug exists for most methods (except OPTIONS which can probe)
        if method == "OPTIONS":
            return _handle_options(slug, rel or "")
        if method == "PROPFIND":
            return _handle_propfind(slug, rel or "")
        if method == "PROPPATCH":
            return _handle_proppatch(slug, rel or "")
        if method in ("GET", "HEAD"):
            return _handle_get_head(slug, rel or "", is_head=(method == "HEAD"))
        if method == "PUT":
            return _handle_put(slug, rel or "")
        if method == "MKCOL":
            return _handle_mkcol(slug, rel or "")
        if method == "DELETE":
            return _handle_delete(slug, rel or "")
        if method == "MOVE":
            return _handle_move_copy(slug, rel or "", is_move=True)
        if method == "COPY":
            return _handle_move_copy(slug, rel or "", is_move=False)
        if method in ("LOCK", "UNLOCK"):
            return _handle_lock_unlock(slug, rel or "")
        return Response("Method not allowed", status=405, headers={"Allow": ", ".join(DAV_METHODS)})

    # Also handle case-insensitive /DAV (some clients uppercase)
    @app.before_request
    def _dav_normalize_case():
        # Flask already handles case-sensitive routing; no-op
        pass

    return app


# ── Standalone runner for 127.0.0.1:8002 mode ─────────────────────────────────

def create_standalone_app():
    """Create a minimal Flask app exposing only /dav for 8002 standalone mode.

    On a dedicated DAV host the tree is also reachable without the /dav prefix
    (e.g. https://dav.cordee.example/example-client/) for cleaner mounts. Both
    forms are accepted; hrefs still use /dav/<slug>/.
    """
    from flask import Flask, jsonify, Response as FlaskResponse, request as s_request, Response, abort

    s_app = Flask(__name__)
    # Cap uploads on the standalone webdav app too (mirrors the main app's
    # MAX_CONTENT_LENGTH, defined once in agent_config — C4).
    s_app.config["MAX_CONTENT_LENGTH"] = getattr(agent_config, 'MAX_CONTENT_LENGTH', 128 * 1024 * 1024)
    register_webdav(s_app)

    @s_app.route("/", methods=["GET", "OPTIONS", "PROPFIND", "HEAD"])
    def _idx():
        # Windows probes PROPFIND on / during map drive — must return 207, not 405
        if s_request.method == "PROPFIND":
            auth = None
            # Reuse same auth check as DAV
            if not _check_auth(s_request):
                return FlaskResponse("Authentication required (Authentik forward-auth).", status=401,
                                     headers={"WWW-Authenticate": 'Basic realm="Cordée Vault"'})
            # Return root listing with alias-style hrefs (no /dav prefix on dedicated host)
            try:
                projects = db.get_projects()
            except Exception:
                projects = []
            responses = [f"""<D:response><D:href>/</D:href><D:propstat><D:status>HTTP/1.1 200 OK</D:status><D:prop><D:displayname>Cordée WebDAV</D:displayname><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>"""]
            for p in projects:
                s = p.get("slug") or _slugify(p.get("name") or "")
                if not s or _forbidden_for_read(s):
                    continue
                # On dedicated host use alias href /<slug>/, on main host keep /dav
                host = (s_request.headers.get("Host") or "").lower()
                href = f"/{s}/" if _is_dav_alias_host(host) else f"/dav/{s}/"
                responses.append(f"""<D:response><D:href>{_he(href, quote=True)}</D:href><D:propstat><D:status>HTTP/1.1 200 OK</D:status><D:prop><D:displayname>{_he(p.get('name') or s, quote=True)}</D:displayname><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>""")
            body = _build_multistatus(responses)
            return FlaskResponse(body, status=207, mimetype="application/xml; charset=utf-8",
                                 headers={"DAV": "1, 2", "MS-Author-Via": "DAV"})
        if s_request.method == "OPTIONS":
            return FlaskResponse("", status=200, headers={"DAV": "1, 2", "Allow": ", ".join(DAV_METHODS + ["OPTIONS"]), "MS-Author-Via": "DAV"})
        return FlaskResponse("Cordée WebDAV — use /dav/<slug>/ (or /<slug>/ on a dedicated dav.* host)", mimetype="text/plain")

    @s_app.route("/health")
    def _health():
        return jsonify({"ok": True, "service": "aingel-webdav"})

    # Alias: /<slug>/... without /dav prefix (only on the dedicated host, but
    # harmless to accept everywhere — main app on 8001 also benefits for
    # backwards compat). Mirrors the /dav/<slug>/ logic.
    @s_app.route("/<path:path>", methods=DAV_METHODS + ["OPTIONS"])
    def _dav_alias(path):
        # Don't shadow the real /dav and /health handlers
        if path.startswith("dav/") or path == "dav" or path.startswith("health"):
            abort(404)
        # Only treat as DAV alias if first segment looks like a project slug
        slug, rel = _split_dav_path(path)
        if not slug:
            abort(404)
        # Quick existence check — if slug doesn't look like a known project, 404
        # so we don't mask other 404s. This also avoids handling random paths.
        if not _get_project_by_slug(slug):
            abort(404)
        method = s_request.method
        # Reuse same auth/handler dispatch as register_webdav's dav_handler
        # Duplicate dispatch to avoid coupling to inner closure
        if method == "OPTIONS":
            return Response("", status=200, headers={"DAV": "1, 2", "Allow": ", ".join(DAV_METHODS + ["OPTIONS"]), "MS-Author-Via": "DAV"})
        # Inline dispatch to existing handlers by reusing register_webdav's helpers
        # We call the underlying Flask handlers via a synthetic sub-request to /dav/<path>
        # Simplest: rebuild path as /dav/<path> and let the registered view handle it
        # by forwarding internally. Use test_client-like internal dispatch:
        # Instead, directly invoke the same logic (copy of dav_handler dispatch)
        from flask import request as flask_request  # s_request is the real request
        # Use the same helper functions defined at module level
        # For PROPFIND/GET etc we can just delegate to the /dav handler by issuing
        # an internal request — but easiest is to call the view function directly:
        # Find the dav_handler view and call it with path
        for rule in s_app.url_map.iter_rules():
            if rule.rule == "/dav/<path:path>" and "GET" in rule.methods:
                # Found dav route; invoke its view with our path
                view = s_app.view_functions[rule.endpoint]
                return view(path)
        abort(404)

    return s_app

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Cordée WebDAV standalone (port 8002)")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    s_app = create_standalone_app()
    print(f"[aingel-webdav] Listening on {args.host}:{args.port} (DAV at /dav/<slug>/)")
    s_app.run(host=args.host, port=args.port, threaded=True)
