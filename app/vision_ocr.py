"""版头区 OCR 后端：优先 macOS 原生 Vision（零安装），回退 Tesseract。

红头与标题在不少政务公文 PDF 里是图片、不进文本层，只能靠 OCR 读取。
macOS 自带 Vision 框架支持简体中文，无需安装任何依赖，故作为首选后端。
"""
from __future__ import annotations
import shutil, subprocess
from pathlib import Path

_SCRIPT = Path(__file__).with_name('macos_vision_ocr.js')


def _macos_vision(png):
    if not (shutil.which('osascript') and _SCRIPT.exists()):
        return None
    try:
        proc = subprocess.run(['osascript', '-l', 'JavaScript', str(_SCRIPT), str(png)],
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def _tesseract(png):
    exe = shutil.which('tesseract')
    if not exe:
        return None
    try:
        langs = subprocess.run([exe, '--list-langs'], capture_output=True, text=True).stdout
        if 'chi_sim' not in langs:
            return None
        proc = subprocess.run([exe, str(png), 'stdout', '-l', 'chi_sim+eng'],
                              capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def available():
    return bool(shutil.which('osascript') and _SCRIPT.exists()) or bool(shutil.which('tesseract'))


def recognize(png_path):
    """返回 (文本, 方法名)；两个后端都不可用或无内容时返回 (None, None)。"""
    for backend, name in ((_macos_vision, 'macos-vision'), (_tesseract, 'tesseract')):
        text = backend(png_path)
        if text:
            return text, name
    return None, None
