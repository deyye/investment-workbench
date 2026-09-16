"""Bounded local document parsers. OCR/conversion never sends documents to a service."""
from __future__ import annotations
import io
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
from lxml import etree

PARSER_VERSION = 'attachments-v3'

# 永久性下载失败：站点明确表示"该文件不存在"，重试到天荒地老也不会成功。
# 必须与暂时性失败（超时、连接重置、5xx）区分开——后者才值得机器重试。
# 实测：浙江省发改委某文件的附件确实已从站点移除（HTTP 404），
# 但另一附件完好（HTTP 200 / 22106 字节）；若不区分，该政策会被永久钉在
# "待补材料"队列里空转，队列再也排不空。
_PERMANENT_DOWNLOAD_ERROR = re.compile(r'HTTP\s*4(?:04|10)\b|not\s+found', re.I)


def is_permanent_download_error(err) -> bool:
    """该附件下载失败是否属永久性（文件已不存在，重试无意义）。"""
    return bool(err) and bool(_PERMANENT_DOWNLOAD_ERROR.search(str(err)))


def detect_format(data, declared):
    if data.startswith(b'%PDF-'): return 'pdf'
    if data.startswith(b'PK'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if 'word/document.xml' in z.namelist(): return 'docx'
                if 'OFD.xml' in z.namelist(): return 'ofd'
        except zipfile.BadZipFile: pass
    return declared


def pdf_text(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if not 0 < len(reader.pages) <= 500: raise ValueError('PDF页数为空或超过500页')
    pages, issues, methods = [], [], set()
    ocr_limit = max(0, min(100, int(os.getenv('POLICY_OCR_MAX_PAGES', '20'))))
    attempted = 0
    for number, page in enumerate(reader.pages, 1):
        try: text = page.extract_text() or ''
        except Exception: text = ''
        if len(text.strip()) < 40:
            if attempted < ocr_limit and shutil.which('pdftoppm') and shutil.which('tesseract'):
                attempted += 1
                try:
                    ocr = ocr_pdf_page(data, number)
                    if len(ocr.strip()) > len(text.strip()): text = ocr; methods.add('ocr')
                except (OSError, subprocess.SubprocessError):
                    issues.append(f'第{number}页OCR失败或缺少语言包')
            if len(text.strip()) < 40: issues.append(f'第{number}页文本不足，需OCR/人工核对')
        else: methods.add('text')
        pages.append(text)
    if 'ocr' in methods: issues.append('含OCR识别内容，需人工核对识别准确性')
    return ('\n\n'.join(f'[第{i+1}页]\n{t}' for i,t in enumerate(pages) if t.strip()),
            '；'.join(issues), len(pages), sum(len(t.strip()) >= 40 for t in pages), '+'.join(sorted(methods)) or 'pdf')


@lru_cache(maxsize=1)
def ocr_languages():
    result=subprocess.run(['tesseract','--list-langs'],check=True,capture_output=True,timeout=10)
    return set(result.stdout.decode('utf-8').splitlines()[1:])


def ocr_pdf_page(data, number):
    requested=os.getenv('POLICY_OCR_LANG','chi_sim+eng')
    if not set(requested.split('+')).issubset(ocr_languages()):
        raise OSError('OCR缺少指定语言包')
    with tempfile.TemporaryDirectory(prefix='policy-ocr-') as directory:
        root=Path(directory); source=root/'input.pdf';source.write_bytes(data)
        subprocess.run(['pdftoppm','-f',str(number),'-l',str(number),'-singlefile','-scale-to','2400',
                        '-png',str(source),str(root/'page')],check=True,capture_output=True,timeout=45)
        result=subprocess.run(['tesseract',str(root/'page.png'),'stdout','-l',
                               requested],check=True,capture_output=True,timeout=60)
        return result.stdout.decode('utf-8').strip()


def legacy_to_pdf(data, fmt):
    binary=shutil.which('libreoffice') or shutil.which('soffice')
    if not binary: raise ValueError('需安装LibreOffice以转换旧Word/WPS')
    with tempfile.TemporaryDirectory(prefix='policy-office-') as directory:
        root=Path(directory);source=root/('input.'+fmt);source.write_bytes(data)
        profile=root/'profile';(profile/'user').mkdir(parents=True)
        (profile/'user/registrymodifications.xcu').write_text('''<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>''')
        subprocess.run([binary,'-env:UserInstallation='+profile.as_uri(),'--headless','--nologo','--nodefault',
                        '--norestore','--convert-to','pdf','--outdir',str(root),str(source)],
                       check=True,capture_output=True,timeout=90)
        output=root/'input.pdf'
        if not output.exists() or output.stat().st_size > 100*1024*1024: raise ValueError('Office转换未生成可用PDF')
        return output.read_bytes()


def xlsx_text(data):
    """提取 xlsx 单元格文本（含共享字符串表）。

    用于「规范性文件目录」这类以 Excel 附表发布的政策清单——正文往往只有一句
    印发通知，实质内容（文件标题、文号、有效期）全在表里。
    只读不写、不计算公式，解压规模设上限。
    """
    NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > 200 * 1024 * 1024:
            raise ValueError('xlsx解压规模超过限制')
        names = set(z.namelist())
        parser = etree.XMLParser(resolve_entities=False, no_network=True)

        shared: list[str] = []
        if 'xl/sharedStrings.xml' in names:
            root = etree.fromstring(z.read('xl/sharedStrings.xml'), parser)
            for si in root:
                shared.append(''.join(si.itertext()).strip())

        sheets = sorted(n for n in names
                        if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))
        if not sheets:
            raise ValueError('xlsx未找到工作表')

        lines: list[str] = []
        for name in sheets:
            root = etree.fromstring(z.read(name), parser)
            for row in root.iter(f'{NS}row'):
                cells = []
                for c in row.iter(f'{NS}c'):
                    kind = c.get('t')
                    value = c.find(f'{NS}v')
                    inline = c.find(f'{NS}is')
                    if kind == 's' and value is not None and value.text is not None:
                        idx = int(value.text)
                        cells.append(shared[idx] if 0 <= idx < len(shared) else '')
                    elif inline is not None:
                        cells.append(''.join(inline.itertext()).strip())
                    elif value is not None and value.text:
                        cells.append(value.text.strip())
                line = ' | '.join(x for x in cells if x)
                if line.strip():
                    lines.append(line)
        if not lines:
            raise ValueError('xlsx未提取到单元格文本')
        # 第二个返回值语义是"问题提示"，不是统计信息——放统计会被上层当成解析失败。
        issue = f'含 {len(sheets)} 个工作表，需核对是否读全' if len(sheets) > 1 else ''
        return ('\n'.join(lines), issue, len(sheets), len(sheets), 'xlsx-text')


def textutil_text(data, fmt):
    """用 macOS 自带的 textutil 提取旧版 Word/WPS 文本（零安装回退方案）。

    .wps 的实际内容是 OLE 复合文档，与旧 .doc 同格式；textutil 按扩展名识别，
    故落盘时统一用 .doc 扩展名。实测两者均可正常提取，内容与 LibreOffice 一致。
    """
    if not shutil.which('textutil'):
        raise ValueError('缺少 textutil（仅 macOS 自带）')
    with tempfile.TemporaryDirectory(prefix='policy-office-') as directory:
        source = Path(directory) / 'input.doc'
        source.write_bytes(data)
        result = subprocess.run(['textutil', '-convert', 'txt', '-stdout', str(source)],
                                check=True, capture_output=True, timeout=90)
        text = result.stdout.decode('utf-8', errors='replace').strip()
    if not text:
        raise ValueError('textutil 未提取到文本')
    return (text, f'经 textutil 转换（原格式 {fmt}），需人工核对转换完整性',
            1, 1 if len(text) >= 40 else 0, 'textutil')


def legacy_text(data, fmt):
    """旧版 Word/WPS 文本提取：优先 LibreOffice，回退 macOS 自带 textutil。

    LibreOffice 无平台限制、转换质量更高；textutil 无需安装，适合 macOS 本机。
    两者都不可用时给出明确提示，不静默失败。
    """
    if shutil.which('libreoffice') or shutil.which('soffice'):
        return pdf_text(legacy_to_pdf(data, fmt))
    if sys.platform == 'darwin' and shutil.which('textutil'):
        return textutil_text(data, fmt)
    raise ValueError('需安装LibreOffice以转换旧Word/WPS（macOS 亦可依赖自带 textutil）')


def ofd_text(data):
    """Follow DocRoot/Pages order; do not mistake ZIP member order for page order."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if len(z.infolist()) > 10000 or sum(i.file_size for i in z.infolist()) > 200*1024*1024:
            raise ValueError('OFD解压规模超过限制')
        def xml(name):
            if name.startswith('/') or '..' in name.split('/'): raise ValueError('OFD内部路径无效')
            if z.getinfo(name).file_size > 20*1024*1024: raise ValueError('OFD页面XML过大')
            return etree.fromstring(z.read(name),etree.XMLParser(resolve_entities=False,no_network=True))
        def path(base, ref):
            value=posixpath.normpath(posixpath.join(posixpath.dirname(base),ref))
            if value.startswith('../') or value.startswith('/'): raise ValueError('OFD内部引用越界')
            return value
        manifest=xml('OFD.xml'); roots=manifest.xpath('//*[local-name()="DocRoot"]/text()')
        if not roots: raise ValueError('OFD缺少DocRoot')
        texts=[];issues=[];complete=0
        for docroot in roots:
            document=xml(docroot)
            # Shared templates can contain substantive text; flag rather than silently omit.
            templates=bool(document.xpath('//*[local-name()="TemplatePage"]'))
            pages=document.xpath('//*[local-name()="Pages"]/*[local-name()="Page"]')
            if len(pages)>500: raise ValueError('OFD超过500页')
            for p in pages:
                page=xml(path(docroot,p.attrib['BaseLoc']))
                fragments=page.xpath('//*[local-name()="TextCode"]/text()')
                text='\n'.join(t for t in fragments if t.strip()); number=len(texts)+1
                graphics=bool(page.xpath('//*[local-name()="ImageObject" or local-name()="CompositeObject" or local-name()="PathObject" or local-name()="Template"]'))
                if not text.strip() or graphics or templates:
                    issues.append(f'第{number}页含图形/模板或缺少文本，需渲染核对')
                else: complete+=1
                texts.append(text)
        if not texts: raise ValueError('OFD未找到页面')
        return ('\n\n'.join(f'[第{i+1}页]\n{t}' for i,t in enumerate(texts) if t.strip()),
                '；'.join(issues),len(texts),complete,'ofd-text')
