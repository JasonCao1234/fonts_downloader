#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ass-font-fetch —— 扫描 ASS/SSA 字幕用到的字体，与「系统已安装字体」和「指定字体文件夹」
比对，找出缺失的字体，并从 https://pan.acgrip.com/ 自动下载到指定目录。

仅依赖 Python 标准库（Python 3.9+ 即可），无需 pip 安装任何东西。

示例
----
# 1. 只看缺什么（不下载）
python ass_font_fetch.py --ass-dir "D:\\动漫\\某番\\Subs" --font-dir "E:\\FontPack" --out "E:\\FontPack\\_missing" --dry-run

# 2. 找到缺失字体并从 acgrip 下载
python ass_font_fetch.py --ass-dir "D:\\动漫\\某番\\Subs" --font-dir "E:\\FontPack" --out "E:\\FontPack\\_missing"

# 3. 首次运行会建立站内字体索引（较慢，之后走缓存）；只想用精简包范围的字体
python ass_font_fetch.py --ass-dir . --font-dir E:\\FontPack --out E:\\FontPack\\_missing --scope lite

# 4. 索引太旧 / 想看完整包范围
python ass_font_fetch.py ... --scope full --refresh-index

说明
----
* 字体名比对会忽略大小写、空格、下划线、连字符、"@"（竖排前缀）以及全角/半角差异；
  字形名优先取字体文件内部的 name 表（family / typographic family / full name），
  因此「文件名与显示名不同」的字体也能正确识别。
* 站点索引默认只抓「LPSub Fonts」与「超级字体整合包 XZ/精简包」（覆盖绝大多数常见字幕），
  `--scope full` 抓整站；索引缓存在脚本旁，`--max-age-days` 天内直接复用。
* 下载默认平铺到 `--out`，同名文件自动跳过；`--keep-tree` 保留站点目录层级。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape as html_unescape
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """可写目录：源码运行时是脚本目录，打包成 exe 后是 exe 所在目录。"""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def res_dir() -> Path:
    """只读资源目录：打包后是 PyInstaller 的解包目录（含 gui.html / 索引等）。"""
    if FROZEN and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return app_dir()


HERE = app_dir()
SITE = "https://pan.acgrip.com/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ass-font-fetch/1.0"
FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc"}
ASS_EXTS = {".ass", ".ssa"}
SCOPE_ROOTS = {
    "lite": ["LPSub Fonts", "超级字体整合包 XZ/精简包"],
    "full": [""],
}
INDEX_VERSION = 1

# 字体源：默认只有 acgrip；sources.json 里可以再追加任意 HFS 风格（?dir= 列表）的字体镜像站
DEFAULT_SOURCES = [
    {
        "name": "acgrip",
        "label": "Anime字幕论坛字体站",
        "base": SITE,
        "index": "acgrip_index.json",
    }
]


def load_sources() -> list[dict]:
    for base in (HERE, res_dir()):
        path = base / "sources.json"
        if path.exists():
            break
    else:
        return [dict(s) for s in DEFAULT_SOURCES]
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("sources") if isinstance(data, dict) else data
            out = []
            for item in items or []:
                if not item.get("name") or not item.get("base"):
                    continue
                item.setdefault("label", item["name"])
                item.setdefault("index", f"index_{item['name']}.json")
                item.setdefault("enabled", True)
                out.append(item)
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
    return [dict(s) for s in DEFAULT_SOURCES]


def get_source(name: str | None) -> dict:
    sources = load_sources()
    for s in sources:
        if s["name"] == name:
            return s
    return sources[0]


def source_base(name: str | None) -> str:
    return get_source(name)["base"]


def source_url(base: str, rel_path: str) -> str:
    return base + urllib.parse.quote(rel_path)


# ---------------------------------------------------------------- 找字网 zhaozi
# 找字网不是目录站：GET /search?q=名字 出结果，详情页里是多个
# https://down-xx.zhaozi.org/download/...?filename=xxx.otf 的直链。

def _name_score(query: str, cand: str) -> int:
    nq, nc = normalize(query), normalize(cand)
    if not nq or not nc:
        return 0
    if nq == nc:
        return 100
    if base_key(query) == base_key(cand):
        return 96
    if nq in nc or nc in nq:
        return 85
    if len(nq) < 3:
        return 0
    ratio = difflib.SequenceMatcher(None, nq, nc).ratio()
    return int(60 + (ratio - 0.80) / 0.20 * 24) if ratio >= 0.80 else 0


def zhaozi_search(query: str, base: str) -> list[dict]:
    url = base.rstrip("/") + "/search?q=" + urllib.parse.quote(query)
    html = http_get(url, retries=2, timeout=40).decode("utf-8", "replace")
    results: list[dict] = []
    for match in re.finditer(r'<span class="fontName"><a href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        detail = match.group(1).strip()
        raw = re.sub(r"<[^>]+>", "", match.group(2))
        names = [x.strip() for x in raw.split(",") if x.strip()]
        if not names:
            continue
        best = max(names, key=lambda n: _name_score(query, n))
        score = _name_score(query, best)
        if score < 60:
            continue
        results.append(
            {"path": detail, "file": best, "names": names, "score": score, "origin": "zhaozi"}
        )
    results.sort(key=lambda r: -r["score"])
    return results


def zhaozi_links(detail_url: str) -> list[tuple[str, str]]:
    """详情页 → [(直链, 文件名)]。"""
    html = http_get(detail_url, retries=2, timeout=40).decode("utf-8", "replace")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'href="(https://down-[^"]+?filename=[^"]+)"', html):
        href = html_unescape(match.group(1))
        head, _, tail = href.partition("filename=")
        fname = urllib.parse.unquote(tail).strip()
        if not fname or fname in seen:
            continue
        seen.add(fname)
        links.append((head + "filename=" + urllib.parse.quote(fname), fname))
    return links


def _filename_quality(fname: str) -> float:
    """给站内的候选文件名打分（越小越像正经字体名）。"""
    stem = os.path.splitext(fname)[0].strip()
    bad = 0.0
    if stem.startswith(("_", "-", ".", " ")):
        bad += 3
    bad += sum(0.6 for ch in stem if ch in "_-.") 
    if len(stem) < 4:
        bad += 3
    if re.search(r"bold|italic|oblique", stem, re.I):
        bad += 2
    if re.search(r"[?？]{1,}", stem):
        bad += 4
    return bad


def pick_zhaozi_file(links: list[tuple[str, str]]) -> tuple[str, str] | None:
    if not links:
        return None
    return sorted(links, key=lambda x: (_filename_quality(x[1]), len(x[1])))[0]


def emit(kind: str, payload: dict, enabled: bool) -> None:
    """给 GUI 用的机器可读进度行。"""
    if not enabled:
        return
    print("@@" + kind + " " + json.dumps(payload, ensure_ascii=False), flush=True)

# ---------------------------------------------------------------- 基础工具


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - 老环境
            pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def info(msg: str) -> None:
    print("  " + msg, flush=True)


def warn(msg: str) -> None:
    print("  ! " + msg, flush=True)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


_SEP_RE = re.compile(r"[\s_\-.\u00b7\u30fb\uff0e]+")
_WEIGHTS = sorted(
    [
        "extralight", "ultralight", "semilight", "demilight", "ultrabold",
        "extrabold", "semibold", "demibold", "extrablack", "ultrablack",
        "regular", "normal", "medium", "black", "heavy", "light", "book",
        "thin", "bold", "italic", "oblique",
    ],
    key=len,
    reverse=True,
)


def normalize(name: str) -> str:
    """比较用的规范名：全角转半角、小写、去掉分隔符与竖排前缀 @。"""
    s = (name or "").strip()
    while s.startswith("@"):
        s = s[1:].strip()
    s = unicodedata.normalize("NFKC", s).casefold()
    return _SEP_RE.sub("", s)


def weight_of(name: str) -> str:
    """取名字末尾的字重词（bold/medium/light…），没有则返回空串。"""
    n = normalize(name)
    for w in _WEIGHTS:
        if n.endswith(w) and len(n) > len(w) + 1:
            return w
    return ""


_ENCODING_SUFFIXES = ("gb18030", "gb2312", "gbk", "big5", "b5", "gb")
_LANG_SUFFIXES = ("cn", "tc", "sc", "hk", "tw", "jp", "kr")


def _strip_suffixes(name: str, suffixes: tuple[str, ...]) -> str:
    n = base_key(name)
    changed = True
    while changed and len(n) > 2:
        changed = False
        for suf in suffixes:
            if n.endswith(suf) and len(n) > len(suf) + 1:
                n = n[: -len(suf)]
                changed = True
                break
    return n


def region_key(name: str) -> str:
    """去掉结尾的编码后缀（_GBK / -GB18030 / _BIG5…），编码不同但字体相同。"""
    return _strip_suffixes(name, _ENCODING_SUFFIXES)


def lang_key(name: str) -> str:
    """连地区后缀（CN/TC/SC/HK/JP…）也去掉，只作低分兜底，避免把繁体当成简体。"""
    return _strip_suffixes(name, _ENCODING_SUFFIXES + _LANG_SUFFIXES)


def base_key(name: str) -> str:
    """去掉末尾的字重/字形词，用来把 '思源黑体 CN Bold' 和 '思源黑体 CN' 视为同一族。"""
    n = normalize(name)
    w = weight_of(name)
    return n[: -len(w)] if w else n


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name)
    return out.strip(" .") or "font"


# ---------------------------------------------------------------- ASS 解析

_STYLE_SECTIONS = {"[v4+ styles]", "[v4 styles]", "[v4+ styles+]", "[v4 styles+]"}
_EVENT_SECTIONS = {"[events]"}
_FN_RE = re.compile(r"\\fn([^\\}]*)")
_BRACE_RE = re.compile(r"\{([^}]*)\}")


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", "replace")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    for enc in ("utf-8", "gb18030", "cp932", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def fonts_in_override(text: str) -> list[str]:
    """取一行字幕文本里 {\\fnXXX} 覆盖标签声明的字体。"""
    found: list[str] = []
    for block in _BRACE_RE.findall(text):
        for raw in _FN_RE.findall(block):
            name = raw.strip()
            if name:
                found.append(name)
    return found


def scan_ass(path: Path, used: dict[str, set[str]]) -> int:
    """解析单个字幕，把用到的字体塞进 used（字体名 -> 来源集合）。返回引用次数。"""
    text = read_text(path)
    section = ""
    style_cols: list[str] | None = None
    event_cols: list[str] | None = None
    text_index = -1
    hits = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            style_cols = event_cols = None
            text_index = -1
            continue
        low = line.lower()
        if section in _STYLE_SECTIONS:
            if low.startswith("format:"):
                style_cols = [c.strip().lower() for c in line.split(":", 1)[1].split(",")]
            elif low.startswith("style:") and style_cols:
                cells = line.split(":", 1)[1].split(",", len(style_cols) - 1)
                if "fontname" in style_cols and len(cells) >= len(style_cols):
                    font = cells[style_cols.index("fontname")].strip()
                    if font:
                        name = cells[style_cols.index("name")].strip() if "name" in style_cols else "?"
                        used.setdefault(font, set()).add(f"样式 {name}")
                        hits += 1
        elif section in _EVENT_SECTIONS:
            if low.startswith("format:"):
                event_cols = [c.strip().lower() for c in line.split(":", 1)[1].split(",")]
                text_index = event_cols.index("text") if "text" in event_cols else -1
            elif event_cols and text_index >= 0 and (
                low.startswith("dialogue:") or low.startswith("comment:")
            ):
                cells = line.split(":", 1)[1].split(",", len(event_cols) - 1)
                if len(cells) > text_index:
                    for font in fonts_in_override(cells[text_index]):
                        used.setdefault(font, set()).add(path.name)
                        hits += 1
    return hits


def extract_subtitles(zip_path: Path, dest: Path) -> list[Path]:
    """从 zip（含一层嵌套 zip）里解出 .ass/.ssa，返回解出的文件列表。"""
    import zipfile

    dest.mkdir(parents=True, exist_ok=True)
    got: list[Path] = []
    nested: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            low = name.lower()
            if low.endswith((".ass", ".ssa")):
                safe = "/".join(p for p in name.split("/") if p and p not in (".", ".."))
                if not safe:
                    continue
                target = dest / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                got.append(target)
            elif low.endswith(".zip") and len(nested) < 5:
                tmp = dest / ".nested" / f"{len(nested)}.zip"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, tmp.open("wb") as out:
                    shutil.copyfileobj(src, out)
                nested.append(tmp)
    for inner in nested:
        try:
            got.extend(extract_subtitles(inner, dest))
        except Exception:  # noqa: BLE001
            continue
        finally:
            inner.unlink(missing_ok=True)
    return got


def prepare_ass_dir(value: str) -> tuple[Path, int]:
    """--ass-dir 支持普通目录，也支持直接给一个字幕 zip。返回 (目录, zip 解出的字幕数)。"""
    p = Path(value).expanduser()
    if p.is_file() and p.suffix.lower() in (".zip", ".7z", ".rar"):
        if p.suffix.lower() != ".zip":
            raise ValueError(f"暂只支持 .zip（.7z/.rar 请先解压）：{p.name}")
        import tempfile

        dest = Path(tempfile.mkdtemp(prefix="ass-font-fetch-subs-"))
        files = extract_subtitles(p, dest)
        log(f"  已从 {p.name} 解出 {len(files)} 个字幕文件 → {dest}")
        return dest, len(files)
    return p, 0


def scan_dirs(ass_dir: Path) -> tuple[dict[str, set[str]], int]:
    files = [
        p
        for p in ass_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in ASS_EXTS
    ]
    files.sort()
    used: dict[str, set[str]] = {}
    hits = 0
    for p in files:
        try:
            hits += scan_ass(p, used)
        except Exception as exc:  # noqa: BLE001
            warn(f"解析失败，已跳过 {p}: {exc}")
    return used, len(files)


# ---------------------------------------------------------------- 字体内部名

_NAME_IDS = {1, 4, 6, 16, 17, 21, 22, 25}
_NAME_CAP = 4 * 1024 * 1024


def _read_at(f, offset: int, size: int) -> bytes:
    f.seek(offset)
    return f.read(size)


def _faces(f) -> list[int]:
    head = _read_at(f, 0, 12)
    if len(head) < 12:
        return []
    tag = head[:4]
    if tag == b"ttcf":
        num = struct.unpack(">I", head[8:12])[0]
        if num <= 0 or num > 64:
            return []
        raw = _read_at(f, 12, 4 * num)
        return list(struct.unpack(f">{num}I", raw[: 4 * num]))
    return [0]


def _tables(f, base: int) -> dict[bytes, tuple[int, int]]:
    head = _read_at(f, base, 12)
    if len(head) < 12:
        return {}
    num = struct.unpack(">H", head[4:6])[0]
    if num <= 0 or num > 512:
        return {}
    rec = _read_at(f, base + 12, 16 * num)
    out: dict[bytes, tuple[int, int]] = {}
    for i in range(num):
        chunk = rec[i * 16 : (i + 1) * 16]
        if len(chunk) < 16:
            break
        tag, _checksum, off, length = struct.unpack(">4sIII", chunk)
        out[tag] = (off, length)
    return out


def _decode_name(platform: int, raw: bytes) -> str:
    if platform in (0, 3):
        return raw.decode("utf-16-be", "ignore")
    if platform == 1:
        return raw.decode("mac_roman", "ignore")
    return ""


def font_names(path: Path) -> set[str]:
    """读字体文件 name 表里的家族名/全名/PostScript 名（含各语言本地化名）。"""
    names: set[str] = set()
    try:
        with path.open("rb") as f:
            for base in _faces(f):
                tables = _tables(f, base)
                loc = tables.get(b"name")
                if not loc:
                    continue
                off, length = loc
                if length <= 0 or length > _NAME_CAP:
                    continue
                buf = _read_at(f, off, length)
                if len(buf) < 6:
                    continue
                _fmt, count, str_off = struct.unpack(">HHH", buf[:6])
                for i in range(min(count, 512)):
                    p = 6 + i * 12
                    if p + 12 > len(buf):
                        break
                    plat, _enc, _lang, nid, slen, soff = struct.unpack(">HHHHHH", buf[p : p + 12])
                    if nid not in _NAME_IDS or slen == 0:
                        continue
                    start = str_off + soff
                    chunk = buf[start : start + slen]
                    if not chunk:
                        continue
                    text = _decode_name(plat, chunk).strip()
                    if text:
                        names.add(text)
    except Exception:  # noqa: BLE001 - 损坏字体不应中断整体流程
        pass
    return names


def names_for_file(path: Path) -> set[str]:
    out = {path.stem}
    out |= font_names(path)
    return out


# ---------------------------------------------------------------- 已装字体清单

def windows_font_dirs() -> list[Path]:
    dirs: list[Path] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return [d for d in dirs if d.is_dir()]


def registry_font_names() -> set[str]:
    """注册表里的字体显示名（含 '宋体 & SimSun (TrueType)' 这类双语值）。"""
    names: set[str] = set()
    try:
        import winreg
    except ImportError:
        return names
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    value, _data, _type = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                v = re.sub(r"\s*\((?:TrueType|OpenType|Type 1)\)\s*$", "", value, flags=re.I)
                for part in re.split(r"\s*&\s*", v):
                    part = part.strip()
                    if part:
                        names.add(part)
    return names


def fc_list_names() -> set[str]:
    """非 Windows 环境用 fontconfig 兜底。"""
    names: set[str] = set()
    if not any(Path(p).is_dir() for p in ("/usr/share/fonts", "/System/Library/Fonts")):
        return names
    try:
        import subprocess

        out = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, timeout=60, check=False
        ).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            for part in line.split(","):
                part = part.strip()
                if part:
                    names.add(part)
    except Exception:  # noqa: BLE001
        pass
    return names


def collect_font_files(dirs: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for root in dirs:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in FONT_EXTS:
                key = str(p).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(p)
    return out


def build_inventory(font_dirs: list[Path], use_system: bool, verbose: bool) -> tuple[set[str], dict[str, int]]:
    """返回 (可用字体名集合, 统计信息)。"""
    have: set[str] = set()
    stats = {"system_files": 0, "dir_files": 0}
    if use_system:
        have |= registry_font_names()
        have |= fc_list_names()
        files = collect_font_files(windows_font_dirs())
        stats["system_files"] = len(files)
        if verbose and files:
            info(f"系统字体文件 {len(files)} 个，读取内部名称…")
        for p in files:
            have |= names_for_file(p)
    files = collect_font_files(font_dirs)
    stats["dir_files"] = len(files)
    if verbose and files:
        info(f"指定字体目录内 {len(files)} 个字体文件，读取内部名称…")
    for p in files:
        have |= names_for_file(p)
    return have, stats


def have_font(name: str, have_norms: set[str], have_bases: set[str]) -> bool:
    n = normalize(name)
    if not n:
        return True
    return n in have_norms or base_key(name) in have_bases


# ---------------------------------------------------------------- 站点索引

_ROW_RE = re.compile(r'<a class="file-link" href="([^"]*)" title="([^"]*)"', re.I)


def http_get(url: str, retries: int = 3, timeout: int = 30) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 + attempt * 1.5)
    raise last  # type: ignore[misc]


def parse_listing(html: str) -> tuple[list[str], list[str]]:
    dirs: list[str] = []
    files: list[str] = []
    for href, title in _ROW_RE.findall(html):
        if href.startswith("?dir="):
            if title != "..":
                dirs.append(urllib.parse.unquote(href[5:]))
        elif href.startswith("http") or href.startswith("#"):
            continue
        else:
            files.append(urllib.parse.unquote(href))
    return dirs, files


def load_index(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != INDEX_VERSION:
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def save_index(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def crawl_site(roots: list[str], jobs: int) -> list[str]:
    """广度优先抓取站点目录，返回所有字体文件相对路径。"""
    from collections import deque
    from concurrent.futures import FIRST_COMPLETED, wait

    done: set[str] = set()
    files: list[str] = []
    queue: deque[str] = deque(roots)
    t0 = time.time()
    errors = 0
    executor = ThreadPoolExecutor(max(jobs, 1))
    inflight: dict = {}

    def work(path: str) -> tuple[list[str], list[str]]:
        url = SITE + "?dir=" + urllib.parse.quote(path)
        return parse_listing(http_get(url).decode("utf-8", "replace"))

    try:
        while queue or inflight:
            while queue and len(inflight) < max(jobs, 1) * 2:
                p = queue.popleft()
                if p in done:
                    continue
                inflight[executor.submit(work, p)] = p
            finished, _ = wait(list(inflight), return_when=FIRST_COMPLETED, timeout=30)
            for fut in finished:
                path = inflight.pop(fut)
                if path in done:
                    continue
                done.add(path)
                try:
                    dirs, fs = fut.result()
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    warn(f"目录抓取失败 {path}: {exc}")
                    continue
                files.extend(fs)
                for d in dirs:
                    if d not in done:
                        queue.append(d)
                if len(done) % 25 == 0:
                    el = max(time.time() - t0, 1e-6)
                    log(
                        f"  已扫描 {len(done)} 个目录，待扫 {len(queue)}，"
                        f"字体 {len(files)}，{el:.0f}s ({len(done)/el:.1f} 目录/秒)"
                    )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if errors:
        warn(f"有 {errors} 个目录抓取失败（不影响其它目录）")
    return files


def ensure_index(
    path: Path,
    scope: str,
    jobs: int,
    refresh: bool,
    max_age_days: float,
    verbose: bool,
) -> list[str]:
    # full 索引是 lite 的超集，可以互相复用
    accepted = {scope} | {"full"}
    data = None if refresh else load_index(path)
    if data and data.get("complete"):
        age_days = (time.time() - float(data.get("timestamp") or 0)) / 86400.0
        cached_scope = data.get("scope")
        if age_days <= max_age_days and cached_scope in accepted:
            if verbose:
                info(
                    f"复用站内索引 {path.name}（scope={cached_scope}，{age_days:.1f} 天前，"
                    f"{len(data['files'])} 个字体）"
                )
            return data["files"]
        if verbose:
            info(f"索引过期或范围不匹配（scope={cached_scope}，{age_days:.1f} 天），需重新抓取")
    log(f"  正在建立站内索引（scope={scope}），首次较慢，请耐心等…")
    log("  提示：不想等就 Ctrl+C，用 --max-age-days 放宽缓存有效期，或继续用现有索引")
    files = crawl_site(SCOPE_ROOTS[scope], jobs)
    files = sorted(set(files))
    save_index(
        path,
        {
            "version": INDEX_VERSION,
            "scope": scope,
            "complete": True,
            "timestamp": time.time(),
            "site": SITE,
            "files": files,
        },
    )
    log(f"  站内索引完成：{len(files)} 个字体文件 → {path.name}")
    return files


# ---------------------------------------------------------------- 匹配

class SiteCatalog:
    def __init__(self, paths: list[str]) -> None:
        self.raw = paths
        self.items: list[tuple[str, str, str, str, str]] = []
        for p in paths:
            base = os.path.basename(p)
            stem = os.path.splitext(base)[0]
            n = normalize(stem)
            self.items.append((p, base, stem, n, base_key(stem)))

    def match(
        self, name: str, aliases: list[str] | None = None, threshold: int = 78, limit: int = 6
    ) -> list[tuple[int, str]]:
        alias_keys = {normalize(a) for a in (aliases or []) if normalize(a)}
        n = normalize(name)
        b = base_key(name)
        if not n:
            return []
        scored: list[tuple[int, str]] = []
        q_weight = weight_of(name)
        for path, _base, stem, en, eb in self.items:
            if not en:
                continue
            if alias_keys and (en in alias_keys or eb in alias_keys):
                scored.append((99, path))
                continue
            if en == n:
                scored.append((100, path))
                continue
            c_weight = weight_of(stem)
            italic = ("italic" in en or "oblique" in en) and not (
                "italic" in n or "oblique" in n
            )
            if len(n) >= 2 and eb != b:
                if region_key(name) == region_key(stem):
                    score = 93 - (3 if italic else 0)
                    if score >= threshold:
                        scored.append((score, path))
                    continue
                if lang_key(name) == lang_key(stem):
                    score = 88 - (3 if italic else 0)
                    if score >= threshold:
                        scored.append((score, path))
                    continue
            if eb == b:
                if q_weight and c_weight == q_weight:
                    score = 96
                elif not q_weight and c_weight == "regular":
                    score = 97
                elif not q_weight and c_weight == "":
                    score = 96
                elif not q_weight and c_weight == "normal":
                    score = 95
                elif not q_weight:
                    score = 89
                elif not c_weight:
                    score = 92
                else:
                    score = 88
            elif len(n) < 2:
                # 单字符只按精确/同族判，不然「x」能把半个站匹配出来
                continue
            elif not (set(b or n) & set(eb or en)):
                continue
            elif (b and b in en) or (n and n in en) or (eb and eb in n):
                score = 85
            else:
                left, right = (b or n), (eb or en)
                if abs(len(left) - len(right)) > 12:
                    continue
                sm = difflib.SequenceMatcher(None, left, right)
                if sm.real_quick_ratio() < 0.75 or sm.quick_ratio() < 0.75:
                    continue
                ratio = sm.ratio()
                if ratio < 0.80:
                    continue
                score = int(60 + (ratio - 0.80) / 0.20 * 24)
            if italic:
                score -= 6
            if score >= threshold:
                scored.append((score, path))
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        # 同一文件名可能在多个镜像目录里出现，只保留得分最高的那一个
        seen: set[str] = set()
        unique: list[tuple[int, str]] = []
        for score, path in scored:
            name = os.path.basename(path).casefold()
            if name in seen:
                continue
            seen.add(name)
            unique.append((score, path))
        return unique[:limit]

    def same_family(self, path: str) -> list[str]:
        key = base_key(os.path.splitext(os.path.basename(path))[0])
        if not key:
            return [path]
        out: list[str] = []
        seen: set[str] = set()
        for p, _b, _s, _n, eb in self.items:
            if eb != key:
                continue
            name = os.path.basename(p).casefold()
            if name in seen:
                continue
            seen.add(name)
            out.append(p)
        return out or [path]


# ---------------------------------------------------------------- 下载


def looks_like_font(blob: bytes) -> bool:
    if len(blob) < 4:
        return False
    head = blob[:4]
    return head in (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"true", b"typ1") or head[:2] == b"\x00\x01"


def download_font(
    rel_path: str,
    out_dir: Path,
    keep_tree: bool,
    force: bool,
    source: str | None = None,
    progress=None,
) -> tuple[str, Path, int]:
    """流式下载并回报进度。返回 (状态, 本地路径, 字节数)。状态: downloaded / exists / failed。"""
    if rel_path.startswith(("http://", "https://")):
        if "zhaozi.org" in rel_path and rel_path.lower().endswith((".htm", ".html")):
            picked = pick_zhaozi_file(zhaozi_links(rel_path))
            if not picked:
                return "failed", out_dir / "unknown", 0
            url, fname = picked
        else:
            url = rel_path
            parsed = urllib.parse.urlparse(rel_path)
            fname = urllib.parse.unquote(os.path.basename(parsed.path)) or "font"
            query = urllib.parse.parse_qs(parsed.query).get("filename")
            if query:
                fname = query[0]
    else:
        fname = os.path.basename(rel_path)
        url = source_url(source_base(source), rel_path)
    base = safe_filename(fname)
    if keep_tree and not rel_path.startswith(("http://", "https://")):
        parts = [safe_filename(p) for p in rel_path.split("/") if p]
        dest = out_dir.joinpath(*parts)
    else:
        dest = out_dir / base
    if dest.exists() and dest.stat().st_size > 1024 and not force:
        return "exists", dest, dest.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=600) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with tmp.open("wb") as fh:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total, fname)
            head = tmp.read_bytes()[:4]
            if not looks_like_font(head):
                tmp.unlink(missing_ok=True)
                return "failed", dest, 0
            os.replace(tmp, dest)
            return "downloaded", dest, dest.stat().st_size
        except Exception:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            if attempt == 0:
                time.sleep(1.5)
    return "failed", dest, 0


def progress_reporter(enabled: bool, file_name: str):
    """把下载回调节流成 @@PROGRESS 行（约 6 帧/秒）。"""
    state = {"last": 0.0}

    def cb(done: int, total: int, name: str | None = None) -> None:
        now = time.time()
        if now - state["last"] < 0.15 and (total <= 0 or done < total):
            return
        state["last"] = now
        emit("PROGRESS", {"file": name or file_name, "done": done, "total": total}, enabled)

    return cb


def display_name(spec: str) -> str:
    """给 URL / 站内路径起个好看的显示名（URL 优先取 ?filename=）。"""
    if spec.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(spec)
        q = urllib.parse.parse_qs(parsed.query).get("filename")
        if q:
            return q[0]
        return os.path.basename(parsed.path) or spec
    return os.path.basename(spec)


# ---------------------------------------------------------------- 主流程


def choose_candidate(name: str, cands: list[tuple[int, str]], catalog: SiteCatalog, assume_yes: bool) -> str | None:
    if not cands:
        return None
    if len(cands) == 1 or assume_yes:
        return cands[0][1]
    log(f"  找到 {len(cands)} 个候选：")
    for i, (score, path) in enumerate(cands, 1):
        log(f"    [{i}] {score:>3}分  {path}")
    if not sys.stdin.isatty():
        warn("非交互环境，自动选择第一个")
        return cands[0][1]
    while True:
        try:
            answer = input(f"  为「{name}」选择下载项（回车=1，s=跳过）: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in ("", "1"):
            return cands[0][1]
        if answer in ("s", "skip", "n", "no"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(cands):
            return cands[int(answer) - 1][1]


def default_index(filename: str) -> Path:
    """索引文件：优先用可写目录里已有的，其次用打包进来的那份。"""
    p = HERE / filename
    if p.exists():
        return p
    q = res_dir() / filename
    return q if q.exists() else p


def split_source(spec: str, default_source: str) -> tuple[str, str]:
    """'acgrip::LPSub Fonts/x.ttf' → ('acgrip', 'LPSub Fonts/x.ttf')"""
    if "::" in spec:
        name, rel = spec.split("::", 1)
        if name and rel:
            return name, rel
    return default_source, spec


def run_search(name: str, args, here: Path) -> int:
    src = get_source(args.source)
    if src.get("type") == "zhaozi":
        results = zhaozi_search(name, src["base"])
        if not results:
            log(f"找字网上没有匹配「{name}」的字体")
            return 1
        log(f"「{name}」在{src['label']}的候选：")
        for item in results[: args.limit]:
            names = " , ".join(item["names"][:4])
            log(f"  {item['score']:>3}分  {names}")
            log(f"          {item['path']}")
        return 0
    index_path = Path(args.index).expanduser() if args.index else default_index(src["index"])
    files = ensure_index(index_path, args.scope, max(args.jobs, 1), args.refresh_index, args.max_age_days, args.verbose)
    catalog = SiteCatalog(files)
    cands = catalog.match(name, limit=args.limit)
    if not cands:
        log(f"没有匹配「{name}」的字体")
        return 1
    log(f"「{name}」的候选（{src['label']}）：")
    for score, path in cands:
        log(f"  {score:>3}分  {path}")
    return 0


def run_direct_download(specs: list[str], args, here: Path) -> int:
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    default_source = args.source
    emit("TOTAL", {"total": len(specs)}, args.progress_json)
    results: list[dict] = []
    log("== 下载选中的字体 ==")
    for idx, spec in enumerate(specs, 1):
        source_name, rel = split_source(spec, default_source)
        fname = display_name(rel)
        if rel.startswith("http") and "zhaozi.org" in rel and rel.lower().endswith((".htm", ".html")):
            try:
                picked = pick_zhaozi_file(zhaozi_links(rel))
                if picked:
                    fname = picked[1]
            except Exception:  # noqa: BLE001
                pass
        emit("FILE", {"idx": idx, "name": fname, "file": fname, "status": "start", "bytes": 0}, args.progress_json)
        cb = progress_reporter(args.progress_json, fname)
        status, dest, size = download_font(rel, out_dir, args.keep_tree, args.force, source_name, cb)
        emit(
            "FILE",
            {"idx": idx, "name": fname, "file": dest.name, "status": status, "bytes": size},
            args.progress_json,
        )
        results.append({"name": fname, "status": status, "file": str(dest), "bytes": size, "source": rel})
        if status == "downloaded":
            info(f"{fname}  →  已下载 ({human_size(size)})")
        elif status == "exists":
            info(f"{fname}  →  已存在，跳过")
        else:
            warn(f"{fname}  →  下载失败")
    done = [r for r in results if r["status"] == "downloaded"]
    exist = [r for r in results if r["status"] == "exists"]
    failed = [r for r in results if r["status"] == "failed"]
    log("\n== 结果 ==")
    info(f"成功 {len(done)} 个，已存在 {len(exist)} 个，失败 {len(failed)} 个")
    info(f"保存目录：{out_dir}")
    if args.report:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": "direct",
            "out_dir": str(out_dir),
            "downloads": results,
            "matched": {},
            "missing": [],
            "not_found": [],
            "fonts_used": {},
            "ass_files": 0,
        }
        Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        info(f"报告已写入 {args.report}")
    return 0 if not failed else 3


def main(argv: list[str] | None = None) -> int:
    setup_console()
    here = app_dir()
    parser = argparse.ArgumentParser(
        description="扫描 ASS/SSA 所需字体，比对系统与字体文件夹，并从字体站下载缺失字体。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ass-dir", help="字幕文件夹（递归扫描 .ass/.ssa）；用 --search/--download 时可省略")
    parser.add_argument(
        "--font-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="额外要比对的字体文件夹，可重复指定",
    )
    parser.add_argument("--out", default=str(here / "fonts_downloaded"), help="下载保存目录")
    parser.add_argument("--index", help="站内索引缓存文件（默认按字体源自动取）")
    parser.add_argument("--source", default="acgrip", help="字体源名称（见 sources.json）")
    parser.add_argument("--search", metavar="NAME", help="只搜索字体名，不扫描字幕、不下载")
    parser.add_argument("--limit", type=int, default=20, help="搜索时最多返回多少条")
    parser.add_argument(
        "--download",
        action="append",
        default=[],
        metavar="PATH",
        help="直接下载站内某个文件路径（可写 源名::路径），可重复",
    )
    parser.add_argument("--progress-json", action="store_true", help="输出 @@PROGRESS 机器可读进度行")
    parser.add_argument("--refresh-index", action="store_true", help="强制重建站内索引")
    parser.add_argument("--scope", choices=sorted(SCOPE_ROOTS), default="lite", help="索引抓取范围")
    parser.add_argument("--max-age-days", type=float, default=90.0, help="索引缓存有效期（天）")
    parser.add_argument("--jobs", type=int, default=3, help="抓取站点索引的并发数（站点较慢，别调太大）")
    parser.add_argument("--aliases", help="字体别名表 JSON：{字幕里的名字: [候选文件名或名字, ...]}")
    parser.add_argument("--variants", choices=["base", "all"], default="base", help="同一字族只下基准文件，还是全部字重")
    parser.add_argument("--dry-run", action="store_true", help="只分析、不下载")
    parser.add_argument("--yes", action="store_true", help="候选有多个时自动选最匹配的")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument("--keep-tree", action="store_true", help="保留下载站点的目录层级")
    parser.add_argument("--no-system", action="store_true", help="不读取系统已安装字体")
    parser.add_argument("--no-zhaozi", action="store_true", help="acgrip 没匹配到时不去找字网兜底")
    parser.add_argument("--report", help="把结果写成 JSON 报告")
    parser.add_argument("--verbose", action="store_true", help="输出更多过程信息")
    args = parser.parse_args(argv)

    if args.search:
        return run_search(args.search, args, here)
    if args.download:
        return run_direct_download(args.download, args, here)
    if not args.ass_dir:
        log("错误：要么给 --ass-dir，要么用 --search / --download")
        return 2

    try:
        ass_dir, zip_subs = prepare_ass_dir(args.ass_dir)
    except ValueError as exc:
        log(f"错误：{exc}")
        return 2
    if not ass_dir.is_dir():
        log(f"错误：字幕目录不存在 → {ass_dir}")
        return 2
    out_dir = Path(args.out).expanduser()
    index_path = Path(args.index).expanduser() if args.index else default_index(get_source(args.source)["index"])
    font_dirs = [Path(d).expanduser() for d in args.font_dir]
    for d in font_dirs:
        if not d.is_dir():
            warn(f"字体目录不存在，已忽略 → {d}")
    font_dirs = [d for d in font_dirs if d.is_dir()]

    log("== 1/4 扫描字幕 ==")
    used, ass_count = scan_dirs(ass_dir)
    if ass_count == 0:
        log("没找到 .ass/.ssa 文件，结束。")
        return 1
    info(f"字幕文件 {ass_count} 个，字体引用 {sum(len(v) for v in used.values())} 处，去重后 {len(used)} 个字体")

    log("== 2/4 读取已安装 / 已有字体 ==")
    have, stats = build_inventory(font_dirs, not args.no_system, args.verbose)
    have_norms = {normalize(x) for x in have if normalize(x)}
    have_bases = {base_key(x) for x in have if base_key(x)}
    info(f"系统字体文件 {stats['system_files']} 个，字体目录文件 {stats['dir_files']} 个，名称条目 {len(have_norms)} 条")

    missing = [name for name in sorted(used) if not have_font(name, have_norms, have_bases)]
    log(f"  缺失 {len(missing)} 个字体")
    if not missing:
        log("\n全部字体都齐了，无需下载。")
        return 0

    aliases: dict[str, list[str]] = {}
    if args.aliases:
        try:
            with open(args.aliases, encoding="utf-8") as f:
                aliases = {k: list(v) if isinstance(v, list) else [str(v)] for k, v in json.load(f).items()}
        except Exception as exc:  # noqa: BLE001
            warn(f"别名表读取失败，已忽略：{exc}")

    log("== 3/4 匹配站内字体 ==")
    site_files = ensure_index(
        index_path, args.scope, max(args.jobs, 1), args.refresh_index, args.max_age_days, args.verbose
    )
    catalog = SiteCatalog(site_files)
    plans: list[dict] = []
    # 第一阶段：所有缺失字体统一在 acgrip 里找
    for name in missing:
        cands = catalog.match(name, aliases.get(name))
        plans.append({"name": name, "candidates": cands, "origin": "acgrip" if cands else ""})
        if cands:
            info(f"{name}  →  {cands[0][1]}  ({cands[0][0]}分)")

    # 第二阶段：acgrip 没找到的，再统一去另一个站（找字网）
    unmatched = [p for p in plans if not p["candidates"]]
    zsrc = next((x for x in load_sources() if x.get("type") == "zhaozi"), None)
    if unmatched:
        if args.no_zhaozi or not zsrc:
            log(f"  另有 {len(unmatched)} 个 acgrip 没找到（找字网兜底已关闭）")
            for plan in unmatched:
                info(f"{plan['name']}  →  未找到")
        else:
            log(f"== 3b/4 acgrip 没找到的 {len(unmatched)} 个，再去「{zsrc['label']}」找 ==")
            for plan in unmatched:
                try:
                    zbatch = zhaozi_search(plan["name"], zsrc["base"])
                except Exception:  # noqa: BLE001
                    zbatch = []
                if zbatch:
                    plan["candidates"] = [(it["score"], it["path"]) for it in zbatch[:4]]
                    plan["origin"] = "zhaozi"
                    info(f"{plan['name']}  →  找字网命中 {len(zbatch)} 条，默认取第一条")
                    info(f"      {plan['candidates'][0][1]}")
                else:
                    info(f"{plan['name']}  →  两站都没找到")
    found = [p for p in plans if p["candidates"]]
    not_found = [p["name"] for p in plans if not p["candidates"]]

    log("== 4/4 下载 ==")
    results: list[dict] = []
    if args.dry_run:
        info("dry-run：以下文件将被下载")
        for plan in found:
            info(f"  {plan['name']}  ←  {plan['candidates'][0][1]}")
        for name in not_found:
            info(f"  {name}  ←  未找到")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        plan_files: list[tuple[str, list[str]]] = []
        for plan in found:
            chosen = choose_candidate(plan["name"], plan["candidates"], catalog, args.yes)
            if not chosen:
                results.append({"name": plan["name"], "status": "skipped", "file": None, "bytes": 0})
                info(f"{plan['name']}  →  跳过")
                continue
            rels = (
                catalog.same_family(chosen)
                if args.variants == "all" and not chosen.startswith("http")
                else [chosen]
            )
            plan_files.append((plan["name"], rels))
        emit("TOTAL", {"total": sum(len(r) for _, r in plan_files)}, args.progress_json)
        done_files = 0
        for name, rels in plan_files:
            for rel in rels:
                done_files += 1
                fname = os.path.basename(rel)
                emit(
                    "FILE",
                    {"idx": done_files, "name": name, "file": fname, "status": "start", "bytes": 0},
                    args.progress_json,
                )
                status, dest, size = download_font(
                    rel, out_dir, args.keep_tree, args.force, args.source, progress_reporter(args.progress_json, fname)
                )
                emit(
                    "FILE",
                    {"idx": done_files, "name": name, "file": dest.name, "status": status, "bytes": size},
                    args.progress_json,
                )
                results.append(
                    {"name": name, "status": status, "file": str(dest), "bytes": size, "source": rel}
                )
                if status == "downloaded":
                    info(f"{name}  →  已下载 {dest.name} ({human_size(size)})")
                elif status == "exists":
                    info(f"{name}  →  已存在 {dest.name}")
                else:
                    warn(f"{name}  →  下载失败 {rel}")
                if status == "downloaded" and args.verbose:
                    inner = names_for_file(dest)
                    inner_norms = {normalize(x) for x in inner if normalize(x)}
                    inner_bases = {base_key(x) for x in inner if base_key(x)}
                    if inner and not have_font(name, inner_norms, inner_bases):
                        warn(f"  注意：{dest.name} 内部字体名与「{name}」不完全一致，请确认是否下对了")

    log("\n== 结果 ==")
    ok = [r for r in results if r["status"] in ("downloaded", "exists")]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    info(f"下载成功 {len([r for r in ok if r['status'] == 'downloaded'])} 个，已存在 {len([r for r in ok if r['status'] == 'exists'])} 个，失败 {len(failed)} 个，跳过 {len(skipped)} 个")
    if not args.dry_run:
        info(f"保存目录：{out_dir}")
    if not_found:
        warn(f"站内未找到 {len(not_found)} 个字体：")
        for name in not_found:
            info(f"  - {name}")
        info("可用 --search 手动搜一下，或到 https://bbs.acgrip.com/ 找；也可用 --aliases 指定对应文件名。")

    if args.report:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ass_dir": str(ass_dir),
            "font_dirs": [str(d) for d in font_dirs],
            "ass_files": ass_count,
            "fonts_used": {k: sorted(v) for k, v in used.items()},
            "missing": missing,
            "matched": {p["name"]: p["candidates"][0][1] for p in found},
            "not_found": not_found,
            "downloads": results,
        }
        Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        info(f"报告已写入 {args.report}")

    return 0 if not failed and not not_found else 3


if __name__ == "__main__":
    raise SystemExit(main())
