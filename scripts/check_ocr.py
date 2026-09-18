"""验证 OCR 可用性，两条通路分别检查并给出方法名。

1. 版头区定向 OCR（`app.vision_ocr`）：先 macOS 原生 Vision，再 Tesseract。
   —— 红头/标题为图片层时走这条，是当前的关键通路。
2. 整页 OCR 兜底（`parse_pdf` 内建，经 `ocr_page_lines`，同样优先 macOS Vision）：
   —— 整页无文本层（纯扫描件）时走这条；识别不出内容时再用墨迹占比
      （`INK_BLANK_RATIO`）区分「真空白页」与「疑似扫描页」。

退出码：版头区 OCR 可用则 0（关键通路），否则 1。
"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf as fitz
from app.extract import parse_pdf
from app import vision_ocr

LINES = ['演示市发展和改革局', '项目总建筑面积4702平方米', '项目建设工期为24个月', '项目总投资2998万元']


def render(td):
    source = fitz.open()
    page = source.new_page()
    for i, text in enumerate(LINES):
        page.insert_text((50, 80 + i * 50), text, fontname='china-s', fontsize=20)
    pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
    png = Path(td) / 'page.png'
    pix.save(str(png))
    scan = fitz.open()
    sp = scan.new_page()
    sp.insert_image(sp.rect, stream=pix.tobytes('png'))
    scan_path = Path(td) / 'scan.pdf'
    scan.save(scan_path)
    return png, scan_path


def main():
    result = {}
    with tempfile.TemporaryDirectory() as td:
        png, scan_path = render(td)

        result['header_ocr_available'] = vision_ocr.available()
        if result['header_ocr_available']:
            text, method = vision_ocr.recognize(str(png))
            compact = (text or '').replace(' ', '')
            result['header_ocr_method'] = method
            result['header_ocr_text'] = text
            result['header_ocr_passed'] = '发展和改革局' in compact

        pages, lines, text, *_ = parse_pdf(scan_path)
        result['page_ocr_method'] = pages[0]['text_method']
        result['page_ocr_text'] = text
        result['page_ocr_state'] = pages[0].get('text_state')
        # 'ocr-vision' 为 macOS Vision 兜底（当前通路），'ocr' 为旧 Tesseract 通路。
        result['page_ocr_passed'] = (pages[0]['text_method'] in ('ocr-vision', 'ocr')
                                     and '4702' in text and '2998' in text)

    result['passed'] = bool(result.get('header_ocr_passed') or result.get('page_ocr_passed'))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
