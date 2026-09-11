#!/usr/bin/env python3
"""Restricted text file browser and editor for the local workspace."""

import json
import os
import re
import subprocess
import tempfile
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


BASE_PATH = os.environ.get("FILE_PORTAL_BASE_PATH", "/shell_file").rstrip("/")
MAX_FILE_SIZE = 2 * 1024 * 1024
ALLOWED_ROOTS = tuple(
    Path(item).resolve()
    for item in os.environ.get("FILE_PORTAL_ROOTS", "/tmp:/home/ubuntu/workspace").split(":")
    if item
)
PATH_PRESETS = tuple(os.environ.get("FILE_PORTAL_PRESETS", "/tmp|/home/ubuntu/workspace|__git__").split("|"))
DOWNLOADABLE_SUFFIXES = frozenset({".patch", ".md", ".txt"})
MAX_DOWNLOAD_FILE_SIZE = 10 * 1024 * 1024


class PortalError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def is_within_roots(path):
    return any(path == root or root in path.parents for root in ALLOWED_ROOTS)


def resolve_path(value, require_exists=True):
    if not isinstance(value, str) or not value.startswith("/"):
        raise PortalError(HTTPStatus.BAD_REQUEST, "An absolute path is required.")

    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=require_exists)
    except FileNotFoundError:
        raise PortalError(HTTPStatus.NOT_FOUND, "Path does not exist.") from None
    except OSError as error:
        raise PortalError(HTTPStatus.BAD_REQUEST, f"Cannot resolve path: {error}") from None

    if not is_within_roots(resolved):
        raise PortalError(HTTPStatus.FORBIDDEN, "Path is outside the allowed workspace.")
    return resolved


def ensure_text_file(path):
    if not path.is_file():
        raise PortalError(HTTPStatus.BAD_REQUEST, "Path is not a regular file.")
    size = path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File exceeds the 2 MiB editing limit.")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PortalError(HTTPStatus.FORBIDDEN, f"Cannot read file: {error}") from None
    if b"\x00" in data[:8192]:
        raise PortalError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Binary files are not editable.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PortalError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Only UTF-8 text files are supported.") from None


def metadata(path):
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "writable": os.access(path, os.W_OK),
    }


def git_root_for(path):
    directory = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = Path(result.stdout.decode("utf-8", "replace").strip()).resolve()
    return root if is_within_roots(root) else None


def git_branch_for(root):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode == 0 and branch:
        return branch

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "detached HEAD"
    commit = result.stdout.decode("utf-8", "replace").strip()
    return f"detached HEAD ({commit})" if result.returncode == 0 and commit else "detached HEAD"


def resolve_git_root(value):
    path = resolve_path(value)
    root = git_root_for(path)
    if root is None:
        raise PortalError(HTTPStatus.BAD_REQUEST, "Path is not inside a Git worktree.")
    return root


def git_output(arguments):
    try:
        result = subprocess.run(
            arguments,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        raise PortalError(HTTPStatus.GATEWAY_TIMEOUT, "Git command timed out.") from None
    if result.returncode != 0:
        raise PortalError(HTTPStatus.BAD_REQUEST, "Git command failed.")
    if len(result.stdout) > MAX_FILE_SIZE:
        raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Commit output exceeds the 2 MiB display limit.")
    return result.stdout.decode("utf-8", "replace")


def read_download_file(path):
    if path.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
        raise PortalError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Only .patch, .md, and .txt files can be downloaded.")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PortalError(HTTPStatus.FORBIDDEN, f"Cannot read file: {error}") from None
    if len(data) > MAX_DOWNLOAD_FILE_SIZE:
        raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Each downloaded file must be 10 MB or smaller.")
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkspaceFilePortal/1.0"

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}", flush=True)

    def do_GET(self):
        request = urlparse(self.path)
        if request.path in (BASE_PATH, f"{BASE_PATH}/"):
            return self.send_html()
        if request.path == f"{BASE_PATH}/favicon.svg":
            return self.send_favicon()
        if request.path == f"{BASE_PATH}/api/list":
            return self.list_directory(request)
        if request.path == f"{BASE_PATH}/api/file":
            return self.read_file(request)
        if request.path == f"{BASE_PATH}/api/download":
            return self.download_path(request)
        if request.path == f"{BASE_PATH}/api/git/commits":
            return self.list_commits(request)
        if request.path == f"{BASE_PATH}/api/git/show":
            return self.show_commit(request)
        if request.path == f"{BASE_PATH}/api/roots":
            return self.send_json(HTTPStatus.OK, {
                "roots": [str(root) for root in ALLOWED_ROOTS],
                "presets": PATH_PRESETS,
            })
        return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        request = urlparse(self.path)
        if request.path != f"{BASE_PATH}/api/file":
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_FILE_SIZE + 65536:
                raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request is too large.")
            payload = json.loads(self.rfile.read(length))
            self.write_file(payload)
        except json.JSONDecodeError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be JSON.")
        except PortalError as error:
            self.send_error_json(error.status, error.message)

    def list_directory(self, request):
        try:
            raw_path = parse_qs(request.query).get("path", [str(ALLOWED_ROOTS[0])])[0]
            path = resolve_path(raw_path)
            if not path.is_dir():
                raise PortalError(HTTPStatus.BAD_REQUEST, "Path is not a directory.")
            items = list(path.iterdir())
            if path == Path("/tmp"):
                def modified_descending(item):
                    try:
                        return item.stat().st_mtime_ns
                    except OSError:
                        return 0

                items.sort(key=modified_descending, reverse=True)
            else:
                items.sort(key=lambda entry: (not entry.is_dir(), entry.name.lower()))

            entries = []
            for item in items:
                try:
                    resolved = item.resolve()
                    if not is_within_roots(resolved):
                        continue
                    item_stat = item.stat()
                    entries.append({
                        "name": item.name,
                        "path": str(resolved),
                        "directory": item.is_dir(),
                        "size": item_stat.st_size,
                        "mtimeNs": item_stat.st_mtime_ns,
                    })
                except OSError:
                    continue
            parent = str(path.parent) if path != next(root for root in ALLOWED_ROOTS if path == root or root in path.parents) else None
            git_root = git_root_for(path)
            git_branch = git_branch_for(git_root) if git_root else None
            self.send_json(HTTPStatus.OK, {
                "path": str(path),
                "parent": parent,
                "entries": entries,
                "gitRoot": str(git_root) if git_root else None,
                "gitBranch": git_branch,
            })
        except PortalError as error:
            self.send_error_json(error.status, error.message)

    def read_file(self, request):
        try:
            raw_path = parse_qs(request.query).get("path", [None])[0]
            path = resolve_path(raw_path)
            content = ensure_text_file(path)
            self.send_json(HTTPStatus.OK, {**metadata(path), "content": content})
        except PortalError as error:
            self.send_error_json(error.status, error.message)

    def download_path(self, request):
        try:
            raw_path = parse_qs(request.query).get("path", [None])[0]
            path = resolve_path(raw_path)
            if path.is_file():
                data = read_download_file(path)
                self.send_download(data, path.name, "text/plain; charset=utf-8")
            elif path.is_dir():
                self.download_directory(path)
            else:
                raise PortalError(HTTPStatus.BAD_REQUEST, "Path is not a regular file or directory.")
        except PortalError as error:
            self.send_error_json(error.status, error.message)

    def download_directory(self, directory):
        files = []
        for root, dirnames, filenames in os.walk(directory, topdown=True, followlinks=False):
            root_path = Path(root)
            dirnames[:] = sorted(name for name in dirnames if not (root_path / name).is_symlink())
            for filename in sorted(filenames):
                candidate = root_path / filename
                if candidate.is_symlink() or candidate.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as error:
                    raise PortalError(HTTPStatus.FORBIDDEN, f"Cannot resolve {candidate.name}: {error}") from None
                if not is_within_roots(resolved) or not (resolved == directory or directory in resolved.parents):
                    raise PortalError(HTTPStatus.FORBIDDEN, f"File is outside the selected directory: {candidate.name}")
                if not resolved.is_file():
                    continue
                files.append((candidate.relative_to(directory).as_posix(), resolved))

        if not files:
            raise PortalError(HTTPStatus.NOT_FOUND, "No .patch, .md, or .txt files found in the selected directory.")

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
                temporary_path = Path(handle.name)
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for archive_name, path in files:
                    archive.writestr(archive_name, read_download_file(path))
            data = temporary_path.read_bytes()
        except OSError as error:
            raise PortalError(HTTPStatus.FORBIDDEN, f"Cannot create directory download: {error}") from None
        finally:
            if temporary_path:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
        self.send_download(data, f"{directory.name or 'download'}.zip", "application/zip")

    def send_download(self, data, filename, content_type):
        encoded_filename = quote(filename, safe="")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_filename}")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def list_commits(self, request):
        try:
            raw_path = parse_qs(request.query).get("path", [None])[0]
            root = resolve_git_root(raw_path)
            output = git_output([
                "git", "-C", str(root), "log", "-10",
                "--date=format:%m-%d %H:%M:%S", "--format=%H%x1f%h%x1f%ad%x1f%s",
            ])
            commits = []
            for line in output.splitlines():
                full_hash, short_hash, date, subject = line.split("\x1f", 3)
                commits.append({"hash": full_hash, "shortHash": short_hash, "date": date, "subject": subject})
            self.send_json(HTTPStatus.OK, {
                "root": str(root), "branch": git_branch_for(root), "commits": commits,
            })
        except PortalError as error:
            self.send_error_json(error.status, error.message)

    def show_commit(self, request):
        try:
            query = parse_qs(request.query)
            root = resolve_git_root(query.get("path", [None])[0])
            commits = query.get("commits", [query.get("commit", [""])[0]])[0].split(",")
            if not 1 <= len(commits) <= 8 or any(
                not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit) for commit in commits
            ):
                raise PortalError(HTTPStatus.BAD_REQUEST, "Provide between one and eight commit identifiers.")
            patch = query.get("patch", ["1"])[0]
            if patch not in ("0", "1"):
                raise PortalError(HTTPStatus.BAD_REQUEST, "Patch mode must be 0 or 1.")
            command = ["git", "-C", str(root), "log", "--no-walk=unsorted", "--decorate=short"]
            if patch == "1":
                command.append("--patch")
            content = git_output([*command, *commits])
            self.send_json(HTTPStatus.OK, {
                "path": str(root), "commits": commits, "patch": patch == "1", "content": content,
            })
        except PortalError as error:
            self.send_error_json(error.status, error.message)

    def write_file(self, payload):
        path = resolve_path(payload.get("path"))
        content = payload.get("content")
        expected_mtime = payload.get("mtimeNs")
        if not isinstance(content, str):
            raise PortalError(HTTPStatus.BAD_REQUEST, "Content must be a string.")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_SIZE:
            raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Content exceeds the 2 MiB editing limit.")
        if not os.access(path, os.W_OK):
            raise PortalError(HTTPStatus.FORBIDDEN, "File is not writable by this service user.")
        current_mtime = path.stat().st_mtime_ns
        if expected_mtime != current_mtime:
            raise PortalError(HTTPStatus.CONFLICT, "File changed on disk. Reload before saving.")

        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.chmod(temporary_path, path.stat().st_mode)
            os.replace(temporary_path, path)
        except OSError as error:
            raise PortalError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unable to save file: {error}") from None
        self.send_json(HTTPStatus.OK, metadata(path))

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status, message):
        self.send_json(status, {"error": message})

    def send_html(self):
        data = INDEX_HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_favicon(self):
        data = FAVICON_SVG.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><path fill="#f4ba5b" d="M7 17a6 6 0 0 1 6-6h15l7 8h16a6 6 0 0 1 6 6v25a6 6 0 0 1-6 6H13a6 6 0 0 1-6-6z"/><path fill="#49c5a9" d="M7 27h50v23a6 6 0 0 1-6 6H13a6 6 0 0 1-6-6z"/><path fill="#172029" d="M18 35h28v4H18zm0 9h20v4H18z"/></svg>'''


INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="/shell_file/favicon.svg" type="image/svg+xml"><title>Workspace Files</title>
<style>
.content-shell{grid-template-rows:minmax(0,1fr)}.line-numbers{height:100%}
#layout.layout{grid-template-columns:minmax(300px,clamp(30%,var(--sidebar-width,30%),55%)) 6px minmax(440px,1fr)}
.code-layer{position:relative;min-width:0;min-height:0;overflow:hidden;background:#101820}.highlight{position:absolute;inset:0;margin:0;padding:16px;overflow:hidden;pointer-events:none;white-space:pre;color:#e7efe9;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;tab-size:4}.code-layer textarea{position:absolute;inset:0;background:transparent;color:transparent;caret-color:#e7efe9}.code-layer textarea:disabled{opacity:1}.code-layer textarea::selection{background:#315c66;color:transparent}.code-layer textarea::placeholder{color:#aab9b6}.tok-comment{color:#7e938f}.tok-string{color:#d9c874}.tok-keyword,.tok-preprocessor{color:#74b9ff}.tok-number{color:#e99b6d}.tok-diff-add{color:#75c995}.tok-diff-remove{color:#f48670}.tok-diff-hunk{color:#d9c874}.tok-diff-meta{color:#72bce4}.tok-git-head{color:#c89cff}.tok-log{color:#d9c874}.tok-url{color:#72bce4}.tok-alert{color:#f48670}.tok-text-head{color:#c89cff}
#entries .git-entry-info{position:relative;display:block;min-height:38px}#entries .git-entry-info strong{display:-webkit-box;overflow:hidden;white-space:normal;line-height:19px;-webkit-box-orient:vertical;-webkit-line-clamp:2}#entries .git-entry-meta{position:absolute;right:0;bottom:0;padding-left:9px;background:#1c2730;color:var(--muted);font:12px ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap}#entries .entry:hover .git-entry-meta{background:#2a3942}
:root{color-scheme:dark;--bg:#172029;--panel:#202c35;--line:#40515a;--text:#eff4f1;--muted:#aab9b6;--teal:#49c5a9;--gold:#f4ba5b;--danger:#f48670}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,sans-serif}.app{height:100vh;display:grid;grid-template-rows:auto 1fr}header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;flex-wrap:nowrap;gap:10px;align-items:center;background:#1c2730}h1{font-size:16px;margin:0 8px 0 0;font-weight:650}.folder-mark{position:relative;width:21px;height:16px;border-radius:2px;background:var(--teal);margin-left:1px}.folder-mark:before{content:"";position:absolute;width:10px;height:4px;top:-3px;left:1px;border-radius:2px 2px 0 0;background:var(--gold)}.clock{min-width:122px;color:var(--muted);font:14px ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap}.path{flex:1;min-width:0;background:#101820;border:1px solid var(--line);color:var(--text);padding:8px 10px;border-radius:4px}button,.commit-count{border:1px solid var(--line);border-radius:4px;background:#2a3942;color:var(--text);padding:8px 11px;cursor:pointer}button:hover,.commit-count:hover{border-color:var(--teal)}button:disabled{opacity:.5;cursor:not-allowed}.save{background:#167b68;border-color:#219d86}.copy{position:relative;width:31px;height:31px;padding:0}.copy:before,.copy:after{content:"";position:absolute;width:10px;height:12px;border:1px solid currentColor;border-radius:1px}.copy:before{left:10px;top:8px}.copy:after{left:7px;top:5px;background:#2a3942}.commit-count{height:31px;padding:0 7px;font:12px system-ui,sans-serif}.layout{min-height:0;display:grid;grid-template-columns:minmax(250px,var(--sidebar-width,30%)) 6px minmax(360px,1fr)}.browser{overflow:auto;background:#1c2730}.splitter{position:relative;cursor:col-resize;background:var(--line);touch-action:none}.splitter:after{content:"";position:absolute;inset:0 -3px}.splitter:hover,.splitter:focus{background:var(--teal);outline:0}body.resizing,body.resizing *{cursor:col-resize!important;user-select:none!important}.crumb{padding:12px 14px;border-bottom:1px solid var(--line);color:var(--muted);word-break:break-all}.entry{display:flex;align-items:center;gap:9px;width:100%;border:0;border-radius:0;background:transparent;text-align:left;padding:8px 14px;color:var(--text)}.entry:hover{background:#2a3942}.entry-info{display:flex;align-items:center;gap:10px;min-width:0;flex:1}.entry-info strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}.entry-meta{display:flex;gap:8px;flex:0 0 auto;color:var(--muted);font:12px ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap}.icon{position:relative;display:block;flex:0 0 18px;width:18px;height:19px}.icon.folder{height:14px;margin-top:3px;border-radius:2px;background:#d9a858}.icon.folder:before{content:"";position:absolute;top:-3px;left:1px;width:9px;height:4px;border-radius:2px 2px 0 0;background:#f2c56e}.icon.file{width:14px;height:18px;margin-left:2px;border:1px solid currentColor;border-radius:2px;color:#aab9b6;background:#24313a}.icon.file:before{content:"";position:absolute;top:-1px;right:-1px;border-top:6px solid #1c2730;border-left:6px solid transparent}.icon.file.code{color:#5bb4e6}.icon.file.config{color:#d6c15e}.icon.file.markdown{color:#67cba3}.icon.file.image{color:#d88cbe}.icon.file.archive{color:#e99662}.icon.git{width:15px;height:15px;margin:2px 0 0 2px;border-left:2px solid #ed8b5f;border-bottom:2px solid #ed8b5f;transform:rotate(-45deg)}.icon.git:before,.icon.git:after{content:"";position:absolute;width:5px;height:5px;border-radius:50%;background:#ed8b5f}.icon.git:before{left:-4px;bottom:-4px}.icon.git:after{right:-4px;top:-4px}.editor{min-width:0;display:grid;grid-template-rows:auto 1fr}.filebar{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;color:var(--muted)}.filebar span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.status{margin-left:auto;color:var(--muted);white-space:nowrap}.status.error{color:var(--danger)}.status.ok{color:var(--teal)}.content-shell{min-height:0;display:grid;grid-template-columns:auto minmax(0,1fr);background:#101820}.line-numbers{margin:0;padding:16px 8px;min-width:52px;overflow:hidden;border-right:1px solid #2a3942;background:#172029;color:#718582;text-align:right;user-select:none;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;tab-size:4}textarea{width:100%;height:100%;resize:none;border:0;outline:0;padding:16px;background:#101820;color:#e7efe9;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;tab-size:4}@media(max-width:760px){.layout{grid-template-columns:1fr;grid-template-rows:36% 64%}.splitter{display:none}.browser{border-bottom:1px solid var(--line)}header{flex-wrap:wrap}.path{order:2;flex-basis:100%}.entry-meta{gap:5px;font-size:11px}}
</style></head><body><main class="app"><header><span class="folder-mark" aria-hidden="true"></span><h1>Workspace Files</h1><select id="presets" class="path" aria-label="Saved paths"></select><input id="path" class="path" spellcheck="false" aria-label="Absolute path"><button id="open">Open</button><button id="up">Up</button><button id="save" class="save" disabled>Save</button><time id="clock" class="clock" title="Current browser time"></time></header><section id="layout" class="layout"><aside class="browser"><div id="directory" class="crumb"></div><div id="entries"></div></aside><div id="splitter" class="splitter" role="separator" aria-orientation="vertical" aria-label="Resize file list and preview"></div><section class="editor"><div class="filebar"><button id="copy" class="copy" type="button" aria-label="Copy file contents" title="Copy file contents" disabled></button><select id="commit-count" class="commit-count" aria-label="Number of commits to show" title="Number of commits to show" hidden><option value="1">1 commit</option><option value="2">2 commits</option><option value="3">3 commits</option><option value="4">4 commits</option><option value="5">5 commits</option><option value="6">6 commits</option><option value="7">7 commits</option><option value="8">8 commits</option></select><select id="commit-view" class="commit-count" aria-label="Commit display mode" title="Commit display mode" hidden><option value="1">Log + patch</option><option value="0">Log only</option></select><span id="file">Choose a text file</span><span id="status" class="status"></span></div><div class="content-shell"><pre id="line-numbers" class="line-numbers" aria-hidden="true">1</pre><div class="code-layer"><pre id="highlight" class="highlight" aria-hidden="true"></pre><textarea id="content" spellcheck="false" wrap="off" placeholder="Select a UTF-8 text file to view or edit." disabled></textarea></div></div></section></section></main>
<script>
const base='/shell_file',layout=document.querySelector('#layout'),splitter=document.querySelector('#splitter'),pathInput=document.querySelector('#path'),presets=document.querySelector('#presets'),entries=document.querySelector('#entries'),directory=document.querySelector('#directory'),content=document.querySelector('#content'),lineNumbers=document.querySelector('#line-numbers'),file=document.querySelector('#file'),status=document.querySelector('#status'),save=document.querySelector('#save'),copy=document.querySelector('#copy'),downloadButton=document.createElement('button'),clock=document.querySelector('#clock'),commitCount=document.querySelector('#commit-count'),commitView=document.querySelector('#commit-view'),lastDirectoryKey='workspaceFilePortal.lastDirectory',lastGitRootKey='workspaceFilePortal.lastGitRoot',lastViewModeKey='workspaceFilePortal.lastViewMode',commitCountKey='workspaceFilePortal.commitCount',commitViewKey='workspaceFilePortal.commitView',sidebarWidthKey='workspaceFilePortal.sidebarWidth';downloadButton.id='download';downloadButton.type='button';downloadButton.textContent='Download';downloadButton.disabled=true;downloadButton.title='Download selected patch, Markdown file, or directory';document.querySelector('.filebar').insertBefore(downloadButton,commitCount);let current=null,currentDirectory=null,viewMode='files',gitRoot=null,gitCommits=[],gitStartIndex=0;
function setStatus(message='',kind=''){status.textContent=message;status.className='status '+kind}async function request(url,options){const response=await fetch(url,options);const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data}function fmt(size){return size<1024?size+' B':(size/1024).toFixed(1)+' KiB'}function dateTime(date){return String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0')+' '+date.toLocaleTimeString('en-GB',{hour12:false})}function formatTime(value){return dateTime(new Date(Number(value)/1000000))}function updateClock(){const now=new Date();clock.textContent=dateTime(now);clock.title=now.toLocaleString('sv-SE',{hour12:false})}function updateLineNumbers(){lineNumbers.textContent=Array.from({length:content.value.split('\n').length},(_,index)=>index+1).join('\n');lineNumbers.scrollTop=content.scrollTop}function setSidebarWidth(width,persist=false){if(window.innerWidth<=760)return;const bounds=layout.getBoundingClientRect(),minimum=250,maximum=Math.max(minimum,bounds.width-366),next=Math.round(Math.max(minimum,Math.min(width,maximum)));layout.style.setProperty('--sidebar-width',next+'px');if(persist)localStorage.setItem(sidebarWidthKey,String(next))}splitter.onpointerdown=event=>{if(window.innerWidth<=760)return;splitter.setPointerCapture(event.pointerId);document.body.classList.add('resizing');setSidebarWidth(event.clientX-layout.getBoundingClientRect().left)};splitter.onpointermove=event=>{if(splitter.hasPointerCapture(event.pointerId))setSidebarWidth(event.clientX-layout.getBoundingClientRect().left)};function stopResizing(event){if(splitter.hasPointerCapture(event.pointerId))splitter.releasePointerCapture(event.pointerId);document.body.classList.remove('resizing');const value=parseInt(layout.style.getPropertyValue('--sidebar-width'),10);if(value)setSidebarWidth(value,true)}splitter.onpointerup=stopResizing;splitter.onpointercancel=stopResizing;window.addEventListener('resize',()=>{const value=parseInt(layout.style.getPropertyValue('--sidebar-width'),10);if(value)setSidebarWidth(value,true)})
function escapeHtml(value){return value.replace(/[&<>]/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[character]))}function token(className,value){return '<span class="'+className+'">'+escapeHtml(value)+'</span>'}function highlightDiff(value){return value.split('\n').map(line=>{if(/^commit\b/.test(line))return token('tok-git-head',line);if(/^(Author|Commit|Date):/.test(line))return token('tok-log',line);if(/^diff --git|^index\b/.test(line))return token('tok-diff-meta',line);if(/^@@/.test(line))return token('tok-diff-hunk',line);if(/^(\+\+\+|---)/.test(line))return token('tok-diff-meta',line);if(/^\+/.test(line))return token('tok-diff-add',line);if(/^-/.test(line))return token('tok-diff-remove',line);return escapeHtml(line)}).join('\n')}function highlightC(value){const pattern=/\/\/.*|\/\*.*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|^\s*#\s*\w+|\b(?:auto|break|case|char|const|continue|default|do|double|else|enum|extern|float|for|goto|if|inline|int|long|register|return|short|signed|sizeof|static|struct|switch|typedef|union|unsigned|void|volatile|while)\b|\b(?:0x[\da-fA-F]+|\d+(?:\.\d+)?)\b/g;return value.split('\n').map(line=>{let output='',position=0;for(const match of line.matchAll(pattern)){const text=match[0];output+=escapeHtml(line.slice(position,match.index));const className=/^\/[/\*]/.test(text)?'tok-comment':/^["']/.test(text)?'tok-string':/^\s*#/.test(text)?'tok-preprocessor':/^\d|^0x/i.test(text)?'tok-number':'tok-keyword';output+=token(className,text);position=match.index+text.length}return output+escapeHtml(line.slice(position))}).join('\n')}function highlightText(value){const pattern=/https?:\/\/[^\s]+|\b(?:ERROR|WARN|WARNING|FATAL|INFO|DEBUG)\b|\b\d+(?:\.\d+)?\b/g;return value.split('\n').map(line=>{if(/^\s*#/.test(line))return token('tok-text-head',line);let output='',position=0;for(const match of line.matchAll(pattern)){const text=match[0];output+=escapeHtml(line.slice(position,match.index));output+=token(/^https?:/.test(text)?'tok-url':/^[A-Z]+$/.test(text)?'tok-alert':'tok-number',text);position=match.index+text.length}return output+escapeHtml(line.slice(position))}).join('\n')}function updateHighlight(){const highlight=document.querySelector('#highlight'),path=current?.path||'';highlight.innerHTML=viewMode==='git'||/\.(patch|diff)$/i.test(path)?highlightDiff(content.value):/\.(c|cc|cpp|h|hpp)$/i.test(path)?highlightC(content.value):highlightText(content.value);highlight.scrollTop=content.scrollTop;highlight.scrollLeft=content.scrollLeft}function syncContentScroll(){lineNumbers.scrollTop=content.scrollTop;const highlight=document.querySelector('#highlight');highlight.scrollTop=content.scrollTop;highlight.scrollLeft=content.scrollLeft}function updateLineNumbers(){lineNumbers.textContent=Array.from({length:content.value.split('\n').length},(_,index)=>index+1).join('\n');updateHighlight();syncContentScroll()}content.addEventListener('scroll',syncContentScroll)
function syncPresetForPath(path){const matches=Array.from(presets.options).map(option=>option.value).filter(value=>value.startsWith('/')&&(path===value||path.startsWith(value+'/'))).sort((left,right)=>right.length-left.length);presets.value=matches[0]||''}
function fileIconType(name){const extension=name.includes('.')?name.split('.').pop().toLowerCase():'';if(['c','cc','cpp','h','hpp','py','js','ts','java','go','rs','sh','bash','zsh'].includes(extension))return'code';if(['json','yaml','yml','toml','ini','conf','xml'].includes(extension))return'config';if(['md','rst','txt','log'].includes(extension))return'markdown';if(['png','jpg','jpeg','gif','webp','svg'].includes(extension))return'image';if(['zip','gz','xz','bz2','tar','7z'].includes(extension))return'archive';return''}
function isDownloadablePath(path){return /\.(patch|md|txt)$/i.test(path)}
function gitDirectoryLabel(path,branch){return branch?path+'  |  branch: '+branch:path}
function setDownloadTarget(path,kind=''){downloadButton.disabled=!path||!kind;downloadButton.dataset.path=path||'';downloadButton.dataset.kind=kind;downloadButton.textContent=kind==='directory'?'Download folder':'Download file';downloadButton.title=kind==='directory'?'Download all .patch, .md, and .txt files in this directory':'Download this .patch, .md, or .txt file'}
async function downloadSelected(){const target=downloadButton.dataset.path;if(!target)return;downloadButton.disabled=true;try{const response=await fetch(base+'/api/download?path='+encodeURIComponent(target));if(!response.ok){let data={};try{data=await response.json()}catch(error){}throw new Error(data.error||'Download failed')}const blob=await response.blob(),objectUrl=URL.createObjectURL(blob),anchor=document.createElement('a'),name=target.slice(target.lastIndexOf('/')+1)||'download';anchor.href=objectUrl;anchor.download=downloadButton.dataset.kind==='directory'?name+'.zip':name;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);setStatus('Download started','ok')}catch(error){setStatus(error.message,'error')}finally{downloadButton.disabled=false}}
async function browse(target){try{viewMode='files';localStorage.setItem(lastViewModeKey,viewMode);commitCount.hidden=true;commitView.hidden=true;const data=await request(base+'/api/list?path='+encodeURIComponent(target));currentDirectory=data.path;directory.textContent=gitDirectoryLabel(data.path,data.gitBranch);syncPresetForPath(data.path);localStorage.setItem(lastDirectoryKey,data.path);if(data.gitRoot)localStorage.setItem(lastGitRootKey,data.gitRoot);entries.replaceChildren();if(data.parent){const button=document.createElement('button'),icon=document.createElement('span'),name=document.createElement('strong');button.className='entry';icon.className='icon folder';icon.setAttribute('aria-hidden','true');name.textContent='..';button.append(icon,name);button.onclick=()=>browse(data.parent);entries.append(button)}for(const entry of data.entries){const button=document.createElement('button'),icon=document.createElement('span'),info=document.createElement('span'),name=document.createElement('strong'),meta=document.createElement('span'),size=document.createElement('span'),time=document.createElement('span');button.className='entry';button.dataset.path=entry.path;icon.className=entry.directory?'icon folder':'icon file '+fileIconType(entry.name);icon.setAttribute('aria-hidden','true');info.className='entry-info';meta.className='entry-meta';name.textContent=entry.name;size.textContent=entry.directory?'Folder':fmt(entry.size);time.textContent=formatTime(entry.mtimeNs);time.title=new Date(Number(entry.mtimeNs)/1000000).toLocaleString('sv-SE',{hour12:false});meta.append(size,time);info.append(name,meta);button.append(icon,info);button.onclick=()=>entry.directory?browse(entry.path):openPath(entry.path);entries.append(button)}pathInput.value=data.path;setDownloadTarget(data.path,'directory');setStatus()}catch(error){setStatus(error.message,'error')}}
async function openFile(target){const data=await request(base+'/api/file?path='+encodeURIComponent(target));commitCount.hidden=true;commitView.hidden=true;syncPresetForPath(data.path);current=data;pathInput.value=data.path;file.textContent=data.path+'  '+fmt(data.size);content.value=data.content;updateLineNumbers();content.disabled=!data.writable;content.readOnly=!data.writable;save.disabled=!data.writable;copy.disabled=false;setDownloadTarget(isDownloadablePath(data.path)?data.path:'','file');setStatus(data.writable?'':'Read-only','ok')}
async function openPath(target){try{await openFile(target)}catch(error){if(error.message==='Path is not a regular file.')await browse(target);else if(error.message==='File exceeds the 2 MiB editing limit.'&&isDownloadablePath(target)){current=null;pathInput.value=target;file.textContent=target+'  (download only)';content.value='';updateLineNumbers();content.disabled=true;content.readOnly=true;save.disabled=true;copy.disabled=true;setDownloadTarget(target,'file');setStatus('File is larger than 2 MiB; download only','ok')}else setStatus(error.message,'error')}}
async function showGit(target){viewMode='git';localStorage.setItem(lastViewModeKey,viewMode);presets.value='__git__';try{const data=await request(base+'/api/git/commits?path='+encodeURIComponent(target));gitRoot=data.root;gitCommits=data.commits;gitStartIndex=0;commitCount.hidden=false;commitView.hidden=false;currentDirectory=data.root;localStorage.setItem(lastGitRootKey,data.root);directory.textContent=gitDirectoryLabel('Recent commits: '+data.root,data.branch);pathInput.value=data.root;entries.replaceChildren();current=null;content.value='';updateLineNumbers();content.disabled=true;content.readOnly=false;save.disabled=true;copy.disabled=true;setDownloadTarget('');file.textContent='Select a commit to view its patch';data.commits.forEach((commit,index)=>{const button=document.createElement('button'),icon=document.createElement('span'),info=document.createElement('span'),name=document.createElement('strong'),meta=document.createElement('small');button.className='entry';icon.className='icon git';icon.setAttribute('aria-hidden','true');info.className='entry-info git-entry-info';meta.className='git-entry-meta';name.textContent=commit.subject;meta.textContent=commit.shortHash+'  '+commit.date;info.append(name,meta);button.append(icon,info);button.onclick=()=>openCommitRange(index);entries.append(button)});if(data.commits.length)await openCommitRange(0);else setStatus('No commits found','ok')}catch(error){setStatus(error.message+' Browse a Git repository first.','error')}}
async function openCommitRange(startIndex){try{if(!gitRoot||!gitCommits.length)return;gitStartIndex=startIndex;const selected=gitCommits.slice(startIndex,startIndex+Number(commitCount.value));const params=new URLSearchParams({path:gitRoot,commits:selected.map(commit=>commit.hash).join(','),patch:commitView.value});const data=await request(base+'/api/git/show?'+params);current=null;content.value=data.content;updateLineNumbers();content.disabled=false;content.readOnly=true;file.textContent=data.path+' @ '+selected.length+' commit'+(selected.length===1?'':'s')+(data.patch?' + patch':' log');save.disabled=true;copy.disabled=false;setDownloadTarget('');setStatus('Read-only commit view','ok')}catch(error){setStatus(error.message,'error')}}
function markDirty(){if(!current||content.readOnly)return;const entry=Array.from(entries.querySelectorAll('button[data-path]')).find(button=>button.dataset.path===current.path);if(entry){const meta=entry.querySelector('.entry-meta');if(meta)meta.textContent='Modified, not saved'}setStatus('Modified, not saved')}
async function copyContents(){try{await navigator.clipboard.writeText(content.value);setStatus('Copied','ok')}catch(error){content.focus();content.select();if(document.execCommand('copy'))setStatus('Copied','ok');else setStatus('Copy failed','error')}}
presets.onchange=()=>{if(presets.value==='__git__'){showGit(localStorage.getItem(lastGitRootKey)||pathInput.value||currentDirectory)}else if(presets.value){browse(presets.value)}else{viewMode='files';localStorage.setItem(lastViewModeKey,viewMode);commitCount.hidden=true;commitView.hidden=true;pathInput.value='';pathInput.focus()}};document.querySelector('#open').onclick=()=>viewMode==='git'?showGit(pathInput.value):openPath(pathInput.value);document.querySelector('#up').onclick=()=>viewMode==='git'?showGit(pathInput.value||localStorage.getItem(lastGitRootKey)):browse(currentDirectory||pathInput.value);pathInput.addEventListener('keydown',event=>{if(event.key==='Enter')viewMode==='git'?showGit(pathInput.value):openPath(pathInput.value)});content.addEventListener('input',()=>{updateLineNumbers();markDirty()});content.addEventListener('scroll',()=>{lineNumbers.scrollTop=content.scrollTop});copy.onclick=copyContents;downloadButton.onclick=downloadSelected;commitCount.onchange=()=>{localStorage.setItem(commitCountKey,commitCount.value);if(viewMode==='git')openCommitRange(gitStartIndex)};commitView.onchange=()=>{localStorage.setItem(commitViewKey,commitView.value);if(viewMode==='git')openCommitRange(gitStartIndex)};save.onclick=async()=>{if(!current)return;save.disabled=true;try{const data=await request(base+'/api/file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:current.path,content:content.value,mtimeNs:current.mtimeNs})});current={...current,...data,content:content.value};file.textContent=current.path+'  '+fmt(current.size);await browse(currentDirectory);setStatus('Saved','ok')}catch(error){setStatus(error.message,'error')}finally{save.disabled=!current?.writable}};const savedCommitCount=localStorage.getItem(commitCountKey),savedCommitView=localStorage.getItem(commitViewKey),savedSidebarWidth=parseInt(localStorage.getItem(sidebarWidthKey),10);if(['1','2','3','4','5','6','7','8'].includes(savedCommitCount))commitCount.value=savedCommitCount;if(['0','1'].includes(savedCommitView))commitView.value=savedCommitView;if(savedSidebarWidth)setSidebarWidth(savedSidebarWidth);updateLineNumbers();updateClock();setInterval(updateClock,1000);request(base+'/api/roots').then(data=>{data.presets.forEach(item=>{const option=document.createElement('option');option.value=item;option.textContent=item==='__git__'?'Git commits':item||'Custom path';presets.append(option)});const lastPath=localStorage.getItem(lastDirectoryKey)||data.roots[0];if(localStorage.getItem(lastViewModeKey)==='git')showGit(localStorage.getItem(lastGitRootKey)||lastPath);else browse(lastPath)}).catch(error=>setStatus(error.message,'error'));
</script></body></html>'''


if __name__ == "__main__":
    if not ALLOWED_ROOTS:
        raise RuntimeError("FILE_PORTAL_ROOTS must contain at least one path")
    server = ThreadingHTTPServer(("127.0.0.1", 7683), Handler)
    print(f"Serving {', '.join(map(str, ALLOWED_ROOTS))} on 127.0.0.1:7683{BASE_PATH}/", flush=True)
    server.serve_forever()
