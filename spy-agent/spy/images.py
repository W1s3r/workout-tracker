"""Images: clipboard capture (screenshots), files, and encoding for the model.

Pasting a screenshot into a terminal doesn't send image bytes to the program -
the terminal only pastes text. So, like Claude Code, Spy reads the OS clipboard
itself when you press Alt+V (or Ctrl+V, if the terminal passes it through), or
run /paste. Supported:
  Windows   pure ctypes (CF_DIB bitmap -> PNG), or files copied in Explorer (CF_HDROP)
  Pillow    ImageGrab.grabclipboard() when Anaconda's Pillow is present (also mac)
  Linux     wl-paste / xclip      macOS  pngpaste / osascript
Images are saved under ~/.spy/images/ and sent to vision-capable models as
OpenAI-style image_url parts (base64 data URLs). Large images are downscaled
when Pillow is available.
"""
from __future__ import annotations

import base64
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import List, Optional, Tuple

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp"}
EST_TOKENS_PER_IMAGE = 1500


def is_image_path(p) -> bool:
    return Path(str(p)).suffix.lower() in IMAGE_EXTS


def sniff_mime(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


# ------------------------------------------------------------ PNG (stdlib) ---
def png_encode(width: int, height: int, rows: List[bytes], channels: int = 3) -> bytes:
    """Encode top-down rows of RGB (channels=3) or RGBA (4) bytes as a PNG."""
    color_type = 2 if channels == 3 else 6
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def png_size(data: bytes) -> Tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return struct.unpack(">II", data[16:24])


def dib_to_png(dib: bytes) -> bytes:
    """Convert a Windows clipboard DIB (BITMAPINFOHEADER/V4/V5 + pixels) to PNG.
    Handles 24- and 32-bit, top-down and bottom-up. Alpha is dropped (clipboard
    screenshots usually carry an all-zero alpha channel)."""
    hdr_size, width, height, planes, bpp, compression = struct.unpack("<IiiHHI", dib[:20])
    clr_used = struct.unpack("<I", dib[32:36])[0] if len(dib) >= 36 else 0
    if bpp not in (24, 32):
        raise ValueError("unsupported clipboard bitmap depth: %d-bit" % bpp)
    offset = hdr_size
    if compression == 3 and hdr_size == 40:  # BI_BITFIELDS masks follow a plain header
        offset += 12
    offset += clr_used * 4
    top_down = height < 0
    h = abs(height)
    stride = ((width * bpp + 31) // 32) * 4
    step = bpp // 8
    rows = []
    for y in range(h):
        src_y = y if top_down else h - 1 - y
        row = dib[offset + src_y * stride: offset + src_y * stride + width * step]
        out = bytearray(width * 3)
        out[0::3] = row[2::step]
        out[1::3] = row[1::step]
        out[2::3] = row[0::step]
        rows.append(bytes(out))
    return png_encode(width, h, rows, 3)


# -------------------------------------------------------------- clipboard ---
def _win_clipboard() -> Tuple[Optional[bytes], List[str], Optional[str]]:
    """(png_bytes, file_paths, text) from the Windows clipboard."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32, shell32 = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.shell32
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    CF_UNICODETEXT, CF_DIB, CF_HDROP = 13, 8, 15
    png, files, text = None, [], None
    for _ in range(10):  # another app may hold the clipboard briefly
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None, [], None
    try:
        if user32.IsClipboardFormatAvailable(CF_DIB):
            h = user32.GetClipboardData(CF_DIB)
            if h:
                p = kernel32.GlobalLock(h)
                try:
                    png = dib_to_png(ctypes.string_at(p, kernel32.GlobalSize(h)))
                finally:
                    kernel32.GlobalUnlock(h)
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            h = user32.GetClipboardData(CF_HDROP)
            n = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
            for i in range(n):
                ln = shell32.DragQueryFileW(h, i, None, 0)
                buf = ctypes.create_unicode_buffer(ln + 1)
                shell32.DragQueryFileW(h, i, buf, ln + 1)
                files.append(buf.value)
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if h:
                p = kernel32.GlobalLock(h)
                try:
                    text = ctypes.wstring_at(p)
                finally:
                    kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    return png, files, text


def _pil_clipboard() -> Tuple[Optional[bytes], List[str]]:
    try:
        from PIL import Image, ImageGrab
    except Exception:
        return None, []
    try:
        obj = ImageGrab.grabclipboard()
    except Exception:
        return None, []
    if isinstance(obj, list):
        return None, [str(p) for p in obj]
    if obj is not None and hasattr(obj, "save"):
        import io
        buf = io.BytesIO()
        obj.convert("RGB").save(buf, "PNG")
        return buf.getvalue(), []
    return None, []


def _cmd_clipboard() -> Optional[bytes]:
    cmds = []
    if sys.platform == "darwin":
        if shutil.which("pngpaste"):
            cmds.append(["pngpaste", "-"])
    else:
        if shutil.which("wl-paste"):
            cmds.append(["wl-paste", "--no-newline", "--type", "image/png"])
        if shutil.which("xclip"):
            cmds.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, timeout=5)
            if r.returncode == 0 and sniff_mime(r.stdout):
                return r.stdout
        except Exception:
            continue
    return None


def grab_clipboard(save_dir: Path) -> Tuple[List[Path], Optional[str]]:
    """Return (image paths, clipboard text). A bitmap is saved as a PNG in save_dir;
    image files copied in Explorer/Finder are returned as their own paths."""
    png, files, text = None, [], None
    if os.name == "nt":
        try:
            png, files, text = _win_clipboard()
        except Exception:
            png, files = _pil_clipboard()
    else:
        png, files = _pil_clipboard()
        if png is None and not files:
            png = _cmd_clipboard()
    out: List[Path] = [Path(f) for f in files if is_image_path(f) and os.path.isfile(f)]
    if png:
        save_dir.mkdir(parents=True, exist_ok=True)
        p = save_dir / ("paste_%s_%03d.png" % (time.strftime("%Y%m%d_%H%M%S"), int(time.time() * 1000) % 1000))
        p.write_bytes(png)
        out.insert(0, p)
    return out, text


# ------------------------------------------------------------ to the model ---
def image_data_url(path, max_px: int = 1568) -> str:
    """Base64 data URL for an image file; downscaled (and BMP converted) with Pillow if available."""
    p = Path(str(path))
    data = p.read_bytes()
    mime = sniff_mime(data) or MIME.get(p.suffix.lower(), "image/png")
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        if max(im.size) > max_px or mime == "image/bmp":
            im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            data, mime = buf.getvalue(), "image/png"
    except Exception:
        pass
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def describe(path) -> str:
    p = Path(str(path))
    try:
        data = p.read_bytes()
    except OSError:
        return str(p)
    w, h = png_size(data)
    try:
        from PIL import Image
        import io
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        pass
    size = "%dx%d, " % (w, h) if w else ""
    return "%s (%s%.0f KB)" % (p.name, size, len(data) / 1024)
