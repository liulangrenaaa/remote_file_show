#!/usr/bin/env python3
"""Restricted text file browser and editor for the local workspace."""

import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


BASE_PATH = os.environ.get("FILE_PORTAL_BASE_PATH", "/shell_file").rstrip("/")
MAX_FILE_SIZE = 2 * 1024 * 1024
ALLOWED_ROOTS = tuple(
    Path(item).resolve()
    for item in os.environ.get("FILE_PORTAL_ROOTS", "/tmp:/home/ubuntu/workspace:/home/ubuntu/.gemini").split(":")
    if item
)
PATH_PRESETS = tuple(os.environ.get("FILE_PORTAL_PRESETS", "/tmp|/home/ubuntu/workspace|/home/ubuntu/.gemini|__git__").split("|"))
DOWNLOADABLE_SUFFIXES = frozenset({".patch", ".md", ".txt"})
MAX_DOWNLOAD_FILE_SIZE = 10 * 1024 * 1024
LOG_FILE = Path(os.environ.get("FILE_PORTAL_LOG_FILE", "/tmp/file_portal.log"))


def log_event(client_ip, event_type, details=""):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail_str = f" {details}" if details else ""
    line = f"[{timestamp}] [{client_ip}] [{event_type}]{detail_str}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


class PortalError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def is_within_roots(path):
    return any(path == root or root in path.parents for root in ALLOWED_ROOTS)


def resolve_path(value, require_exists=True):
    if not isinstance(value, str):
        raise PortalError(HTTPStatus.BAD_REQUEST, "A valid path string is required.")

    clean = value.strip().strip("'\"")
    for prefix in ("patch:", "patch：", "file:", "file："):
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):].strip().strip("'\"")
    if clean.startswith("~"):
        clean = os.path.expanduser(clean)
    if not clean.startswith("/"):
        raise PortalError(HTTPStatus.BAD_REQUEST, "An absolute path is required.")

    for root in ALLOWED_ROOTS:
        root_str = str(root)
        idx = clean.rfind(root_str)
        if idx > 0:
            candidate_p = Path(clean[idx:])
            if candidate_p.exists():
                clean = clean[idx:]
                break

    candidate = Path(clean)
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
        "mtimeNs": str(stat.st_mtime_ns),
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


def is_safe_git_ref(ref):
    if not ref or len(ref) > 200:
        return False
    if ref.startswith("-") or ref.startswith(".") or ".." in ref:
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9_./+-]+", ref))


def git_branches_for(root):
    try:
        result = subprocess.run(
            [
                "git", "-C", str(root), "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)%00%(committerdate:unix)%00%(committerdate:relative)",
                "refs/heads/",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    local_branches = []
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\x00")
        if not parts or not parts[0].strip():
            continue
        name = parts[0].strip()
        ts = 0
        rel = ""
        if len(parts) >= 2:
            try:
                ts = int(parts[1].strip())
            except ValueError:
                ts = 0
        if len(parts) >= 3:
            rel = parts[2].strip()
        local_branches.append({
            "name": name,
            "committerDate": ts,
            "relative": rel,
        })
    return local_branches


def resolve_git_root(value):
    path = resolve_path(value)
    root = git_root_for(path)
    if root is None:
        if path.is_dir():
            try:
                for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
                    if child.is_dir():
                        child_root = git_root_for(child)
                        if child_root:
                            return child_root
            except OSError:
                pass
        raise PortalError(HTTPStatus.BAD_REQUEST, "Path is not inside a Git worktree.")
    return root


def is_upstream_ref(ref_name, current_branch=None):
    clean = ref_name.strip()
    if clean.startswith("tag: ") or clean.startswith("HEAD ->") or clean.startswith("HEAD"):
        return False
    if current_branch:
        clean_target = current_branch.removeprefix("refs/heads/").removeprefix("refs/remotes/").strip()
        if clean == clean_target or clean.endswith(f"/{clean_target}"):
            return False
    if "/" not in clean and clean not in ("master", "main"):
        return False
    for prefix in ("origin/", "upstream/", "bpf-next/", "bpf/", "linux-next/", "tip/"):
        if clean.startswith(prefix):
            return True
    if clean.endswith("/master") or clean.endswith("/main") or clean.endswith("/HEAD"):
        return True
    if clean in ("master", "main"):
        return True
    return False


def is_linus_ref(ref_name):
    clean = ref_name.strip()
    return clean in ("origin/master", "origin/HEAD", "upstream/master", "master", "main") or (
        (clean.startswith("origin/") or clean.startswith("upstream/")) and (clean.endswith("/master") or clean.endswith("/main"))
    )


def format_upstream_tree(ref_name):
    clean = (ref_name or "").strip()
    is_base = False
    if clean.endswith(" base"):
        clean = clean[:-5].strip()
        is_base = True

    if "bpf-next" in clean:
        tree = "bpf-next tree"
    elif clean.startswith("bpf/") or clean == "bpf":
        tree = "bpf tree"
    elif "tip" in clean and ("sched" in clean or "schedule" in clean):
        tree = "tip/schedule tree"
    elif clean.startswith("tip/") or clean == "tip":
        tree = "tip/schedule tree"
    elif clean in ("sched-core", "sched/core"):
        tree = "tip/schedule tree"
    elif "linux-next" in clean:
        tree = "linux-next tree"
    elif "net-next" in clean:
        tree = "net-next tree"
    elif clean.startswith("net/") or clean == "net":
        tree = "net tree"
    elif is_linus_ref(clean) or clean in ("origin/master", "master", "main", "origin/main", "upstream/master"):
        tree = "master tree"
    else:
        tree = clean

    return f"{tree} base" if is_base else tree


_MASTER_BASE_CACHE = {}


def find_master_base(root, target_ref, active_branch=None):
    ref_to_check = active_branch if target_ref == "HEAD" and active_branch else target_ref
    clean_target = (ref_to_check or "").removeprefix("refs/heads/").removeprefix("refs/remotes/").strip()
    if not clean_target or clean_target in ("master", "main", "origin/master", "origin/main", "upstream/master", "upstream/main"):
        return None, None

    cache_key = (str(root), clean_target)
    if cache_key in _MASTER_BASE_CACHE:
        return _MASTER_BASE_CACHE[cache_key]

    target_lower = clean_target.lower()
    candidate_list = [
        "refs/remotes/origin/master",
        "refs/remotes/origin/main",
        "refs/remotes/upstream/master",
        "refs/remotes/upstream/main",
        "refs/remotes/tip/sched/core",
        "refs/remotes/tip/schedule",
        "refs/remotes/tip/sched-core",
        "refs/remotes/tip/master",
        "refs/remotes/bpf-next/master",
        "refs/remotes/bpf/master",
        "refs/remotes/net-next/master",
        "refs/remotes/net/master",
        "refs/heads/master",
        "refs/heads/main",
    ]

    try:
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{target_ref}@{{u}}"],
            capture_output=True,
            text=True,
            timeout=0.3,
        )
        if res.returncode == 0 and res.stdout.strip():
            tracking_ref = res.stdout.strip()
            if tracking_ref not in candidate_list:
                candidate_list.insert(0, tracking_ref)
    except (OSError, subprocess.TimeoutExpired):
        pass

    best_candidate = None
    min_dist = None
    best_pref = -1

    for ref in candidate_list:
        try:
            res = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                capture_output=True,
                text=True,
                timeout=0.3,
            )
            if res.returncode != 0 or not res.stdout.strip():
                continue
            ref_commit = res.stdout.strip()
            mb = subprocess.run(
                ["git", "-C", str(root), "merge-base", target_ref, ref_commit],
                capture_output=True,
                text=True,
                timeout=0.4,
            )
            if mb.returncode != 0 or not mb.stdout.strip():
                continue
            base_commit = mb.stdout.strip()

            cnt = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", f"{base_commit}..{target_ref}"],
                capture_output=True,
                text=True,
                timeout=0.4,
            )
            if cnt.returncode != 0 or not cnt.stdout.strip():
                continue
            dist = int(cnt.stdout.strip())

            found_ref = ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")

            pref = 0
            if "tip" in target_lower and "tip" in ref.lower():
                pref = 10
            elif "bpf" in target_lower and "bpf" in ref.lower():
                pref = 10
            elif "net" in target_lower and "net" in ref.lower():
                pref = 10
            elif "origin/master" in ref:
                pref = 5
            elif "master" in ref:
                pref = 2

            if min_dist is None or dist < min_dist or (dist == min_dist and pref > best_pref):
                min_dist = dist
                best_pref = pref
                label = found_ref if base_commit == ref_commit else f"{found_ref} base"
                best_candidate = (base_commit, label, dist)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue

    if best_candidate and best_candidate[2] > 0:
        ret = (best_candidate[0], best_candidate[1])
    else:
        ret = (None, None)

    _MASTER_BASE_CACHE[cache_key] = ret
    return ret


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

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = self.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        return self.address_string()

    def log_action(self, event_type, details=""):
        log_event(self.client_ip(), event_type, details)

    def log_message(self, format_string, *args):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{self.client_ip()}] [HTTP] {format_string % args}", flush=True)

    def do_HEAD(self):
        self._is_head = True
        return self.do_GET()

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
        if request.path == f"{BASE_PATH}/api/logs":
            return self.get_logs(request)
        return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        request = urlparse(self.path)
        if request.path != f"{BASE_PATH}/api/file":
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        payload = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_FILE_SIZE + 65536:
                raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request is too large.")
            payload = json.loads(self.rfile.read(length))
            self.write_file(payload)
        except json.JSONDecodeError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be JSON.")
        except PortalError as error:
            raw_path = payload.get("path") if isinstance(payload, dict) else None
            self.send_error_json(error.status, error.message, path=raw_path)

    def get_logs(self, request):
        try:
            query = parse_qs(request.query)
            try:
                limit = min(max(int(query.get("lines", ["100"])[0]), 1), 1000)
            except ValueError:
                limit = 100
            lines = []
            if LOG_FILE.exists():
                try:
                    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                        all_lines = f.readlines()
                        lines = [line.rstrip("\r\n") for line in all_lines[-limit:]]
                except OSError as error:
                    raise PortalError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Cannot read log file: {error}") from None
            self.log_action("GET_LOGS", f"returned={len(lines)} limit={limit}")
            self.send_json(HTTPStatus.OK, {"lines": lines, "total": len(lines)})
        except PortalError as error:
            self.send_error_json(error.status, error.message)

    def list_directory(self, request):
        raw_path = None
        try:
            query = parse_qs(request.query)
            raw_path = query.get("path", [str(ALLOWED_ROOTS[0])])[0]
            action = query.get("action", ["browse"])[0]
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
                        "mtimeNs": str(item_stat.st_mtime_ns),
                    })
                except OSError:
                    continue
            parent = str(path.parent) if path != next(root for root in ALLOWED_ROOTS if path == root or root in path.parents) else None
            git_root = git_root_for(path)
            git_branch = git_branch_for(git_root) if git_root else None
            event_name = "REFRESH_DIR" if action == "refresh" else "LIST_DIR"
            self.log_action(event_name, f"path={path} entries={len(entries)} gitRoot={git_root or 'none'}")
            self.send_json(HTTPStatus.OK, {
                "path": str(path),
                "parent": parent,
                "entries": entries,
                "gitRoot": str(git_root) if git_root else None,
                "gitBranch": git_branch,
            })
        except PortalError as error:
            self.send_error_json(error.status, error.message, path=raw_path)

    def read_file(self, request):
        raw_path = None
        try:
            query = parse_qs(request.query)
            raw_path = query.get("path", [None])[0]
            action = query.get("action", ["open"])[0]
            path = resolve_path(raw_path)
            if path.is_dir():
                self.send_json(HTTPStatus.OK, {"isDirectory": True, "path": str(path)})
                return
            content = ensure_text_file(path)
            meta = metadata(path)
            event_name = "REFRESH_FILE" if action == "refresh" else ("RESTORE_FILE" if action == "restore" else "OPEN_FILE")
            self.log_action(event_name, f"path={path} size={meta['size']} writable={meta['writable']}")
            self.send_json(HTTPStatus.OK, {**meta, "isDirectory": False, "content": content})
        except PortalError as error:
            self.send_error_json(error.status, error.message, path=raw_path)

    def download_path(self, request):
        raw_path = None
        try:
            raw_path = parse_qs(request.query).get("path", [None])[0]
            path = resolve_path(raw_path)
            if path.is_file():
                data = read_download_file(path)
                self.log_action("DOWNLOAD", f"kind=file path={path} size={len(data)}")
                self.send_download(data, path.name, "text/plain; charset=utf-8")
            elif path.is_dir():
                self.log_action("DOWNLOAD", f"kind=directory path={path}")
                self.download_directory(path)
            else:
                raise PortalError(HTTPStatus.BAD_REQUEST, "Path is not a regular file or directory.")
        except PortalError as error:
            self.send_error_json(error.status, error.message, path=raw_path)

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
        try:
            encoded_filename = quote(filename, safe="")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_filename}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not getattr(self, "_is_head", False):
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def list_commits(self, request):
        raw_path = None
        try:
            query = parse_qs(request.query)
            raw_path = query.get("path", [None])[0]
            action = query.get("action", ["list"])[0]
            root = resolve_git_root(raw_path)
            branches = git_branches_for(root)
            active_branch = git_branch_for(root)

            requested_branch = query.get("branch", [None])[0]
            target_ref = None
            if requested_branch and is_safe_git_ref(requested_branch):
                clean_ref = requested_branch.removeprefix("refs/heads/")
                if clean_ref == "HEAD":
                    target_ref = "HEAD"
                else:
                    verify = subprocess.run(
                        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"refs/heads/{clean_ref}^{{commit}}"],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    if verify.returncode == 0:
                        target_ref = clean_ref

            if not target_ref:
                target_ref = active_branch if active_branch and not active_branch.startswith("detached HEAD") else "HEAD"

            if active_branch and active_branch.startswith("detached HEAD"):
                branches.insert(0, {"name": "HEAD", "label": active_branch, "committerDate": 0, "relative": ""})

            output = git_output([
                "git", "-C", str(root), "log", "-30",
                "--date=format:%m-%d %H:%M:%S", "--format=%H%x1f%h%x1f%ad%x1f%s%x1f%D",
                target_ref, "--",
            ])
            raw_lines = [line.split("\x1f", 4) for line in output.splitlines() if len(line.split("\x1f", 4)) >= 4]

            master_base_hash, master_label = find_master_base(root, target_ref, active_branch)

            commits = []
            found_first_master = False
            base_index = -1
            for index, parts in enumerate(raw_lines):
                full_hash = parts[0]
                short_hash = parts[1]
                date = parts[2]
                subject = parts[3]
                decorations = parts[4] if len(parts) > 4 else ""

                is_master = False
                badge = None
                badge_detail = None
                if master_base_hash and full_hash == master_base_hash and not found_first_master and index > 0:
                    is_master = True
                    base_index = index
                    ref_name = master_label or "upstream"
                    tree_name = format_upstream_tree(ref_name)
                    badge = f"{tree_name} ({short_hash})"
                    badge_detail = f"{tree_name} [{ref_name}] ({short_hash}: {subject})"
                    found_first_master = True

                commit_item = {
                    "hash": full_hash,
                    "shortHash": short_hash,
                    "date": date,
                    "subject": subject,
                    "isMaster": is_master,
                }
                if is_master:
                    commit_item["badge"] = badge
                    commit_item["badgeDetail"] = badge_detail or master_label or decorations or "upstream"
                commits.append(commit_item)

            event_name = "REFRESH_GIT" if action == "refresh" else "LIST_COMMITS"
            branch_commits_count = base_index if base_index >= 0 else (0 if master_base_hash else len(commits))
            log_detail = (
                f"root={root} branch={target_ref} commits={len(commits)} "
                f"base={master_base_hash[:12] if master_base_hash else 'none'} "
                f"label='{master_label or 'none'}' base_idx={base_index} "
                f"branch_commits={branch_commits_count}"
            )
            self.log_action(event_name, log_detail)
            self.send_json(HTTPStatus.OK, {
                "root": str(root),
                "branch": target_ref,
                "activeBranch": active_branch,
                "branches": branches,
                "commits": commits,
            })
        except PortalError as error:
            self.send_error_json(error.status, error.message, path=raw_path)

    def show_commit(self, request):
        raw_path = None
        try:
            query = parse_qs(request.query)
            raw_path = query.get("path", [None])[0]
            root = resolve_git_root(raw_path)
            commits = query.get("commits", [query.get("commit", [""])[0]])[0].split(",")
            if not 1 <= len(commits) <= 20 or any(
                not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit) for commit in commits
            ):
                raise PortalError(HTTPStatus.BAD_REQUEST, "Provide between one and twenty commit identifiers.")
            view_mode = query.get("view", query.get("patch", ["1"]))[0]
            if view_mode not in ("0", "1", "stat", "patch", "log"):
                raise PortalError(HTTPStatus.BAD_REQUEST, "Commit display mode must be 0, 1, stat, patch, or log.")
            command = ["git", "-C", str(root), "log", "--no-walk=unsorted", "--decorate=short"]
            if view_mode in ("1", "patch"):
                command.append("--patch")
                mode_label = "patch"
            elif view_mode == "stat":
                command.append("--stat")
                mode_label = "stat"
            else:
                mode_label = "log"
            content = git_output([*command, *commits])
            self.log_action("SHOW_COMMIT", f"root={root} commits={','.join(commits)} mode={mode_label}")
            self.send_json(HTTPStatus.OK, {
                "path": str(root), "commits": commits, "mode": mode_label, "patch": mode_label == "patch", "content": content,
            })
        except PortalError as error:
            self.send_error_json(error.status, error.message, path=raw_path)

    def write_file(self, payload):
        raw_path = payload.get("path")
        path = resolve_path(raw_path)
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
        mtime_matches = False
        if expected_mtime is not None:
            if str(expected_mtime) == str(current_mtime):
                mtime_matches = True
            else:
                try:
                    exp_int = int(expected_mtime)
                    if abs(exp_int - current_mtime) <= 10_000_000:
                        mtime_matches = True
                except (ValueError, TypeError):
                    pass
        if not mtime_matches:
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
        meta = metadata(path)
        self.log_action("SAVE_FILE", f"path={path} size={meta['size']}")
        self.send_json(HTTPStatus.OK, meta)

    def send_json(self, status, payload):
        try:
            data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not getattr(self, "_is_head", False):
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_error_json(self, status, message, path=None):
        details = f"status={int(status)} message={message!r}"
        if path:
            details += f" path={path}"
        self.log_action("ERROR", details)
        self.send_json(status, {"error": message})

    def send_html(self):
        try:
            data = INDEX_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not getattr(self, "_is_head", False):
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_favicon(self):
        try:
            data = FAVICON_SVG.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not getattr(self, "_is_head", False):
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><path fill="#f4ba5b" d="M7 17a6 6 0 0 1 6-6h15l7 8h16a6 6 0 0 1 6 6v25a6 6 0 0 1-6 6H13a6 6 0 0 1-6-6z"/><path fill="#49c5a9" d="M7 27h50v23a6 6 0 0 1-6 6H13a6 6 0 0 1-6-6z"/><path fill="#172029" d="M18 35h28v4H18zm0 9h20v4H18z"/></svg>'''


INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="/shell_file/favicon.svg" type="image/svg+xml"><title>Workspace Files</title>
<style>
.content-shell{min-height:0;height:100%;overflow:hidden;grid-template-rows:minmax(0,1fr)}.line-numbers{min-height:0;height:100%}
#layout.layout{min-height:0;height:100%;overflow:hidden;grid-template-columns:minmax(300px,clamp(30%,var(--sidebar-width,30%),55%)) 6px minmax(440px,1fr);grid-template-rows:minmax(0,1fr)}
.code-layer{position:relative;min-width:0;min-height:0;overflow:hidden;background:#101820}.highlight{position:absolute;inset:0;margin:0;padding:16px;border:0;outline:0;overflow:hidden;pointer-events:none;white-space:pre;color:#e7efe9;font:13px/20px ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;letter-spacing:0;word-spacing:0;font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0;text-rendering:optimizeSpeed;-webkit-font-smoothing:subpixel-antialiased;tab-size:4;-moz-tab-size:4}.code-layer textarea{position:absolute;inset:0;margin:0;padding:16px;border:0;outline:0;background:transparent;color:transparent;caret-color:#e7efe9;white-space:pre;overflow-wrap:normal;word-break:normal;font:13px/20px ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;letter-spacing:0;word-spacing:0;font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0;text-rendering:optimizeSpeed;-webkit-font-smoothing:subpixel-antialiased;tab-size:4;-moz-tab-size:4}.code-layer textarea:disabled{opacity:1}.code-layer textarea::-moz-selection{background:#264f78;color:#eff4f1}.code-layer textarea::selection{background:#264f78;color:#eff4f1}.code-layer textarea::placeholder{color:#aab9b6}.tok-comment{color:#7e938f}.tok-string{color:#d9c874}.tok-keyword,.tok-preprocessor{color:#74b9ff}.tok-number{color:#e99b6d}.tok-diff-add{color:#75c995}.tok-diff-remove{color:#f48670}.tok-diff-hunk{color:#d9c874}.tok-diff-meta{color:#72bce4}.tok-git-head{color:#c89cff}.tok-log{color:#d9c874}.tok-url{color:#72bce4}.tok-alert{color:#f48670}.tok-text-head{color:#c89cff}
#entries .git-entry-info{display:flex;flex-direction:column;justify-content:center;gap:3px;min-width:0;flex:1;min-height:38px;padding:3px 0}#entries .git-entry-info strong{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;line-height:18px;font-size:13px;font-weight:500;color:#eff4f1;word-break:break-word}#entries .git-entry-meta{display:flex;align-items:center;gap:10px;font:11px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap}#entries .git-entry-meta .commit-hash{color:#49c5a9;font-weight:600}#entries .git-entry-meta .commit-date{color:#788d9a}#entries .entry:hover .git-entry-meta .commit-date{color:#9cb1be}#entries .entry.active .git-entry-meta .commit-hash{color:#5eead4}#entries .entry.active .git-entry-meta .commit-date{color:#a4bcc9}.entry.git-commit-entry{align-items:flex-start}#entries .entry.git-commit-entry .icon.git{margin-top:4px}#entries .entry.upstream-commit strong{color:#9cb1be;font-weight:400}#entries .entry.upstream-commit:hover strong{color:#eff4f1}#entries .entry.upstream-commit .git-entry-meta .commit-hash{color:#6a8898}#entries .entry.upstream-commit:hover .git-entry-meta .commit-hash{color:#49c5a9}#entries .entry.upstream-commit .git-entry-meta .commit-date{color:#637785}.git-master-divider{margin:8px 6px 5px;padding:6px 10px;background:#141f27;border:1px solid #2d4351;border-left:3px solid #f4ba5b;border-radius:4px;display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11px;font-weight:700;color:#ffd166;letter-spacing:0.5px;user-select:none;cursor:pointer;transition:background 0.15s ease,border-color 0.15s ease}.git-master-divider:hover{background:#1a2a35;border-color:#3f5d71}.git-master-divider .divider-tag{display:flex;align-items:center;gap:5px;font-weight:800;color:#ffd166;font-size:11px}.git-master-divider .divider-toggle{display:flex;align-items:center;gap:5px;font-size:11px;font-weight:600;color:#8fa6b4;cursor:pointer;transition:color 0.15s ease}.git-master-divider:hover .divider-toggle{color:#fff}.git-master-divider .divider-arrow{display:inline-block;font-size:10px;color:#f4ba5b;margin-right:2px}#entries .entry.upstream-commit.collapsed{display:none !important}.entry.master-commit{position:relative;background:rgba(244,186,91,0.07) !important;border-left:3px solid #f4ba5b !important;margin:2px 0}#entries .entry.master-commit:hover{background:rgba(244,186,91,0.14) !important}#entries .entry.master-commit.active{background:rgba(244,186,91,0.22) !important;box-shadow:inset 4px 0 0 #f4ba5b !important}#entries .entry.master-commit .git-entry-meta .commit-hash{color:#ffd166}#entries .entry.master-commit .git-entry-meta .commit-date{color:#c9a65d}.master-badge{display:inline-flex;align-items:center;gap:3px;background:rgba(244,186,91,0.16);border:1px solid #f4ba5b;color:#ffd166;font-size:11px;font-weight:700;line-height:1.2;padding:1px 6px;border-radius:3px;margin-right:6px;vertical-align:1px;white-space:nowrap;letter-spacing:0.3px}
:root{color-scheme:dark;--bg:#141c24;--panel:#19232c;--line:#2c3e4b;--text:#eff4f1;--muted:#8b9fae;--teal:#49c5a9;--gold:#f4ba5b;--danger:#f48670}*{box-sizing:border-box}html,body{height:100%;margin:0;overflow:hidden;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,sans-serif}.app{height:100vh;max-height:100vh;overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr)}header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:nowrap;gap:8px;align-items:center;background:#151e27}h1{font-size:15px;margin:0 4px 0 0;font-weight:700;color:#eff4f1;letter-spacing:0.2px;white-space:nowrap}.folder-mark{position:relative;width:20px;height:15px;border-radius:2px;background:var(--teal);margin-left:1px}.folder-mark:before{content:"";position:absolute;width:10px;height:4px;top:-3px;left:1px;border-radius:2px 2px 0 0;background:var(--gold)}.path-bar{flex:1;min-width:280px;height:33px;background:#0d151c;border:1px solid #304452;border-radius:6px;display:flex;align-items:center;overflow:hidden;transition:border-color .15s,box-shadow .15s,background .15s}.path-bar:hover{border-color:#3f5d71;background:#101922}.path-bar:focus-within{border-color:var(--teal);box-shadow:0 0 0 2px rgba(73,197,169,0.22);background:#0f1821}.preset-select{flex:0 0 auto;width:auto;max-width:145px;height:100%;background:#141f28;border:none;border-right:1px solid #283a47;color:#5eead4;font-size:12px;font-weight:600;padding:0 10px;outline:none;cursor:pointer;text-overflow:ellipsis;transition:color .15s,background .15s}.preset-select:hover,.preset-select:focus{background:#1a2a36;color:#fff}.preset-select option{background:#111921;color:#eff4f1;font-size:12px;padding:4px}.path-prefix-icon{display:flex;align-items:center;justify-content:center;padding-left:9px;color:#5c7587;flex-shrink:0;user-select:none}.path{flex:1;min-width:120px;height:100%;background:transparent;border:none;color:#f2f7f4;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12.5px;padding:0 8px 0 6px;outline:none}.path::-webkit-calendar-picker-indicator{cursor:pointer;opacity:0.55;filter:invert(0.8) sepia(1) saturate(5) hue-rotate(120deg);transition:opacity .15s,transform .15s;padding:2px 4px}.path::-webkit-calendar-picker-indicator:hover{opacity:1;transform:scale(1.15)}.clock{min-width:110px;color:var(--muted);font:12px ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap;text-align:right}button,.commit-count{border:1px solid #304654;border-radius:4px;background:#1c2a35;color:#e1e9ec;padding:7px 11px;font-size:12px;cursor:pointer;transition:border-color .15s,background .15s}button:hover,.commit-count:hover{border-color:var(--teal);background:#223441;color:#fff}button:disabled{opacity:.5;cursor:not-allowed}.save{background:#166e5d;border-color:#219d86;color:#fff;font-weight:600}.save:hover:not(:disabled){background:#1c8672;border-color:var(--teal)}#open{background:#1b3340;border-color:#2f5367;color:#5eead4;font-weight:600}#open:hover{background:#234253;border-color:var(--teal);color:#fff}.copy{position:relative;width:31px;height:31px;padding:0}.copy:before,.copy:after{content:"";position:absolute;width:10px;height:12px;border:1px solid currentColor;border-radius:1px}.copy:before{left:10px;top:8px}.copy:after{left:7px;top:5px;background:#1c2a35}.commit-count{height:31px;padding:0 8px;font:12px system-ui,sans-serif}.layout{min-height:0;height:100%;overflow:hidden;display:grid;grid-template-columns:minmax(250px,var(--sidebar-width,30%)) 6px minmax(360px,1fr);grid-template-rows:minmax(0,1fr)}.browser{min-height:0;height:100%;overflow-y:auto;overflow-x:hidden;background:#162029}.splitter{position:relative;cursor:col-resize;background:var(--line);touch-action:none}.splitter:after{content:"";position:absolute;inset:0 -3px}.splitter:hover,.splitter:focus{background:var(--teal);outline:0}body.resizing,body.resizing *{cursor:col-resize!important;user-select:none!important}.crumb{padding:8px 12px;border-bottom:1px solid var(--line);background:#141f27;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;min-height:44px;display:flex;flex-direction:column;justify-content:center;gap:6px}.crumb-trail{display:flex;flex-wrap:wrap;align-items:center;gap:2px 3px;line-height:1.35}.crumb-segment{display:inline-flex;align-items:center;padding:1.5px 5px;border-radius:3px;color:#8da5b5;font-size:11.5px;font-weight:500;text-decoration:none;cursor:pointer;white-space:nowrap;transition:background .12s ease,color .12s ease}.crumb-segment:hover{background:#20313e;color:#5eead4}.crumb-segment.active{color:#eff4f1;font-weight:700;background:rgba(73,197,169,0.14);border:1px solid rgba(73,197,169,0.28);cursor:default}.crumb-segment.disabled{color:#556c7b;cursor:default}.crumb-sep{color:#3f5564;font-size:11px;user-select:none;padding:0 1px}.crumb-git-line{display:flex;align-items:center;gap:6px;margin-top:1px}.crumb-git-badge{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:10px;background:rgba(73,197,169,0.12);border:1px solid rgba(73,197,169,0.32);color:#5eead4;font-size:11px;font-weight:600;cursor:pointer;max-width:100%;transition:all .15s ease}.crumb-git-badge:hover{background:rgba(73,197,169,0.22);border-color:#5eead4;color:#fff}.crumb-git-icon{font-size:11px;color:#49c5a9}.crumb-git-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}.crumb-git-arrow{font-size:10px;color:#8da5b5;margin-left:2px}.crumb-git-badge:hover .crumb-git-arrow{color:#fff}.crumb-git-card{display:flex;flex-direction:column;gap:4px}.crumb-git-card-top{display:flex;align-items:center;gap:6px}.crumb-git-repo-tag{display:inline-flex;align-items:center;padding:1px 5px;font-size:10px;font-weight:800;letter-spacing:0.5px;border-radius:3px;background:rgba(73,197,169,0.16);border:1px solid #49c5a9;color:#5eead4;flex-shrink:0}.crumb-git-repo-name{font-size:13px;font-weight:700;color:#eff4f1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.crumb-git-browse-btn{padding:2px 7px;font-size:11px;font-weight:600;border-radius:3px;background:#1a2a35;border:1px solid #2d4857;color:#8fa6b4;cursor:pointer;transition:all .15s;flex-shrink:0}.crumb-git-browse-btn:hover{background:#233b4b;border-color:#49c5a9;color:#fff}.crumb-git-card-path{font-size:11px;color:#6e8697;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.entry{display:flex;align-items:center;gap:10px;width:100%;border:0;border-radius:0;background:transparent;text-align:left;padding:7px 12px;color:var(--text);border-left:3px solid transparent;transition:background .12s ease}.entry:hover{background:#1f2e39}.entry.active{background:#1c3340;border-left-color:var(--teal)}.entry-info{display:flex;align-items:center;gap:10px;min-width:0;flex:1}.entry-info strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}.entry-meta{display:flex;gap:8px;flex:0 0 auto;color:var(--muted);font:12px ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap}.icon{position:relative;display:block;flex:0 0 18px;width:18px;height:19px}.icon.folder{height:14px;margin-top:3px;border-radius:2px;background:#d9a858}.icon.folder:before{content:"";position:absolute;top:-3px;left:1px;width:9px;height:4px;border-radius:2px 2px 0 0;background:#f2c56e}.icon.file{width:14px;height:18px;margin-left:2px;border:1px solid currentColor;border-radius:2px;color:#aab9b6;background:#24313a}.icon.file:before{content:"";position:absolute;top:-1px;right:-1px;border-top:6px solid #1c2730;border-left:6px solid transparent}.icon.file.code{color:#5bb4e6}.icon.file.config{color:#d6c15e}.icon.file.markdown{color:#67cba3}.icon.file.image{color:#d88cbe}.icon.file.archive{color:#e99662}.icon.git{position:relative;display:block;flex:0 0 10px;width:10px;height:10px;margin:0 6px 0 2px;border-radius:50%;border:2px solid #49c5a9;background:#172029;box-shadow:0 0 0 2px rgba(73,197,169,0.15);transition:all .15s ease;transform:none}.icon.git:before,.icon.git:after{display:none}#entries .entry:hover .icon.git{border-color:#5eead4;background:#1d3137;box-shadow:0 0 0 3px rgba(94,234,212,0.25)}#entries .entry.active .icon.git{border-color:#5eead4;background:#5eead4;box-shadow:0 0 6px rgba(94,234,212,0.6)}#entries .entry.master-commit .icon.git{border-color:#f4ba5b;background:#f4ba5b;box-shadow:0 0 6px rgba(244,186,91,0.5)}#entries .entry.upstream-commit .icon.git{border-color:#5a6e7c;background:#172029;box-shadow:none;opacity:0.7}.editor{min-width:0;min-height:0;height:100%;overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr)}.filebar{padding:8px 14px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center;background:#162029;color:var(--muted);font-size:12px}.filebar span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#file{color:#d1dde3;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}.status{margin-left:auto;color:var(--muted);white-space:nowrap}.status.error{color:var(--danger)}.status.ok{color:var(--teal)}.content-shell{min-height:0;height:100%;overflow:hidden;display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-rows:minmax(0,1fr);background:#101820}.line-numbers{min-height:0;height:100%;margin:0;padding:16px 8px;min-width:52px;overflow:hidden;border-right:1px solid #2a3942;background:#172029;color:#718582;text-align:right;user-select:none;font:13px/20px ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;letter-spacing:0;font-variant-ligatures:none;tab-size:4;-moz-tab-size:4}textarea{width:100%;height:100%;resize:none;border:0;outline:0;padding:16px;margin:0;background:#101820;color:#e7efe9;font:13px/20px ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;letter-spacing:0;font-variant-ligatures:none;tab-size:4;-moz-tab-size:4;white-space:pre;overflow-wrap:normal;word-break:normal}.branch-select{flex:0 0 auto;min-width:200px;max-width:480px;height:31px;padding:0 10px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;font-weight:600;color:#5eead4;background:#12212b;border:1px solid #2d4857;border-radius:4px;cursor:pointer;text-overflow:ellipsis;box-shadow:inset 0 1px 2px rgba(0,0,0,0.25);transition:border-color .15s,background .15s,color .15s}.branch-select:hover,.branch-select:focus{border-color:var(--teal);background:#162b38;color:#fff;outline:none}.branch-select optgroup{background:#172029;color:var(--teal);font-family:system-ui,sans-serif;font-weight:700;font-size:11px}.branch-select optgroup:last-of-type{color:#8295a3}.branch-select option{background:#101820;color:#eaf1ed;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;font-weight:normal;padding:4px 6px}@media(max-width:760px){.layout{grid-template-columns:1fr;grid-template-rows:minmax(0,36%) minmax(0,64%)}.splitter{display:none}.browser{border-bottom:1px solid var(--line)}header{flex-wrap:wrap}.path-bar{order:2;flex-basis:100%}.entry-meta{gap:5px;font-size:11px}}
.md-toggle{height:31px;padding:0 10px;font:12px system-ui,sans-serif;cursor:pointer}
.md-toggle.active{background:#167b68;border-color:#219d86;color:#fff;font-weight:700}
.md-preview{min-height:0;height:100%;overflow-y:auto;overflow-x:hidden;padding:20px 28px;background:#101820;color:#e7efe9;line-height:1.6;word-break:break-word}
.md-preview h1,.md-preview h2,.md-preview h3,.md-preview h4{color:#eff4f1;font-weight:650;margin-top:20px;margin-bottom:10px}
.md-preview h1{font-size:22px;border-bottom:1px solid #40515a;padding-bottom:6px}
.md-preview h2{font-size:18px;border-bottom:1px solid rgba(64,81,90,0.5);padding-bottom:5px}
.md-preview h3{font-size:15px}
.md-preview p{margin:0 0 12px}
.md-preview hr{height:1px;margin:20px 0;background:#40515a;border:0}
.md-preview blockquote{margin:12px 0;padding:8px 14px;color:#aab9b6;border-left:4px solid var(--teal);background:rgba(73,197,169,0.06);border-radius:0 4px 4px 0}
.md-preview .md-alert{margin:12px 0;padding:8px 14px;border-radius:4px;border-left:4px solid}
.md-preview .md-alert-note{border-color:#5bb4e6;background:rgba(91,180,230,0.1)}
.md-preview .md-alert-tip{border-color:#49c5a9;background:rgba(73,197,169,0.1)}
.md-preview .md-alert-important{border-color:#c89cff;background:rgba(200,156,255,0.1)}
.md-preview .md-alert-warning{border-color:#f4ba5b;background:rgba(244,186,91,0.1)}
.md-preview .md-alert-caution{border-color:#f48670;background:rgba(244,134,112,0.1)}
.md-preview .md-alert-title{font-weight:700;font-size:11px;letter-spacing:0.5px;margin-bottom:4px}
.md-preview ul,.md-preview ol{padding-left:24px;margin:0 0 12px}
.md-preview li{margin:3px 0}
.md-preview .md-inline-code{padding:2px 5px;margin:0 2px;font-size:12px;background:#202c35;border:1px solid #40515a;border-radius:3px;color:#f4ba5b;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.md-preview .md-code-block{padding:12px 14px;overflow:auto;font-size:12px;line-height:1.45;background:#172029;border:1px solid #40515a;border-radius:4px;margin:12px 0;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.md-preview .md-table-wrap{overflow-x:auto;margin:12px 0}
.md-preview table.md-table{border-collapse:collapse;width:100%;font-size:12px;border:1px solid #40515a}
.md-preview table.md-table th,.md-preview table.md-table td{padding:6px 12px;border:1px solid #40515a}
.md-preview table.md-table th{background:#202c35;font-weight:600;color:#eff4f1}
.md-preview table.md-table tr:nth-child(even){background:rgba(255,255,255,0.02)}
.md-preview a.md-link{color:#5bb4e6;text-decoration:none}
.md-preview a.md-link:hover{text-decoration:underline}
.md-preview .md-checkbox{margin-right:5px;vertical-align:-1px}
</style></head><body><main class="app"><header><span class="folder-mark" aria-hidden="true"></span><h1>Workspace Files</h1><div class="path-bar"><select id="presets" class="preset-select" aria-label="Saved paths"></select><span class="path-prefix-icon" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 13h12a1 1 0 0 0 1-1V5a1 1 0 0 0-1-1H8L6.5 2H2a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1z"/></svg></span><input id="path" class="path" list="path-history" spellcheck="false" aria-label="Absolute path" autocomplete="off"></div><datalist id="path-history"></datalist><button id="open">Open</button><button id="up">Up</button><button id="refresh-head" type="button" title="Refresh">Refresh</button><button id="save" class="save" disabled>Save</button><time id="clock" class="clock" title="Current browser time"></time></header><section id="layout" class="layout"><aside class="browser"><div id="directory" class="crumb"></div><div id="entries"></div></aside><div id="splitter" class="splitter" role="separator" aria-orientation="vertical" aria-label="Resize file list and preview"></div><section class="editor"><div class="filebar"><button id="refresh" type="button" title="Refresh directory/commits and content">Refresh</button><button id="copy" class="copy" type="button" aria-label="Copy file contents" title="Copy file contents" disabled></button><button id="download" type="button" title="Download selected patch, Markdown file, or directory" disabled>Download</button><button id="md-preview-btn" class="commit-count md-toggle" type="button" title="Toggle Markdown Preview" hidden>Preview</button><select id="git-branch" class="commit-count branch-select" aria-label="Git branch" title="Switch git branch" hidden></select><select id="commit-count" class="commit-count" aria-label="Number of commits to show" title="Number of commits to show" hidden><option value="1">1 commit</option><option value="2">2 commits</option><option value="3">3 commits</option><option value="4">4 commits</option><option value="5">5 commits</option><option value="6">6 commits</option><option value="7">7 commits</option><option value="8">8 commits</option><option value="9">9 commits</option><option value="10">10 commits</option><option value="11">11 commits</option><option value="12">12 commits</option><option value="13">13 commits</option><option value="14">14 commits</option><option value="15">15 commits</option><option value="16">16 commits</option><option value="17">17 commits</option><option value="18">18 commits</option><option value="19">19 commits</option><option value="20">20 commits</option></select><select id="commit-view" class="commit-count" aria-label="Commit display mode" title="Commit display mode" hidden><option value="1">Log + patch</option><option value="stat">Log + stat</option><option value="0">Log only</option></select><span id="file">Choose a text file</span><span id="status" class="status"></span></div><div class="content-shell"><pre id="line-numbers" class="line-numbers" aria-hidden="true">1</pre><div class="code-layer"><pre id="highlight" class="highlight" aria-hidden="true"></pre><textarea id="content" spellcheck="false" wrap="off" placeholder="Select a UTF-8 text file to view or edit." disabled></textarea></div></div><div id="md-preview-body" class="md-preview" hidden></div></section></section></main>
<script>
const base='/shell_file',layout=document.querySelector('#layout'),splitter=document.querySelector('#splitter'),pathInput=document.querySelector('#path'),pathDatalist=document.querySelector('#path-history'),presets=document.querySelector('#presets'),entries=document.querySelector('#entries'),directory=document.querySelector('#directory'),content=document.querySelector('#content'),lineNumbers=document.querySelector('#line-numbers'),file=document.querySelector('#file'),status=document.querySelector('#status'),save=document.querySelector('#save'),copy=document.querySelector('#copy'),downloadButton=document.querySelector('#download'),mdPreviewBtn=document.querySelector('#md-preview-btn'),mdPreviewBody=document.querySelector('#md-preview-body'),contentShell=document.querySelector('.content-shell'),gitBranch=document.querySelector('#git-branch'),refreshButton=document.querySelector('#refresh'),refreshHead=document.querySelector('#refresh-head'),clock=document.querySelector('#clock'),commitCount=document.querySelector('#commit-count'),commitView=document.querySelector('#commit-view'),lastDirectoryKey='workspaceFilePortal.lastDirectory',lastGitRootKey='workspaceFilePortal.lastGitRoot',lastViewModeKey='workspaceFilePortal.lastViewMode',lastFileKey='workspaceFilePortal.lastFile',commitCountKey='workspaceFilePortal.commitCount',commitViewKey='workspaceFilePortal.commitView',sidebarWidthKey='workspaceFilePortal.sidebarWidth',lastMdPreviewKey='workspaceFilePortal.mdPreview',pathHistoryKey='workspaceFilePortal.pathHistory',branchHistoryKey='workspaceFilePortal.branchHistory';const DEFAULT_BRANCH_SEEDS={"/home/ubuntu/workspace/linux/02-linux-stable":{"sched-ext-idle-cand005":{last:1791865020000},"sched-ext-idle-cand004-v3":{last:1791789885000},"master":{last:1791864250000}},"/home/ubuntu/workspace/linux/04-linux-stable":{"sched/loadavg-calc-load-idx-once-20261013":{last:1791864276000},"sched/cpufreq-schedutil-rate-limit-v3":{last:1791860696000},"sched/cpufreq-schedutil-rate-limit-v2":{last:1791854670000},"master":{last:1791776123000}}};function loadBranchHistory(root){try{const raw=localStorage.getItem(branchHistoryKey);if(raw){const all=JSON.parse(raw);if(all&&all[root])return all[root]}}catch(e){}return DEFAULT_BRANCH_SEEDS[root]||{}}function saveBranchHistory(root,hist){try{let all={};const raw=localStorage.getItem(branchHistoryKey);if(raw)all=JSON.parse(raw)||{};all[root]=hist;localStorage.setItem(branchHistoryKey,JSON.stringify(all))}catch(e){}}function recordBranchHistory(root,branch){if(!root||!branch)return;const hist={...(loadBranchHistory(root)||{})},now=Date.now();hist[branch]={last:now};const sorted=Object.entries(hist).sort((a,b)=>(b[1].last||0)-(a[1].last||0)).slice(0,50);const pruned={};for(const [k,v] of sorted)pruned[k]=v;saveBranchHistory(root,pruned)}function getTopBranchForRoot(root,availableBranches=[]){const hist=loadBranchHistory(root)||{};if(availableBranches&&availableBranches.length){let bestBranch=null,bestTime=-1;for(const item of availableBranches){const gitTime=(item.committerDate||0)*1000,portalTime=hist[item.name]?.last||0,effTime=Math.max(gitTime,portalTime);if(effTime>bestTime){bestTime=effTime;bestBranch=item.name}}if(bestBranch)return bestBranch}const sorted=Object.entries(hist).sort((a,b)=>(b[1].last||0)-(a[1].last||0));return sorted.length?sorted[0][0]:null}function renderBranchSelect(branches,currentBranch,root){gitBranch.replaceChildren();if(!branches||!branches.length)return;const branchHist=loadBranchHistory(root)||{},now=Date.now();const ranked=branches.map(item=>{const gitTime=(item.committerDate||0)*1000,portalTime=branchHist[item.name]?.last||0,effTime=Math.max(gitTime,portalTime),isPortalRecent=portalTime>gitTime;return{item,effTime,gitTime,portalTime,isPortalRecent}});ranked.sort((a,b)=>b.effTime-a.effTime);const recentCutoff=now-7*86400*1000,recentGroup=document.createElement('optgroup');recentGroup.label='★ 最近活跃分支 (按时间排序)';const olderGroup=document.createElement('optgroup');olderGroup.label='更多历史分支';ranked.forEach(({item,effTime,isPortalRecent,portalTime,gitTime},idx)=>{const option=document.createElement('option');option.value=item.name;let timeLabel='';if(isPortalRecent&&portalTime>0){timeLabel=formatRelativeTime(portalTime)+'使用'}else if(gitTime>0){timeLabel=formatRelativeTime(gitTime)+'提交'}option.textContent=item.name+(timeLabel?'  ['+timeLabel+']':'');if(idx<8||effTime>=recentCutoff){recentGroup.append(option)}else{olderGroup.append(option)}});if(recentGroup.children.length>0)gitBranch.append(recentGroup);if(olderGroup.children.length>0)gitBranch.append(olderGroup);if(currentBranch&&!Array.from(gitBranch.options).some(o=>o.value===currentBranch)){const option=document.createElement('option');option.value=currentBranch;option.textContent=currentBranch;gitBranch.prepend(option)}gitBranch.value=currentBranch||''}const DEFAULT_HISTORY_SEEDS={"/home/ubuntu/workspace/linux/02-linux-stable":{count:131,last:1791863038000},"/home/ubuntu/workspace/linux/04-linux-stable":{count:45,last:1791854671000},"/home/ubuntu/workspace/linux/audits/cpufreq-schedutil-rate-limit-race/mail/2026-10-13/cpufreq-schedutil-v3-concise/0001-cpufreq-schedutil-Serialize-start-against-rate-limit.patch":{count:31,last:1791863149000},"/home/ubuntu/workspace/linux/audits/cpufreq-schedutil-rate-limit-race/mail/2026-10-13/cpufreq-schedutil-v3-concise":{count:30,last:1791862533000},"/tmp":{count:30,last:1791861067000},"/home/ubuntu/workspace/linux/03-linux-stable":{count:33,last:1791812494000},"/home/ubuntu/workspace/linux/04-linux-stable/mail/2026-10-12/autogroup-nice-rate-limit-v2":{count:9,last:1791855805000},"/home/ubuntu/workspace/linux/04-linux-stable/mail/2026-10-12/autogroup-nice-rate-limit-v2/0001-sched-autogroup-Make-nice-rate-limit-updates-atomic.patch":{count:8,last:1791855805000},"/home/ubuntu/workspace/linux/03-linux-stable/mail/2026-10-10/r01-re-tejun-dsq-uaf-fixture.txt":{count:6,last:1791786231000},"/home/ubuntu/workspace/linux/02-linux-stable/mail/2026-10-12-cand004-v1-final6-package":{count:5,last:1791791343000},"/home/ubuntu/workspace/linux/02-linux-stable/mail/2026-10-12-cand004-v1-final6-package/0000-cover-letter.patch":{count:5,last:1791787129000},"/home/ubuntu/workspace/linux/02-linux-stable/mail/2026-10-12-cand004-v1-final6-package/0002-selftests-sched_ext-Cover-strict-core-CPU-selection-.patch":{count:5,last:1791791344000},"/home/ubuntu/workspace/linux/03-linux-stable/mail/2026-10-10":{count:4,last:1791786231000},"/home/ubuntu/workspace/linux/02-linux-stable/mail/2026-10-12/r02-reply-andrea-001-v3.txt":{count:7,last:1791738454000},"/home/ubuntu/workspace/linux/02-linux-stable/mail/2026-10-12-cand001-v3-full-package/0001-sched_ext-idle-Respect-task-affinity-in-restricted-CPU-selection.patch":{count:4,last:1791806304000},"/home/ubuntu/workspace/linux/02-linux-stable/mail/2026-10-12-cand001-v3-full-package":{count:4,last:1791806303000},"/home/ubuntu/workspace/linux/04-linux-stable/mail/2026-10-12/r01-autogroup-cmpxchg-review-reply.txt":{count:4,last:1791791933000},"/home/ubuntu/workspace/linux/04-linux-stable/mail/2026-10-12":{count:4,last:1791791933000},"/home/ubuntu/workspace/linux/02-linux-stable/mail/2026-10-12-cand004-v1-final6-package/0001-sched_ext-idle-Honor-SCX_PICK_IDLE_CORE-in-CPU-selec.patch":{count:3,last:1791787299000},"/home/ubuntu/workspace/linux/audits/cpufreq-schedutil-rate-limit-race/mail/2026-10-13/r01-rafael-review-reply.txt":{count:2,last:1791855967000},"/home/ubuntu/workspace/linux/audits/cpufreq-schedutil-rate-limit-race/mail/2026-10-13":{count:2,last:1791855967000},"/home/ubuntu/workspace/linux/audits/cpufreq-schedutil-rate-limit-race/mail/2026-10-13/cpufreq-schedutil-v3":{count:2,last:1791856217000},"/home/ubuntu/workspace/linux/audits/cpufreq-schedutil-rate-limit-race":{count:2,last:1791863314000}};function getFrecencyScore(item,now){const deltaHours=Math.max(0,(now-(item.last||now))/3600000);let weight=5;if(deltaHours<4)weight=100;else if(deltaHours<24)weight=70;else if(deltaHours<72)weight=40;else if(deltaHours<168)weight=20;return(item.count||1)*weight}function formatRelativeTime(ts){if(!ts)return'';const diffSec=Math.max(0,Math.floor((Date.now()-ts)/1000));if(diffSec<60)return'刚刚';const diffMin=Math.floor(diffSec/60);if(diffMin<60)return diffMin+'分钟前';const diffHours=Math.floor(diffMin/60);if(diffHours<24)return diffHours+'小时前';const diffDays=Math.floor(diffHours/24);if(diffDays<30)return diffDays+'天前';return Math.floor(diffDays/30)+'月前'}function loadPathHistory(){try{const raw=localStorage.getItem(pathHistoryKey);if(raw)return JSON.parse(raw)}catch(e){}return null}function savePathHistory(hist){try{localStorage.setItem(pathHistoryKey,JSON.stringify(hist))}catch(e){}}function recordPathHistory(rawPath){const clean=(rawPath||'').trim();if(!clean||clean.startsWith('__git')||clean.includes('/fake.patch/'))return;const hist=loadPathHistory()||{...DEFAULT_HISTORY_SEEDS},now=Date.now(),entry=hist[clean]||{count:0,last:now};entry.count=(entry.count||0)+1;entry.last=now;hist[clean]=entry;const sorted=Object.entries(hist).filter(([k])=>k&&!k.includes('/fake.patch/')).sort((a,b)=>getFrecencyScore(b[1],now)-getFrecencyScore(a[1],now)).slice(0,50);const pruned={};for(const [k,v] of sorted)pruned[k]=v;savePathHistory(pruned);renderPathDatalist(pruned)}function renderPathDatalist(history){if(!pathDatalist)return;const hist=history||loadPathHistory()||DEFAULT_HISTORY_SEEDS,now=Date.now();const sorted=Object.entries(hist).filter(([k])=>k&&!k.includes('/fake.patch/')).sort((a,b)=>getFrecencyScore(b[1],now)-getFrecencyScore(a[1],now)).slice(0,30);pathDatalist.replaceChildren();for(const [p,item] of sorted){const opt=document.createElement('option');opt.value=p;opt.label=formatRelativeTime(item.last)+(item.count>1?' · '+item.count+'次':'');pathDatalist.append(opt)}}function lastFileKeyFor(dir){return 'workspaceFilePortal.lastFile:'+(dir||'')}function gitBranchKey(root){return 'workspaceFilePortal.gitBranch:'+(root||'')}function presetForPath(path){if(!path)return'';const matches=Array.from(presets.options).map(option=>option.value).filter(value=>value.startsWith('/')&&(path===value||path.startsWith(value+'/'))).sort((left,right)=>right.length-left.length);return matches[0]||''}function lastDirKeyForPreset(preset){return 'workspaceFilePortal.lastDir:'+(preset||'')}function lastFileKeyForPreset(preset){return 'workspaceFilePortal.lastFile:'+(preset||'')}function syncPresetForPath(path){presets.value=presetForPath(path)}let current=null,currentDirectory=null,currentParent=null,viewMode='files',gitRoot=null,gitCommits=[],gitStartIndex=0,gitMasterIndex=-1,upstreamCollapsed=true,isMdPreview=localStorage.getItem(lastMdPreviewKey)==='1';
function isMarkdownPath(path){return /\.(md|markdown)$/i.test(path||'')}
function renderMarkdown(src){const lines=src.split(/\r?\n/),out=[];let i=0;function inlineFormat(text){let s=escapeHtml(text);const codes=[];s=s.replace(/`([^`]+)`/g,(_,c)=>{codes.push(c);return `\x00C${codes.length-1}\x00`});s=s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g,'<img src="$2" alt="$1" class="md-img" />');s=s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer" class="md-link">$1</a>');s=s.replace(/\*\*\*(.*?)\*\*\*/g,'<strong><em>$1</em></strong>');s=s.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');s=s.replace(/(^|[^\w])__([^_]+)__(?=[^\w]|$)/g,'$1<strong>$2</strong>');s=s.replace(/(^|[^\*])\*([^\*]+)\*(?=[^\*]|$)/g,'$1<em>$2</em>');s=s.replace(/(^|[^\w])_([^_]+)_(?=[^\w]|$)/g,'$1<em>$2</em>');s=s.replace(/~~(.*?)~~/g,'<del>$1</del>');s=s.replace(/\x00C(\d+)\x00/g,(_,idx)=>`<code class="md-inline-code">${codes[idx]}</code>`);return s}while(i<lines.length){const line=lines[i];if(/^\s*```/.test(line)){const m=line.match(/^\s*```(\w*)/),lang=m?m[1]:'',codeLines=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i])){codeLines.push(escapeHtml(lines[i]));i++}i++;out.push(`<pre class="md-code-block"><code class="language-${lang}">${codeLines.join('\n')}</code></pre>`);continue}if(/^\s*([-*_]\s*){3,}$/.test(line)){out.push('<hr class="md-hr" />');i++;continue}const hm=line.match(/^(#{1,6})\s+(.*)$/);if(hm){const lvl=hm[1].length;out.push(`<h${lvl} class="md-h${lvl}">${inlineFormat(hm[2])}</h${lvl}>`);i++;continue}if(/^\s*>/.test(line)){const qLines=[];let alertType=null;while(i<lines.length&&/^\s*>/.test(lines[i])){let c=lines[i].replace(/^\s*>\s?/,'');const am=c.match(/^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]/i);if(am&&qLines.length===0){alertType=am[1].toUpperCase();c=c.replace(/^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*/i,'')}if(c)qLines.push(inlineFormat(c));i++}if(alertType){out.push(`<div class="md-alert md-alert-${alertType.toLowerCase()}"><div class="md-alert-title">${alertType}</div><div class="md-alert-body">${qLines.join('<br/>')}</div></div>`)}else{out.push(`<blockquote class="md-quote">${qLines.join('<br/>')}</blockquote>`)}continue}if(line.includes('|')&&i+1<lines.length&&/^\s*\|?[\s:-]+[-|:\s]+/.test(lines[i+1])){const parseRow=row=>row.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim());const hCols=parseRow(line),aCols=parseRow(lines[i+1]),aligns=aCols.map(c=>/^:-+:$/.test(c)?'center':/^-+:$/.test(c)?'right':'left');i+=2;const bRows=[];while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()!==''){bRows.push(parseRow(lines[i]));i++}let tbl='<div class="md-table-wrap"><table class="md-table"><thead><tr>';hCols.forEach((col,idx)=>{const al=aligns[idx]?` style="text-align:${aligns[idx]}"`:'';tbl+=`<th${al}>${inlineFormat(col)}</th>`});tbl+='</tr></thead><tbody>';bRows.forEach(row=>{tbl+='<tr>';hCols.forEach((_,idx)=>{const col=row[idx]||'',al=aligns[idx]?` style="text-align:${aligns[idx]}"`:'';tbl+=`<td${al}>${inlineFormat(col)}</td>`});tbl+='</tr>'});tbl+='</tbody></table></div>';out.push(tbl);continue}if(/^\s*([-*+]|\d+\.)\s+/.test(line)){const isOrd=/^\s*\d+\.\s+/.test(line),tag=isOrd?'ol':'ul';let listHtml=`<${tag} class="md-list">`;while(i<lines.length&&/^\s*([-*+]|\d+\.)\s+/.test(lines[i])){let itemText=lines[i].replace(/^\s*([-*+]|\d+\.)\s+/,'');const tm=itemText.match(/^\[([ xX])\]\s+(.*)$/);if(tm){const chk=tm[1].toLowerCase()==='x'?'checked':'';itemText=`<input type="checkbox" disabled ${chk} class="md-checkbox" /> ${inlineFormat(tm[2])}`}else{itemText=inlineFormat(itemText)}listHtml+=`<li>${itemText}</li>`;i++}listHtml+=`</${tag}>`;out.push(listHtml);continue}if(/^ {4}/.test(line)){const cLines=[];while(i<lines.length&&(/^ {4}/.test(lines[i])||lines[i].trim()==='')){cLines.push(escapeHtml(lines[i].slice(4)));i++}out.push(`<pre class="md-code-block"><code>${cLines.join('\n')}</code></pre>`);continue}if(line.trim()===''){i++;continue}const pLines=[];while(i<lines.length&&lines[i].trim()!==''&&!/^(#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+|```|\||([-*_]\s*){3,}$| {4})/.test(lines[i])){pLines.push(inlineFormat(lines[i]));i++}out.push(`<p class="md-p">${pLines.join('<br/>')}</p>`)}return out.join('\n')}
function updatePreviewState(){const isMd=isMarkdownPath(current?.path);if(!isMd||viewMode==='git'){mdPreviewBtn.hidden=true;contentShell.hidden=false;mdPreviewBody.hidden=true;return}mdPreviewBtn.hidden=false;if(isMdPreview){mdPreviewBtn.textContent='Edit';mdPreviewBtn.classList.add('active');mdPreviewBtn.title='Switch to Editor (切回编辑模式)';contentShell.hidden=true;mdPreviewBody.hidden=false;mdPreviewBody.innerHTML=renderMarkdown(content.value);save.disabled=true}else{mdPreviewBtn.textContent='Preview';mdPreviewBtn.classList.remove('active');mdPreviewBtn.title='Toggle Markdown Preview (开启预览)';contentShell.hidden=false;mdPreviewBody.hidden=true;save.disabled=!current?.writable}}
function logPortal(level,tag,...args){const time=new Date().toLocaleTimeString('en-GB',{hour12:false}),prefix='[Portal '+time+'] ['+tag+']';if(level==='error')console.error(prefix,...args);else if(level==='warn')console.warn(prefix,...args);else console.log(prefix,...args)}
function setStatus(message='',kind=''){status.textContent=message;status.className='status '+kind}async function request(url,options){const response=await fetch(url,options);const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data}function fmt(size){return size<1024?size+' B':(size/1024).toFixed(1)+' KiB'}function dateTime(date){return String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0')+' '+date.toLocaleTimeString('en-GB',{hour12:false})}function formatTime(value){return dateTime(new Date(Number(value)/1000000))}function updateClock(){const now=new Date();clock.textContent=dateTime(now);clock.title=now.toLocaleString('sv-SE',{hour12:false})}function updateLineNumbers(){lineNumbers.textContent=Array.from({length:content.value.split('\n').length},(_,index)=>index+1).join('\n');lineNumbers.scrollTop=content.scrollTop}function setSidebarWidth(width,persist=false){if(window.innerWidth<=760)return;const bounds=layout.getBoundingClientRect(),minimum=250,maximum=Math.max(minimum,bounds.width-366),next=Math.round(Math.max(minimum,Math.min(width,maximum)));layout.style.setProperty('--sidebar-width',next+'px');if(persist)localStorage.setItem(sidebarWidthKey,String(next))}splitter.onpointerdown=event=>{if(window.innerWidth<=760)return;splitter.setPointerCapture(event.pointerId);document.body.classList.add('resizing');setSidebarWidth(event.clientX-layout.getBoundingClientRect().left)};splitter.onpointermove=event=>{if(splitter.hasPointerCapture(event.pointerId))setSidebarWidth(event.clientX-layout.getBoundingClientRect().left)};function stopResizing(event){if(splitter.hasPointerCapture(event.pointerId))splitter.releasePointerCapture(event.pointerId);document.body.classList.remove('resizing');const value=parseInt(layout.style.getPropertyValue('--sidebar-width'),10);if(value)setSidebarWidth(value,true)}splitter.onpointerup=stopResizing;splitter.onpointercancel=stopResizing;window.addEventListener('resize',()=>{const value=parseInt(layout.style.getPropertyValue('--sidebar-width'),10);if(value)setSidebarWidth(value,true)})
function escapeHtml(value){return value.replace(/[&<>]/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[character]))}function token(className,value){return '<span class="'+className+'">'+escapeHtml(value)+'</span>'}function highlightDiff(value){return value.split('\n').map(line=>{if(/^commit\b/.test(line))return token('tok-git-head',line);if(/^(Author|Commit|Date):/.test(line))return token('tok-log',line);if(/^diff --git|^index\b/.test(line))return token('tok-diff-meta',line);if(/^@@/.test(line))return token('tok-diff-hunk',line);if(/^(\+\+\+|---)/.test(line))return token('tok-diff-meta',line);if(/^\+/.test(line))return token('tok-diff-add',line);if(/^-/.test(line))return token('tok-diff-remove',line);if(/\b\d+\s+files?\s+changed\b/.test(line))return token('tok-diff-meta',line);return escapeHtml(line)}).join('\n')}function highlightC(value){const pattern=/\/\/.*|\/\*.*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|^\s*#\s*\w+|\b(?:auto|break|case|char|const|continue|default|do|double|else|enum|extern|float|for|goto|if|inline|int|long|register|return|short|signed|sizeof|static|struct|switch|typedef|union|unsigned|void|volatile|while)\b|\b(?:0x[\da-fA-F]+|\d+(?:\.\d+)?)\b/g;return value.split('\n').map(line=>{let output='',position=0;for(const match of line.matchAll(pattern)){const text=match[0];output+=escapeHtml(line.slice(position,match.index));const className=/^\/[/\*]/.test(text)?'tok-comment':/^["']/.test(text)?'tok-string':/^\s*#/.test(text)?'tok-preprocessor':/^\d|^0x/i.test(text)?'tok-number':'tok-keyword';output+=token(className,text);position=match.index+text.length}return output+escapeHtml(line.slice(position))}).join('\n')}function highlightText(value){const pattern=/https?:\/\/[^\s]+|\b(?:ERROR|WARN|WARNING|FATAL|INFO|DEBUG)\b|\b\d+(?:\.\d+)?\b/g;return value.split('\n').map(line=>{if(/^\s*#/.test(line))return token('tok-text-head',line);let output='',position=0;for(const match of line.matchAll(pattern)){const text=match[0];output+=escapeHtml(line.slice(position,match.index));output+=token(/^https?:/.test(text)?'tok-url':/^[A-Z]+$/.test(text)?'tok-alert':'tok-number',text);position=match.index+text.length}return output+escapeHtml(line.slice(position))}).join('\n')}function updateHighlight(){const highlight=document.querySelector('#highlight'),path=current?.path||'',rawVal=content.value;let html=viewMode==='git'||/\.(patch|diff)$/i.test(path)?highlightDiff(rawVal):/\.(c|cc|cpp|h|hpp)$/i.test(path)?highlightC(rawVal):highlightText(rawVal);if(rawVal.endsWith('\n'))html+=' ';highlight.innerHTML=html;highlight.scrollTop=content.scrollTop;highlight.scrollLeft=content.scrollLeft}function syncContentScroll(){lineNumbers.scrollTop=content.scrollTop;const highlight=document.querySelector('#highlight');highlight.scrollTop=content.scrollTop;highlight.scrollLeft=content.scrollLeft}function updateLineNumbers(){lineNumbers.textContent=Array.from({length:content.value.split('\n').length},(_,index)=>index+1).join('\n');updateHighlight();syncContentScroll()}content.addEventListener('scroll',syncContentScroll)
function fileIconType(name){const extension=name.includes('.')?name.split('.').pop().toLowerCase():'';if(['c','cc','cpp','h','hpp','py','js','ts','java','go','rs','sh','bash','zsh'].includes(extension))return'code';if(['json','yaml','yml','toml','ini','conf','xml'].includes(extension))return'config';if(['md','rst','txt','log'].includes(extension))return'markdown';if(['png','jpg','jpeg','gif','webp','svg'].includes(extension))return'image';if(['zip','gz','xz','bz2','tar','7z'].includes(extension))return'archive';return''}
function isDownloadablePath(path){return /\.(patch|md|txt)$/i.test(path)}
let allowedRoots=['/tmp','/home/ubuntu/workspace','/home/ubuntu/.gemini'];
function isPathAllowed(p){if(!p)return false;return allowedRoots.some(r=>p===r||p.startsWith(r+'/'))}
function gitDirectoryLabel(path,branch){return branch?path+'  |  branch: '+branch:path}
function renderDirectoryCrumb(path,branch,isGit=false){directory.replaceChildren();if(!path)return;if(isGit){const card=document.createElement('div');card.className='crumb-git-card';const topRow=document.createElement('div');topRow.className='crumb-git-card-top';const tag=document.createElement('span');tag.className='crumb-git-repo-tag';tag.textContent='GIT REPO';const repoParts=path.split('/').filter(Boolean);const repoName=repoParts.length?repoParts[repoParts.length-1]:path;const nameEl=document.createElement('span');nameEl.className='crumb-git-repo-name';nameEl.textContent=repoName;nameEl.title=path;const browseBtn=document.createElement('button');browseBtn.type='button';browseBtn.className='crumb-git-browse-btn';browseBtn.textContent='文件 ↗';browseBtn.title='浏览仓库文件 ('+repoName+')';browseBtn.onclick=()=>browse(path);topRow.append(tag,nameEl,browseBtn);const pathEl=document.createElement('div');pathEl.className='crumb-git-card-path';const displayPath=path.startsWith('/home/ubuntu')?'~'+path.slice('/home/ubuntu'.length):path;pathEl.textContent=displayPath;pathEl.title=path;card.append(topRow,pathEl);directory.append(card);return}const wrap=document.createElement('div');wrap.className='crumb-trail';const segments=[];if(path.startsWith('/home/ubuntu/')){segments.push({label:'~',subPath:'/home/ubuntu',allowed:false});const rel=path.slice('/home/ubuntu/'.length);const parts=rel.split('/').filter(Boolean);let acc='/home/ubuntu';parts.forEach((p,idx)=>{acc+='/'+p;segments.push({label:p,subPath:acc,allowed:isPathAllowed(acc),isLast:idx===parts.length-1})})}else if(path==='/home/ubuntu'){segments.push({label:'~',subPath:'/home/ubuntu',allowed:false,isLast:true})}else{const parts=path.split('/').filter(Boolean);let acc='';parts.forEach((p,idx)=>{acc+='/'+p;segments.push({label:(idx===0?'/':'')+p,subPath:acc,allowed:isPathAllowed(acc),isLast:idx===parts.length-1})})}segments.forEach((seg,idx)=>{if(idx>0){const sep=document.createElement('span');sep.className='crumb-sep';sep.textContent='/';wrap.append(sep)}const el=document.createElement('span');el.className='crumb-segment'+(seg.isLast?' active':(!seg.allowed?' disabled':''));el.textContent=seg.label;el.title=seg.subPath;if(seg.allowed&&!seg.isLast){el.onclick=()=>browse(seg.subPath)}wrap.append(el)});directory.append(wrap);if(branch){const branchLine=document.createElement('div');branchLine.className='crumb-git-line';const badge=document.createElement('span');badge.className='crumb-git-badge';badge.title='进入当前分支 Git 提交视图 ('+branch+')';badge.onclick=()=>showGit(path,branch);const icon=document.createElement('span');icon.className='crumb-git-icon';icon.textContent='⎇';const branchName=document.createElement('span');branchName.className='crumb-git-name';branchName.textContent=branch;const jumpText=document.createElement('span');jumpText.className='crumb-git-arrow';jumpText.textContent='commits ↗';badge.append(icon,branchName,jumpText);branchLine.append(badge);directory.append(branchLine)}}
function scrollToActiveEntry(activeEl,smooth=true){if(!activeEl)return;const browser=document.querySelector('.browser');if(!browser)return;requestAnimationFrame(()=>{const rect=activeEl.getBoundingClientRect(),bRect=browser.getBoundingClientRect(),margin=28;if(rect.top>=bRect.top+margin&&rect.bottom<=bRect.bottom-margin)return;try{activeEl.scrollIntoView({behavior:smooth?'smooth':'auto',block:'center',inline:'nearest'})}catch(_){const target=browser.scrollTop+(rect.top-bRect.top)-(bRect.height/2)+(rect.height/2);browser.scrollTo({top:Math.max(0,target),behavior:smooth?'smooth':'auto'})}})}
function highlightActiveFile(path,shouldScroll=true){let activeBtn=null;entries.querySelectorAll('.entry').forEach(btn=>{if(btn.dataset.path===path){btn.classList.add('active');activeBtn=btn}else btn.classList.remove('active')});if(shouldScroll&&activeBtn)scrollToActiveEntry(activeBtn,true)}
function highlightActiveCommitRange(start,count){let firstActive=null;entries.querySelectorAll('.entry').forEach((btn,idx)=>{if(idx>=start&&idx<start+count){btn.classList.add('active');if(!firstActive)firstActive=btn}else btn.classList.remove('active')});if(firstActive)scrollToActiveEntry(firstActive,true)}
function setDownloadTarget(path,kind=''){downloadButton.disabled=!path||!kind;downloadButton.dataset.path=path||'';downloadButton.dataset.kind=kind;downloadButton.textContent=kind==='directory'?'Download folder':'Download file';downloadButton.title=kind==='directory'?'Download all .patch, .md, and .txt files in this directory':'Download this .patch, .md, or .txt file'}
function setGitDownloadTarget(selected){if(!selected||!selected.length){downloadButton.disabled=true;downloadButton.dataset.path='';downloadButton.dataset.kind='';downloadButton.textContent='Download';downloadButton.title='No commit patch to download';return}downloadButton.disabled=false;downloadButton.dataset.path='__git_patch__';downloadButton.dataset.kind='git-patch';downloadButton.textContent=selected.length===1?'Download patch':'Download patches ('+selected.length+')';downloadButton.title='Download currently viewed patch as a .patch file'}
async function downloadSelected(){const kind=downloadButton.dataset.kind;if(kind==='git-patch'){const patchContent=content.value;if(!patchContent){setStatus('No patch content to download','error');return}const blob=new Blob([patchContent],{type:'text/x-diff;charset=utf-8'}),objectUrl=URL.createObjectURL(blob),anchor=document.createElement('a');let name='commit.patch';if(gitStartIndex>=0&&gitCommits[gitStartIndex]){const c=gitCommits[gitStartIndex],count=commitCount.value==='branch'?(gitMasterIndex>0?Math.max(1,gitMasterIndex-gitStartIndex):1):(Number(commitCount.value)||1);if(count===1){const cleanSubj=(c.subject||'').toLowerCase().replace(/[^a-z0-9_\-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,35);name=c.shortHash+(cleanSubj?'-'+cleanSubj:'')+'.patch'}else{name=(gitBranch.value||'branch')+'-'+count+'-commits.patch'}}anchor.href=objectUrl;anchor.download=name;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);setStatus('Download started: '+name,'ok');logPortal('info','DOWNLOAD_GIT_PATCH',name);return}const target=downloadButton.dataset.path;if(!target)return;downloadButton.disabled=true;logPortal('info','DOWNLOAD_START',target,downloadButton.dataset.kind);try{const response=await fetch(base+'/api/download?path='+encodeURIComponent(target));if(!response.ok){let data={};try{data=await response.json()}catch(error){}throw new Error(data.error||'Download failed')}const blob=await response.blob(),objectUrl=URL.createObjectURL(blob),anchor=document.createElement('a'),name=target.slice(target.lastIndexOf('/')+1)||'download';anchor.href=objectUrl;anchor.download=downloadButton.dataset.kind==='directory'?name+'.zip':name;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);setStatus('Download started','ok');logPortal('info','DOWNLOAD_SUCCESS',target)}catch(error){logPortal('error','DOWNLOAD_FAILED',target,error.message);setStatus(error.message,'error')}finally{downloadButton.disabled=false}}
async function browse(target,action='browse'){try{logPortal('info',action==='refresh'?'REFRESH_DIR':'BROWSE',target);viewMode='files';localStorage.setItem(lastViewModeKey,viewMode);gitBranch.hidden=true;commitCount.hidden=true;commitView.hidden=true;const prevDir=currentDirectory,data=await request(base+'/api/list?path='+encodeURIComponent(target)+(action?'&action='+encodeURIComponent(action):''));currentDirectory=data.path;currentParent=data.parent;renderDirectoryCrumb(data.path,data.gitBranch,false);syncPresetForPath(data.path);recordPathHistory(data.path);localStorage.setItem(lastDirectoryKey,data.path);const preset=presetForPath(data.path);if(preset){localStorage.setItem(lastDirKeyForPreset(preset),data.path)}if(data.gitRoot)localStorage.setItem(lastGitRootKey,data.gitRoot);if(current&&current.path.slice(0,current.path.lastIndexOf('/'))!==data.path){current=null;content.value='';updateLineNumbers();content.disabled=true;file.textContent='Choose a text file';save.disabled=true;copy.disabled=true;localStorage.removeItem(lastFileKey);if(preset)localStorage.removeItem(lastFileKeyForPreset(preset))}entries.replaceChildren();if(data.parent){const button=document.createElement('button'),icon=document.createElement('span'),name=document.createElement('strong');button.className='entry';icon.className='icon folder';icon.setAttribute('aria-hidden','true');name.textContent='..';button.append(icon,name);button.onclick=()=>browse(data.parent);entries.append(button)}let activeBtnToScroll=null;for(const entry of data.entries){const button=document.createElement('button'),icon=document.createElement('span'),info=document.createElement('span'),name=document.createElement('strong'),meta=document.createElement('span'),size=document.createElement('span'),time=document.createElement('span');button.className='entry';button.dataset.path=entry.path;if(current&&current.path===entry.path){button.classList.add('active');activeBtnToScroll=button}icon.className=entry.directory?'icon folder':'icon file '+fileIconType(entry.name);icon.setAttribute('aria-hidden','true');info.className='entry-info';meta.className='entry-meta';name.textContent=entry.name;size.textContent=entry.directory?'Folder':fmt(entry.size);time.textContent=formatTime(entry.mtimeNs);time.title=new Date(Number(entry.mtimeNs)/1000000).toLocaleString('sv-SE',{hour12:false});meta.append(size,time);info.append(name,meta);button.append(icon,info);button.onclick=()=>{entries.querySelectorAll('.entry').forEach(btn=>btn.classList.remove('active'));button.classList.add('active');entry.directory?browse(entry.path):openPath(entry.path)};entries.append(button)}if(!activeBtnToScroll&&prevDir&&prevDir!==data.path&&prevDir.startsWith(data.path+'/')){const rel=prevDir.slice(data.path.length+(data.path==='/'?0:1)),firstSeg=rel.split('/')[0],childDir=data.path==='/'?('/'+firstSeg):(data.path+'/'+firstSeg),childBtn=Array.from(entries.querySelectorAll('.entry')).find(b=>b.dataset.path===childDir);if(childBtn){childBtn.classList.add('active');activeBtnToScroll=childBtn}}if(!current){pathInput.value=data.path;setDownloadTarget(data.path,'directory');updatePreviewState()}if(activeBtnToScroll){scrollToActiveEntry(activeBtnToScroll,action!=='restore')}setStatus();logPortal('info','DIR_LOADED',data.path,data.entries.length+' items',data.gitRoot?'git='+data.gitRoot:'non-git')}catch(error){logPortal('error','BROWSE_FAILED',target,error.message);setStatus(error.message,'error')}}
async function openFile(target,action='open'){logPortal('info',action==='refresh'?'REFRESH_FILE':(action==='restore'?'RESTORE_FILE':'OPEN_FILE'),target);const data=await request(base+'/api/file?path='+encodeURIComponent(target)+(action?'&action='+encodeURIComponent(action):''));if(data.isDirectory){return browse(data.path)}gitBranch.hidden=true;commitCount.hidden=true;commitView.hidden=true;syncPresetForPath(data.path);current=data;recordPathHistory(data.path);pathInput.value=data.path;file.textContent=data.path+'  '+fmt(data.size);content.value=data.content;updateLineNumbers();content.disabled=false;content.readOnly=!data.writable;save.disabled=!data.writable;copy.disabled=false;setDownloadTarget(isDownloadablePath(data.path)?data.path:'','file');setStatus(data.writable?'':'Read-only','ok');const parentDir=data.path.slice(0,data.path.lastIndexOf('/'))||'/';localStorage.setItem(lastFileKey,data.path);localStorage.setItem(lastDirectoryKey,parentDir);localStorage.setItem(lastFileKeyFor(parentDir),data.path);const preset=presetForPath(data.path);if(preset){localStorage.setItem(lastDirKeyForPreset(preset),parentDir);localStorage.setItem(lastFileKeyForPreset(preset),data.path)}if(currentDirectory!==parentDir){await browse(parentDir)}highlightActiveFile(data.path,true);updatePreviewState();logPortal('info','FILE_LOADED',data.path,fmt(data.size),data.writable?'writable':'read-only')}
async function openPath(target){const clean=(target||'').trim();if(!clean)return;if(clean.endsWith('/')||clean==='.'||clean==='..')return browse(clean);try{await openFile(clean)}catch(error){logPortal('warn','OPEN_PATH_FALLBACK',clean,error.message);if(error.message==='Path is not a regular file.')await browse(clean);else if(error.message==='File exceeds the 2 MiB editing limit.'&&isDownloadablePath(clean)){current=null;pathInput.value=clean;file.textContent=clean+'  (download only)';content.value='';updateLineNumbers();content.disabled=true;content.readOnly=true;save.disabled=true;copy.disabled=true;setDownloadTarget(clean,'file');setStatus('File is larger than 2 MiB; download only','ok');const parentDir=clean.slice(0,clean.lastIndexOf('/'))||'/';if(currentDirectory!==parentDir)await browse(parentDir);highlightActiveFile(clean);updatePreviewState()}else{logPortal('error','OPEN_PATH_FAILED',clean,error.message);setStatus(error.message,'error')}}}
let updateDividerToggle=()=>{};
function syncCommitCountOptions(masterIndex){const prevValue=commitCount.value;commitCount.replaceChildren();if(masterIndex>0){const branchOpt=document.createElement('option');branchOpt.value='branch';branchOpt.textContent='Branch commits ('+masterIndex+')';commitCount.append(branchOpt)}for(let i=1;i<=20;i++){const opt=document.createElement('option');opt.value=String(i);opt.textContent=i===1?'1 commit':(i+' commits');commitCount.append(opt)}const saved=localStorage.getItem(commitCountKey);if(saved==='branch'&&masterIndex>0){commitCount.value='branch'}else if(saved&&Array.from(commitCount.options).some(o=>o.value===saved)){commitCount.value=saved}else if(prevValue&&Array.from(commitCount.options).some(o=>o.value===prevValue)){commitCount.value=prevValue}else if(masterIndex>0){commitCount.value='branch'}else{commitCount.value='1'}}
function renderCommitList(data){entries.replaceChildren();let dividerInserted=false,masterIndex=-1;data.commits.forEach((commit,index)=>{if(commit.isMaster&&masterIndex===-1)masterIndex=index});gitMasterIndex=masterIndex;const upstreamCount=masterIndex>0?data.commits.length-(masterIndex+1):0;const masterCommit=masterIndex>0?data.commits[masterIndex]:null;const masterTitle=masterCommit?(masterCommit.badgeDetail||masterCommit.badge||'Upstream base'):'Upstream base';updateDividerToggle=()=>{};data.commits.forEach((commit,index)=>{if(commit.isMaster&&!dividerInserted&&index>0){dividerInserted=true;const divider=document.createElement('div');divider.className='git-master-divider';divider.title='点击展开或折叠 '+masterTitle+' 之前的历史提交';const tag=document.createElement('span');tag.className='divider-tag';tag.textContent='✦ UPSTREAM BASE'+(masterCommit?.shortHash?' ('+masterCommit.shortHash+')':'');const toggle=document.createElement('span');toggle.className='divider-toggle';updateDividerToggle=function(){if(upstreamCount>0){toggle.innerHTML=upstreamCollapsed?('<span class="divider-arrow">▶</span> 展开更早历史 ('+upstreamCount+')'):('<span class="divider-arrow">▼</span> 折叠更早历史 ('+upstreamCount+')');toggle.title=upstreamCollapsed?('点击展开 '+masterTitle+' 之前的 '+upstreamCount+' 个历史提交'):('点击折叠 '+masterTitle+' 之前的 '+upstreamCount+' 个历史提交')}else{toggle.innerHTML='<span class="divider-arrow">✦</span> Upstream Base';toggle.title=masterTitle+' commit'}};updateDividerToggle();divider.append(tag,toggle);divider.onclick=()=>{upstreamCollapsed=!upstreamCollapsed;entries.querySelectorAll('.entry.upstream-commit').forEach(btn=>{if(upstreamCollapsed)btn.classList.add('collapsed');else btn.classList.remove('collapsed')});updateDividerToggle()};entries.append(divider)}const button=document.createElement('button'),icon=document.createElement('span'),info=document.createElement('span'),name=document.createElement('strong'),meta=document.createElement('div');button.className='entry git-commit-entry';button.dataset.hash=commit.hash;button.dataset.index=index;if(commit.isMaster&&index>0){button.classList.add('master-commit');button.title='Upstream base: '+(commit.badgeDetail||commit.badge||commit.shortHash);const badge=document.createElement('span');badge.className='master-badge';badge.textContent='✦ base';badge.title='Upstream base: '+(commit.badgeDetail||commit.badge||commit.shortHash);name.append(badge)}else if(masterIndex>0&&index>masterIndex){button.classList.add('upstream-commit');if(upstreamCollapsed)button.classList.add('collapsed')}name.append(document.createTextNode(commit.subject));icon.className='icon git';icon.setAttribute('aria-hidden','true');info.className='entry-info git-entry-info';meta.className='git-entry-meta';const hSpan=document.createElement('span');hSpan.className='commit-hash';hSpan.textContent=commit.shortHash;const dSpan=document.createElement('span');dSpan.className='commit-date';dSpan.textContent=commit.date;meta.append(hSpan,dSpan);info.append(name,meta);button.append(icon,info);button.onclick=()=>openCommitRange(index);entries.append(button)})}
async function showGit(target,branch,action='git'){viewMode='git';localStorage.setItem(lastViewModeKey,viewMode);presets.value='__git__';upstreamCollapsed=true;try{const rootTarget=target||localStorage.getItem(lastGitRootKey)||currentDirectory||'';const rememberedBranch=branch||(rootTarget?(localStorage.getItem(gitBranchKey(rootTarget))||getTopBranchForRoot(rootTarget)):null);logPortal('info',action==='refresh'?'REFRESH_GIT':'GIT_VIEW',rootTarget,rememberedBranch||'HEAD');let url=base+'/api/git/commits?path='+encodeURIComponent(rootTarget);if(rememberedBranch)url+='&branch='+encodeURIComponent(rememberedBranch);if(action)url+='&action='+encodeURIComponent(action);const data=await request(url);gitRoot=data.root;gitCommits=data.commits;gitStartIndex=0;gitMasterIndex=-1;currentDirectory=data.root;currentParent=null;recordPathHistory(data.root);localStorage.setItem(lastGitRootKey,data.root);if(data.branch){localStorage.setItem(gitBranchKey(data.root),data.branch);recordBranchHistory(data.root,data.branch)}renderDirectoryCrumb(data.root,data.branch,true);pathInput.value=data.root;renderBranchSelect(data.branches,data.branch,data.root);gitBranch.hidden=false;renderCommitList(data);syncCommitCountOptions(gitMasterIndex);commitCount.hidden=false;commitView.hidden=false;current=null;content.value='';updateLineNumbers();content.disabled=true;content.readOnly=false;save.disabled=true;copy.disabled=true;file.textContent='Select a commit to view its patch';updatePreviewState();logPortal('info','COMMITS_LOADED',data.root,data.branch,data.commits.length+' commits');if(data.commits.length)await openCommitRange(0);else{setGitDownloadTarget([]);setStatus('No commits found on branch '+(data.branch||''),'ok')}}catch(error){logPortal('error','GIT_FAILED',error.message);setStatus(error.message+' Browse a Git repository first.','error')}}
async function openCommitRange(startIndex){try{if(!gitRoot||!gitCommits.length)return;gitStartIndex=startIndex;let count=1;if(commitCount.value==='branch'){count=gitMasterIndex>0?Math.max(1,gitMasterIndex-startIndex):1}else{count=Number(commitCount.value)||1}count=Math.max(1,Math.min(count,gitCommits.length-startIndex));if(gitMasterIndex>0&&startIndex+count>gitMasterIndex&&upstreamCollapsed){upstreamCollapsed=false;entries.querySelectorAll('.entry.upstream-commit').forEach(btn=>btn.classList.remove('collapsed'));updateDividerToggle()}const selected=gitCommits.slice(startIndex,startIndex+count);highlightActiveCommitRange(startIndex,count);setStatus('Loading commit diff...','');logPortal('info','SHOW_COMMIT',gitRoot,'hashes='+selected.map(c=>c.shortHash).join(','),'count='+count,'view='+commitView.value);const params=new URLSearchParams({path:gitRoot,commits:selected.map(commit=>commit.hash).join(','),view:commitView.value,patch:commitView.value});const data=await request(base+'/api/git/show?'+params);current=null;content.value=data.content;updateLineNumbers();content.disabled=false;content.readOnly=true;const modeSuffix=data.mode==='stat'?' + stat':(data.mode==='patch'||data.patch?' + patch':' log');const repoBase=data.path?data.path.split("/").filter(Boolean).pop():"git";file.textContent=repoBase+" @ "+selected.length+" commit"+(selected.length===1?"":"s")+modeSuffix;file.title=data.path+" ("+(gitBranch.value||"")+")";save.disabled=true;copy.disabled=false;setGitDownloadTarget(selected);setStatus('Read-only commit view','ok');highlightActiveCommitRange(startIndex,count)}catch(error){logPortal('error','SHOW_COMMIT_FAILED',error.message);setStatus(error.message,'error')}}
async function refreshCurrent(){logPortal('info','REFRESH_START','mode='+viewMode);refreshButton.disabled=true;if(refreshHead)refreshHead.disabled=true;setStatus('Refreshing...','');try{if(viewMode==='git'){if(!gitRoot)return;const rootTarget=gitRoot,currentBranch=gitBranch.value||localStorage.getItem(gitBranchKey(rootTarget));let url=base+'/api/git/commits?path='+encodeURIComponent(rootTarget)+'&action=refresh';if(currentBranch)url+='&branch='+encodeURIComponent(currentBranch);const data=await request(url);logPortal('info','REFRESH_GIT_COMMITS',data.root,data.branch,data.commits.length+' commits');gitCommits=data.commits;renderDirectoryCrumb(data.root,data.branch,true);renderBranchSelect(data.branches,data.branch,data.root);renderCommitList(data);syncCommitCountOptions(gitMasterIndex);if(data.commits.length){if(gitStartIndex>=data.commits.length)gitStartIndex=0;await openCommitRange(gitStartIndex);setStatus('Refreshed commits','ok')}else{current=null;content.value='';updateLineNumbers();file.textContent='No commits found';setGitDownloadTarget([]);setStatus('No commits found on branch '+(data.branch||''),'ok')}}else{const dir=currentDirectory||(current?current.path.slice(0,current.path.lastIndexOf('/')):null)||pathInput.value;if(!dir)return;const activeFilePath=current?current.path:(localStorage.getItem(lastFileKeyFor(dir))||null);await browse(dir,'refresh');if(activeFilePath){try{await openFile(activeFilePath,'refresh');setStatus('Refreshed','ok')}catch(error){logPortal('warn','REFRESH_FILE_LOST',activeFilePath,error.message);localStorage.removeItem(lastFileKeyFor(dir));localStorage.removeItem(lastFileKey);const preset=presetForPath(dir);if(preset)localStorage.removeItem(lastFileKeyForPreset(preset));current=null;content.value='';updateLineNumbers();content.disabled=true;file.textContent='Choose a text file';save.disabled=true;copy.disabled=true;setDownloadTarget(dir,'directory');setStatus('File no longer accessible: '+error.message,'error')}}else{setStatus('Refreshed','ok')}}logPortal('info','REFRESH_COMPLETED','mode='+viewMode)}catch(error){logPortal('error','REFRESH_FAILED',error.message);setStatus(error.message,'error')}finally{refreshButton.disabled=false;if(refreshHead)refreshHead.disabled=false}}
function markDirty(){if(!current||content.readOnly)return;const entry=Array.from(entries.querySelectorAll('button[data-path]')).find(button=>button.dataset.path===current.path);if(entry){const meta=entry.querySelector('.entry-meta');if(meta)meta.textContent='Modified, not saved'}setStatus('Modified, not saved')}
async function copyContents(){const text=content.value;if(!text){setStatus('Nothing to copy','');return}try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);setStatus('Copied','ok');return}throw new Error('fallback')}catch(_){const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.left='-9999px';ta.style.top='0';document.body.appendChild(ta);ta.focus();ta.select();const ok=document.execCommand('copy');document.body.removeChild(ta);if(ok)setStatus('Copied','ok');else setStatus('Copy failed','error')}}
presets.onchange=async()=>{logPortal('info','PRESET_CHANGE',presets.value);if(presets.value==='__git__'){await showGit(localStorage.getItem(lastGitRootKey)||pathInput.value||currentDirectory)}else if(presets.value){const rememberedFile=localStorage.getItem(lastFileKeyForPreset(presets.value));const rememberedDir=localStorage.getItem(lastDirKeyForPreset(presets.value))||presets.value;if(rememberedFile){try{const parentDir=rememberedFile.slice(0,rememberedFile.lastIndexOf('/'))||'/';await browse(parentDir);await openFile(rememberedFile,'restore')}catch(error){logPortal('warn','RESTORE_PRESET_FILE_FAILED',rememberedFile,error.message);localStorage.removeItem(lastFileKeyForPreset(presets.value));await browse(rememberedDir)}}else{await browse(rememberedDir)}}else{viewMode='files';localStorage.setItem(lastViewModeKey,viewMode);gitBranch.hidden=true;commitCount.hidden=true;commitView.hidden=true;pathInput.value='';pathInput.focus()}};document.querySelector('#open').onclick=()=>viewMode==='git'?showGit((pathInput.value||'').trim(),gitBranch.value):openPath(pathInput.value);document.querySelector('#up').onclick=()=>viewMode==='git'?showGit((pathInput.value||'').trim()||localStorage.getItem(lastGitRootKey)):(currentParent?browse(currentParent):browse(currentDirectory||pathInput.value));if(refreshHead)refreshHead.onclick=refreshCurrent;refreshButton.onclick=refreshCurrent;pathInput.addEventListener('focus',()=>pathInput.select());pathInput.addEventListener('change',()=>{const val=(pathInput.value||'').trim(),hist=loadPathHistory()||DEFAULT_HISTORY_SEEDS;if(val&&hist[val]){viewMode==='git'?showGit(val,gitBranch.value):openPath(val)}});pathInput.addEventListener('keydown',event=>{if(event.key==='Enter')viewMode==='git'?showGit((pathInput.value||'').trim(),gitBranch.value):openPath(pathInput.value)});let highlightTimer=null;content.addEventListener('input',()=>{lineNumbers.textContent=Array.from({length:content.value.split('\n').length},(_,index)=>index+1).join('\n');syncContentScroll();markDirty();if(highlightTimer)clearTimeout(highlightTimer);highlightTimer=setTimeout(updateHighlight,80)});lineNumbers.addEventListener('wheel',event=>{content.scrollTop+=event.deltaY},{passive:true});copy.onclick=copyContents;downloadButton.onclick=downloadSelected;mdPreviewBtn.onclick=()=>{isMdPreview=!isMdPreview;localStorage.setItem(lastMdPreviewKey,isMdPreview?'1':'0');updatePreviewState()};gitBranch.onchange=async()=>{logPortal('info','BRANCH_CHANGE',gitBranch.value);setStatus('Loading branch commits...','');if(gitRoot){recordBranchHistory(gitRoot,gitBranch.value);localStorage.setItem(gitBranchKey(gitRoot),gitBranch.value);await showGit(gitRoot,gitBranch.value)}};commitCount.onchange=()=>{localStorage.setItem(commitCountKey,commitCount.value);if(viewMode==='git')openCommitRange(gitStartIndex)};commitView.onchange=()=>{localStorage.setItem(commitViewKey,commitView.value);if(viewMode==='git')openCommitRange(gitStartIndex)};save.onclick=async()=>{if(!current)return;save.disabled=true;logPortal('info','SAVE_START',current.path,'bytes='+content.value.length);try{const data=await request(base+'/api/file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:current.path,content:content.value,mtimeNs:current.mtimeNs})});current={...current,...data,content:content.value};file.textContent=current.path+'  '+fmt(current.size);await browse(currentDirectory);highlightActiveFile(current.path);setStatus('Saved','ok');logPortal('info','SAVE_SUCCESS',current.path,fmt(data.size))}catch(error){logPortal('error','SAVE_FAILED',current.path,error.message);setStatus(error.message,'error')}finally{save.disabled=!current?.writable}};const savedCommitCount=localStorage.getItem(commitCountKey),savedCommitView=localStorage.getItem(commitViewKey),savedSidebarWidth=parseInt(localStorage.getItem(sidebarWidthKey),10);const parsedCommitCount=parseInt(savedCommitCount,10);if(savedCommitCount==='branch')commitCount.value='branch';else if(parsedCommitCount>=1&&parsedCommitCount<=20)commitCount.value=String(parsedCommitCount);if(['0','1','stat','patch','log'].includes(savedCommitView))commitView.value=savedCommitView;if(savedSidebarWidth)setSidebarWidth(savedSidebarWidth);updateLineNumbers();updateClock();setInterval(updateClock,1000);request(base+'/api/roots').then(async data=>{if(data.roots)allowedRoots=data.roots;renderPathDatalist();logPortal('info','APP_INIT','roots='+data.roots.join(','));data.presets.forEach(item=>{const option=document.createElement('option');option.value=item;let label='Custom path';if(item==='__git__')label='Git commits';else if(item==='/tmp')label='/tmp';else if(item)label=item.split('/').filter(Boolean).slice(-1)[0]||item;option.textContent=label;option.title=item||'Custom path';presets.append(option)});const fallbackRoot=data.roots[0]||'/tmp';if(localStorage.getItem(lastViewModeKey)==='git'){const lastGitPath=localStorage.getItem(lastGitRootKey)||localStorage.getItem(lastDirectoryKey)||fallbackRoot;try{await showGit(lastGitPath)}catch(e){await browse(fallbackRoot)}}else{const lastFile=localStorage.getItem(lastFileKey);const lastPath=localStorage.getItem(lastDirectoryKey)||fallbackRoot;if(lastFile){const parentDir=lastFile.slice(0,lastFile.lastIndexOf('/'))||'/';try{await browse(parentDir);try{await openFile(lastFile,'restore')}catch(error){logPortal('warn','INIT_RESTORE_FAILED',lastFile,error.message);localStorage.removeItem(lastFileKey);const preset=presetForPath(parentDir);if(preset)localStorage.removeItem(lastFileKeyForPreset(preset))}}catch(dirErr){logPortal('warn','INIT_DIR_FAILED',parentDir,dirErr.message);localStorage.removeItem(lastFileKey);localStorage.removeItem(lastDirectoryKey);await browse(fallbackRoot)}}else{try{await browse(lastPath)}catch(dirErr){logPortal('warn','INIT_DIR_FAILED',lastPath,dirErr.message);localStorage.removeItem(lastDirectoryKey);await browse(fallbackRoot)}}}}).catch(error=>{logPortal('error','APP_INIT_FAILED',error.message);setStatus(error.message,'error')});
</script></body></html>'''


if __name__ == "__main__":
    if not ALLOWED_ROOTS:
        raise RuntimeError("FILE_PORTAL_ROOTS must contain at least one path")
    server = ThreadingHTTPServer(("127.0.0.1", 7683), Handler)
    print(f"Serving {', '.join(map(str, ALLOWED_ROOTS))} on 127.0.0.1:7683{BASE_PATH}/", flush=True)
    server.serve_forever()
