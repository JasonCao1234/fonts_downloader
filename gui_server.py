#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ass-font-fetch GUI —— 给 ass_font_fetch.py 套一个本地可视化页面。

启动：
    python gui_server.py
    python gui_server.py --port 8770 --no-browser

它只监听 127.0.0.1，并为每个路径带上一次性 token，避免同机器上别的程序乱调。
页面里的「选择文件夹」可以直接在页面里浏览目录，也可以调起 Windows 原生对话框。
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import ass_font_fetch as core  # noqa: E402  （同目录，复用匹配逻辑）

FROZEN = core.FROZEN
HERE = core.app_dir()          # 可写目录（源码=脚本目录，exe=exe 所在目录）
RES = core.res_dir()           # 只读资源目录（打包后是解包目录）
SCRIPT = SCRIPT_DIR / "ass_font_fetch.py"
HTML = RES / "gui.html"
INDEX = HERE / "acgrip_index.json"
TOKEN = secrets.token_hex(12)
SERVER_VERSION = "2.0"
LOCK = HERE / ".gui.lock"
STARTED = time.time()


def ensure_index_file() -> None:
    """打包版把索引放在 exe 内部，首次运行复制到 exe 旁边（可写、可替换）。"""
    if INDEX.exists():
        return
    bundled = RES / "acgrip_index.json"
    if bundled.exists() and bundled.resolve() != INDEX.resolve():
        try:
            shutil.copyfile(bundled, INDEX)
        except Exception:  # noqa: BLE001
            pass


def cli_prefix() -> list[str]:
    """命令行入口：源码版跑 python 脚本，打包版复用 exe 自己的 --cli 模式。"""
    if FROZEN:
        return [sys.executable, "--cli"]
    return [sys.executable, "-u", str(SCRIPT)]


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in out.stdout.decode("gbk", "replace")
    except Exception:  # noqa: BLE001
        return False


def running_instance() -> dict | None:
    """已有实例的话返回它的信息（含 token），避免多开抢端口。"""
    if not LOCK.exists():
        return None
    try:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    pid, port = int(data.get("pid") or 0), int(data.get("port") or 0)
    if not pid or not port or not _pid_alive(pid):
        LOCK.unlink(missing_ok=True)
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=3) as r:
            info = json.loads(r.read().decode("utf-8"))
        if info.get("pid") == pid:
            data["alive"] = True
            return data
    except Exception:  # noqa: BLE001
        pass
    return None


def write_lock(port: int) -> None:
    LOCK.write_text(
        json.dumps(
            {"pid": os.getpid(), "port": port, "token": TOKEN, "version": SERVER_VERSION, "started": STARTED},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def cleanup_imports(keep: Path | None = None) -> None:
    """清掉之前导入产生的临时目录，只留当前这个。"""
    import glob
    import tempfile

    for path in glob.glob(os.path.join(tempfile.gettempdir(), "ass-font-fetch-import-*")):
        if keep and os.path.abspath(path) == os.path.abspath(str(keep)):
            continue
        shutil.rmtree(path, ignore_errors=True)

_job_lock = threading.Lock()
_job: dict | None = None
_index_cache: dict = {"mtime": -1.0, "info": {}}
_catalog_cache: dict = {"key": "", "mtime": -1.0, "catalog": None}
IDM_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe"),
    Path(r"C:\Program Files\Internet Download Manager\IDMan.exe"),
]


# ------------------------------------------------------------------ 工具


def json_default(o):
    return str(o)


class JobError(Exception):
    pass


def index_info() -> dict:
    """读取索引概况（按 mtime 缓存）。"""
    if not INDEX.exists():
        return {"exists": False}
    mtime = INDEX.stat().st_mtime
    if abs(mtime - _index_cache["mtime"]) < 1e-6:
        return _index_cache["info"]
    info = {"exists": True, "size": INDEX.stat().st_size, "fonts": 0, "scope": "?", "generated": None}
    try:
        with INDEX.open(encoding="utf-8") as f:
            data = json.load(f)
        info["fonts"] = len(data.get("files", []))
        info["scope"] = data.get("scope", "?")
        ts = data.get("timestamp")
        if ts:
            info["generated"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        info["broken"] = True
    _index_cache.update(mtime=mtime, info=info)
    return info


def source_index_path(name: str | None) -> Path:
    src = core.get_source(name)
    return HERE / src.get("index", f"index_{src['name']}.json")


def sources_info() -> list[dict]:
    out = []
    for src in core.load_sources():
        path = HERE / src.get("index", f"index_{src['name']}.json")
        fonts = 0
        if path.exists():
            try:
                fonts = len(json.loads(path.read_text(encoding="utf-8")).get("files", []))
            except Exception:  # noqa: BLE001
                fonts = -1
        out.append({"name": src["name"], "label": src["label"], "indexed": path.exists(), "fonts": fonts})
    return out


def get_catalog(source: str | None = None):
    """站点索引 → 可搜索的目录对象（按文件 mtime 缓存）。"""
    path = source_index_path(source)
    if not path.exists():
        return None
    key = str(path)
    mtime = path.stat().st_mtime
    if _catalog_cache.get("key") == key and _catalog_cache["mtime"] == mtime and _catalog_cache["catalog"] is not None:
        return _catalog_cache["catalog"]
    files = json.loads(path.read_text(encoding="utf-8")).get("files", [])
    catalog = core.SiteCatalog(files)
    _catalog_cache.update(key=key, mtime=mtime, catalog=catalog)
    return catalog


def search_fonts(q: str, limit: int = 30, source: str | None = None) -> list[dict]:
    catalog = get_catalog(source)
    if catalog is None:
        raise JobError("这个字体源还没有索引，先重建一次索引，或换回 acgrip")
    q = (q or "").strip()
    if not q:
        return []
    hits = catalog.match(q, limit=limit, threshold=60)
    results = [
        {"path": p, "file": os.path.basename(p), "score": s, "size": 0} for s, p in hits
    ]
    if not results:  # 退化为纯子串匹配，尽量有结果
        needle = core.normalize(q)
        if len(needle) >= 2:
            for path in catalog.raw:
                if needle in core.normalize(os.path.basename(path)):
                    results.append({"path": path, "file": os.path.basename(path), "score": 55, "size": 0})
                    if len(results) >= limit:
                        break
    results.sort(key=lambda r: (-r["score"], len(r["file"])))
    return results


def idm_status() -> dict:
    for candidate in IDM_CANDIDATES:
        if candidate.exists():
            return {"available": True, "exe": str(candidate)}
    try:
        import winreg

        for hive, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\DownloadManager"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\DownloadManager"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\DownloadManager"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    exe, _ = winreg.QueryValueEx(k, "ExePath")
                if exe and Path(str(exe)).exists():
                    return {"available": True, "exe": str(exe)}
            except OSError:
                continue
    except ImportError:
        pass
    return {"available": False, "exe": None}


def font_url(rel: str, source: str | None = None) -> str:
    return core.source_url(core.source_base(source), rel)


def links_for(paths: list[str], source: str | None = None) -> list[dict]:
    return [
        {"path": p, "file": os.path.basename(p), "url": font_url(p, source)}
        for p in paths
        if p
    ]


def import_subtitles(src: str) -> dict:
    """把字幕包（本地 zip 或下载链接）解开，返回可扫描的目录。"""
    src = (src or "").strip().strip('"')
    if not src:
        raise JobError("请输入字幕 zip 路径或下载链接")
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="ass-font-fetch-import-"))
    if src.lower().startswith(("http://", "https://")):
        try:
            data = core.http_get(src, retries=2, timeout=600)
        except Exception as exc:  # noqa: BLE001
            raise JobError(f"下载失败：{exc}") from exc
        if data[:2] != b"PK":
            head = data[:200].decode("utf-8", "replace").replace("\n", " ")
            raise JobError(f"这个链接返回的不是 zip（可能是网页/需要登录/提取码）。开头内容：{head[:80]}")
        zp = work / "download.zip"
        zp.write_bytes(data)
    else:
        zp = Path(src).expanduser()
        if not zp.is_file():
            raise JobError(f"文件不存在：{src}")
        if zp.suffix.lower() != ".zip":
            raise JobError("只支持 .zip 字幕包（.rar/.7z 请先解压）")
    files = core.extract_subtitles(zp, work / "subs")
    if not files:
        raise JobError("压缩包里没有 .ass / .ssa 字幕")
    cleanup_imports(keep=work)
    return {
        "dir": str(work / "subs"),
        "count": len(files),
        "files": [p.name for p in files[:20]],
    }


def send_to_idm(paths: list[str], out_dir: str, source: str | None = None) -> dict:
    st = idm_status()
    if not st["available"]:
        raise JobError("没找到 IDM（IDMan.exe）。可以先「导出链接列表」，在 IDM 里用「从文本文件导入」。")
    target = Path(out_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    items = []
    for rel in paths:
        url = font_url(rel, source)
        name = os.path.basename(rel)
        subprocess.run(
            [st["exe"], "/n", "/d", url, "/p", str(target), "/f", name],
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        items.append({"file": name, "url": url})
        time.sleep(0.2)
    return {"sent": len(items), "items": items, "dir": str(target)}


def drives() -> list[dict]:
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZAB":
        root = f"{letter}:\\"
        if os.path.exists(root):
            out.append({"name": root, "path": root})
    out.sort(key=lambda d: d["path"])
    return out


def shortcuts() -> list[dict]:
    home = Path(os.path.expanduser("~"))
    cand = [
        ("桌面", home / "Desktop"),
        ("下载", home / "Downloads"),
        ("文档", home / "Documents"),
        ("用户目录", home),
        ("系统字体", Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"),
        ("字体项目", HERE),
    ]
    return [{"name": n, "path": str(p)} for n, p in cand if p.exists()]


def browse(path: str, mode: str) -> dict:
    if not path:
        return {"path": None, "parent": None, "entries": drives(), "shortcuts": shortcuts(), "mode": mode}
    target = Path(path).expanduser()
    if not target.exists() or not target.is_dir():
        raise JobError(f"目录不存在：{path}")
    target = target.resolve()
    entries = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        entries.append({"name": entry.name, "path": entry.path, "type": "dir"})
                    elif mode == "file" and entry.is_file():
                        entries.append({"name": entry.name, "path": entry.path, "type": "file"})
                except OSError:
                    continue
    except PermissionError as exc:
        raise JobError(f"没有权限读取：{target}") from exc
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].casefold()))
    if len(entries) > 4000:
        entries = entries[:4000]
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "entries": entries,
            "shortcuts": shortcuts() if parent is None else [], "mode": mode}


def pick_native_dir(initial: str, title: str) -> str:
    """用独立子进程弹 tkinter 目录对话框，返回选中路径（取消返回空串）。"""
    if FROZEN:
        try:
            out = subprocess.run(
                [sys.executable, "--tk-pick", initial or "", title or "选择文件夹"],
                capture_output=True,
                timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return out.stdout.decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""
    code = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        "d = filedialog.askdirectory(initialdir=sys.argv[1] or None, title=sys.argv[2], mustexist=True)\n"
        "sys.stdout.write(d or '')\n"
    )
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else sys.executable
    try:
        out = subprocess.run(
            [exe, "-c", code, initial or "", title or "选择文件夹"],
            capture_output=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 任务


def build_command(body: dict) -> list[str]:
    ass_dir = (body.get("ass_dir") or "").strip()
    out_dir = (body.get("out_dir") or "").strip()
    if not ass_dir or not Path(ass_dir).is_dir():
        raise JobError("请先选择存在的字幕目录")
    if not out_dir:
        raise JobError("请先选择输出目录")
    font_dirs = [d for d in (body.get("font_dirs") or []) if d and Path(d).is_dir()]
    cmd = cli_prefix() + ["--ass-dir", ass_dir, "--out", out_dir, "--progress-json"]
    for d in font_dirs:
        cmd += ["--font-dir", d]
    scope = body.get("scope") or "lite"
    cmd += ["--scope", scope if scope in ("lite", "full") else "lite"]
    variants = body.get("variants") or "base"
    cmd += ["--variants", variants if variants in ("base", "all") else "base"]
    for flag, key in (
        ("--force", "force"),
        ("--keep-tree", "keep_tree"),
        ("--no-system", "no_system"),
        ("--refresh-index", "refresh_index"),
    ):
        if body.get(key):
            cmd.append(flag)
    if body.get("dry_run"):
        cmd.append("--dry-run")
    cmd.append("--yes")
    aliases = (body.get("aliases") or "").strip()
    if aliases and Path(aliases).is_file():
        cmd += ["--aliases", aliases]
    report = Path(body.get("report_path") or (HERE / ".gui_report.json")).resolve()
    cmd += ["--report", str(report)]
    if (body.get("jobs") or 0):
        cmd += ["--jobs", str(int(body["jobs"]))]
    return cmd


def build_direct_command(body: dict) -> list[str]:
    out_dir = (body.get("out_dir") or "").strip()
    if not out_dir:
        raise JobError("请先选择输出目录")
    paths = [p for p in (body.get("paths") or []) if p]
    if not paths:
        raise JobError("没有选中任何字体")
    cmd = cli_prefix() + ["--out", out_dir, "--progress-json"]
    for p in paths:
        cmd += ["--download", p]
    if body.get("keep_tree"):
        cmd.append("--keep-tree")
    if body.get("force"):
        cmd.append("--force")
    if body.get("source"):
        cmd += ["--source", str(body["source"])]
    report = Path(body.get("report_path") or (HERE / ".gui_report.json")).resolve()
    cmd += ["--report", str(report)]
    return cmd


def _reader(proc: subprocess.Popen, q: queue.Queue) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        q.put(line.rstrip("\r\n"))
    q.put(None)


def _new_progress() -> dict:
    return {"total": 0, "done": 0, "bytes_done": 0, "bytes_total": 0, "files": [], "active": []}


def _progress_entry(prog: dict, file_name: str) -> dict:
    for entry in prog["files"]:
        if entry["file"] == file_name:
            return entry
    entry = {"file": file_name, "name": file_name, "done": 0, "total": 0, "status": "wait", "speed": 0, "ts": 0}
    prog["files"].append(entry)
    return entry


def _recalc(prog: dict) -> None:
    finished = {"downloaded", "exists", "failed", "skipped"}
    prog["done"] = sum(1 for f in prog["files"] if f["status"] in finished)
    prog["bytes_done"] = sum(f.get("done", 0) for f in prog["files"])
    prog["bytes_total"] = sum(f.get("total", 0) for f in prog["files"])
    prog["active"] = [f["file"] for f in prog["files"] if f["status"] == "running"]


def _handle_progress(job: dict, line: str) -> None:
    kind, _, payload = line[2:].partition(" ")
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return
    prog = job.setdefault("progress", _new_progress())
    if kind == "TOTAL":
        prog["total"] = int(data.get("total") or 0)
        return
    if kind == "FILE":
        fname = data.get("file") or data.get("name") or "?"
        entry = _progress_entry(prog, fname)
        entry["name"] = data.get("name") or entry["name"]
        status = data.get("status") or "running"
        entry["status"] = "running" if status == "start" else status
        if data.get("bytes"):
            entry["done"] = int(data["bytes"])
            entry["total"] = int(data["bytes"])
        entry["ts"] = time.time()
    elif kind == "PROGRESS":
        fname = data.get("file") or "?"
        entry = _progress_entry(prog, fname)
        now = time.time()
        prev_done, prev_ts = entry.get("done", 0), entry.get("ts", 0)
        done = int(data.get("done") or 0)
        total = int(data.get("total") or 0)
        if prev_ts and now - prev_ts > 0.05:
            entry["speed"] = max(0.0, (done - prev_done) / (now - prev_ts))
        entry["done"] = done
        entry["total"] = total
        entry["ts"] = now
        if entry["status"] not in ("downloaded", "exists", "failed"):
            entry["status"] = "running"
    _recalc(prog)


def start_job(body: dict) -> dict:
    global _job
    cmd = build_direct_command(body) if body.get("paths") else build_command(body)
    with _job_lock:
        if _job and _job["running"]:
            raise JobError("已有任务在跑，先等它结束或点停止")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            cmd,
            cwd=str(HERE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _job = {
            "id": secrets.token_hex(4),
            "cmd": cmd,
            "mode": "direct" if body.get("paths") else "scan",
            "dry_run": bool(body.get("dry_run")),
            "proc": proc,
            "lines": [],
            "progress": _new_progress(),
            "running": True,
            "exit_code": None,
            "result": None,
            "started": time.time(),
            "finished": None,
            "report": str(body.get("report_path") or (HERE / ".gui_report.json")),
        }
        q: queue.Queue = queue.Queue()
        threading.Thread(target=_reader, args=(proc, q), daemon=True).start()
        threading.Thread(target=_collect, args=(_job, proc, q), daemon=True).start()
        return {"job_id": _job["id"]}


def _collect(job: dict, proc: subprocess.Popen, q: queue.Queue) -> None:
    while True:
        line = q.get()
        if line is None:
            break
        if line.startswith("@@"):
            _handle_progress(job, line)
            continue
        job["lines"].append(line)
        if len(job["lines"]) > 4000:
            del job["lines"][:1000]
    code = proc.wait()
    job["running"] = False
    job["exit_code"] = code
    job["finished"] = time.time()
    report = Path(job["report"])
    if report.exists():
        try:
            job["result"] = json.loads(report.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            job["result"] = None
        finally:
            report.unlink(missing_ok=True)


def job_state(since: int) -> dict:
    job = _job
    if not job:
        return {"running": False, "lines": [], "next": 0, "exit_code": None,
                "result": None, "progress": None}
    lines = job["lines"]
    chunk = lines[since:]
    return {
        "id": job["id"],
        "mode": job.get("mode", "scan"),
        "running": job["running"],
        "dry_run": job["dry_run"],
        "dry_missing": job["dry_run"],
        "exit_code": job["exit_code"],
        "next": len(lines),
        "lines": chunk[-600:],
        "result": job["result"],
        "progress": job.get("progress"),
        "started": job["started"],
        "finished": job["finished"],
    }


def stop_job() -> dict:
    job = _job
    if not job or not job["running"]:
        return {"stopped": False}
    proc = job["proc"]
    try:
        proc.terminate()
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass
    return {"stopped": True}


def open_path(path: str) -> None:
    p = Path(path).expanduser()
    if not p.exists():
        raise JobError(f"路径不存在：{path}")
    if sys.platform == "win32":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = f"ass-font-fetch-gui/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音访问日志
        return

    # -- helpers

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _authed(self) -> bool:
        if self.path.split("?")[0] in ("/", "/favicon.ico", "/api/ping"):
            return True
        return self.headers.get("X-Token", "") == TOKEN

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # -- routes

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                html = (
                    HTML.read_text(encoding="utf-8")
                    .replace("__TOKEN__", TOKEN)
                    .replace("__VERSION__", SERVER_VERSION)
                )
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._send(204, b"")
                return
            if not self._authed():
                self._err("forbidden", 403)
                return
            if path == "/api/ping":
                self._json({"version": SERVER_VERSION, "pid": os.getpid(), "started": STARTED,
                            "features": ["search", "direct", "progress", "idm", "import"]})
                return
            if path == "/api/diag":
                self._json({
                    "version": SERVER_VERSION,
                    "pid": os.getpid(),
                    "python": sys.version.split()[0],
                    "here": str(HERE),
                    "index": index_info(),
                    "idm": idm_status(),
                    "sources": sources_info(),
                    "job_running": bool(_job and _job["running"]),
                    "html_size": HTML.stat().st_size if HTML.exists() else 0,
                    "lock": str(LOCK),
                    "started": STARTED,
                })
                return
            if path == "/api/status":
                self._json({"index": index_info(), "job": job_state(int(qs.get("since", ["0"])[0])),
                            "idm": idm_status(), "sources": sources_info(),
                            "script": str(SCRIPT), "here": str(HERE)})
                return
            if path == "/api/search":
                src = qs.get("source", [None])[0]
                self._json({
                    "query": qs.get("q", [""])[0],
                    "source": src or "acgrip",
                    "results": search_fonts(qs.get("q", [""])[0], int(qs.get("limit", ["40"])[0]), src),
                })
                return
            if path == "/api/link":
                rel = qs.get("path", [""])[0]
                if not rel:
                    self._err("缺少 path")
                    return
                url = font_url(rel, qs.get("source", [None])[0])
                self._send(200, json.dumps({"url": url}).encode("utf-8"))
                return
            if path == "/api/browse":
                self._json(browse(qs.get("path", [""])[0], qs.get("mode", ["dir"])[0]))
                return
            if path == "/api/log":
                self._json(job_state(int(qs.get("since", ["0"])[0])))
                return
            if path == "/api/report":
                job = _job
                if not job or not job.get("result"):
                    self._err("还没有报告", 404)
                    return
                body = json.dumps(job["result"], ensure_ascii=False, indent=2).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8",
                           {"Content-Disposition": "attachment; filename=font_report.json"})
                return
            self._err("not found", 404)
        except JobError as exc:
            self._err(str(exc))
        except Exception as exc:  # noqa: BLE001
            self._err(f"服务器错误：{exc}\n{traceback.format_exc()}", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if not self._authed():
                self._err("forbidden", 403)
                return
            body = self._body()
            if path == "/api/start":
                self._json(start_job(body))
                return
            if path == "/api/stop":
                self._json(stop_job())
                return
            if path == "/api/pick":
                self._json({"path": pick_native_dir(body.get("initial", ""), body.get("title", "选择文件夹"))})
                return
            if path == "/api/open":
                open_path(body.get("path", ""))
                self._json({"ok": True})
                return
            if path == "/api/links":
                self._json({"links": links_for(body.get("paths") or [], body.get("source"))})
                return
            if path == "/api/import":
                self._json(import_subtitles(body.get("src", "")))
                return
            if path == "/api/idm":
                self._json(send_to_idm(body.get("paths") or [], body.get("out_dir", ""), body.get("source")))
                return
            if path == "/api/shutdown":
                self._json({"ok": True})
                threading.Timer(0.3, self.server.shutdown).start()
                return
            self._err("not found", 404)
        except JobError as exc:
            self._err(str(exc))
        except Exception as exc:  # noqa: BLE001
            self._err(f"服务器错误：{exc}", 500)


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 allow_reuse_address=True 会让多个实例同时绑同一个端口，
    # 请求随机落到不同进程（旧版页面/任务丢失），所以这里必须关掉。
    allow_reuse_address = False


def tk_pick(initial: str, title: str) -> int:
    """--tk-pick：在独立进程里弹系统目录选择框（打包版用）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(initialdir=initial or None, title=title or "选择文件夹", mustexist=True)
        sys.stdout.write(chosen or "")
        sys.stdout.flush()
        return 0
    except Exception:  # noqa: BLE001
        return 1


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--cli":
        return core.main(argv[2:])
    if len(argv) > 1 and argv[1] == "--tk-pick":
        return tk_pick(argv[2] if len(argv) > 2 else "", argv[3] if len(argv) > 3 else "")
    import argparse

    parser = argparse.ArgumentParser(description="ass-font-fetch 本地可视化页面")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ASS_FONT_FETCH_PORT", "8770")))
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--force", action="store_true", help="结束已在运行的实例后重新启动")
    args = parser.parse_args(argv[1:])

    missing = []
    if not HTML.exists():
        missing.append(str(HTML))
    if not FROZEN and not SCRIPT.exists():
        missing.append(str(SCRIPT))
    if missing:
        print("缺少必要文件，请把它们放在同一目录：")
        for item in missing:
            print("  " + item)
        return 2

    existing = running_instance()
    if existing:
        if args.force:
            print(f"结束旧实例 PID={existing['pid']} …", flush=True)
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(existing["pid"])],
                               capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.8)
            LOCK.unlink(missing_ok=True)
        else:
            url = f"http://127.0.0.1:{existing['port']}/?t={existing['token']}"
            print("=" * 60)
            print("  已经有一个实例在运行（同一个服务只能开一个）")
            print(f"  {url}")
            print("  直接在浏览器打开上面的地址即可；")
            print("  想强制重启：python gui_server.py --force")
            print("=" * 60, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return 0

    ensure_index_file()
    port = free_port(args.port)
    server = GuiServer(("127.0.0.1", port), Handler)
    write_lock(port)
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    print("=" * 60)
    print("  ass-font-fetch 可视化页面已启动")
    print(f"  {url}")
    print("  按 Ctrl+C 或点页面右上角「关闭」退出")
    print("=" * 60, flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_job()
        server.server_close()
        LOCK.unlink(missing_ok=True)
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
