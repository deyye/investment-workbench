"""外省站点只读探测：为「扩展更多省份」判断列表机制与可接入性（不发请求以外动作）。

用法:
  python scripts/probe_sources.py <首页URL> [更多首页URL...]

对每个首页做 2 层只读探测并输出接入建议：
1) 首页连通性/编码/框架；2) 站点内的「政策/文件/公开/通知公告」栏目候选链接；
3) 对候选栏目 URL 做一次 GET，判定列表机制：
   - html-static   页面含真实详情链接 → ListPageParser 可直接用
   - recordset     script 内嵌 <datastore><recordset> XML(CDATA) → ListPageParser 通用解析(江苏模式)
   - unitbuild     hanweb「页面构建单元」动态接口 → list_format: zj_unit(浙江模式)
   - js-render     列表需前端 JS/模板渲染 → 需另行适配数据接口
   - shell/blocked 空壳/验证页/被拦截 → 需人工核实

判定结论写建议 config 字段(include/detail_url_pattern/pagination/list_format)。
探测只做 GET，不下载附件、不执行脚本、不写库。
"""
import re
import sys
import urllib.request
import ssl

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/126.0 Safari/537.36',
      'Accept-Language': 'zh-CN,zh;q=0.9'}
CTX = ssl.create_default_context()


def get(url: str, timeout: int = 15) -> tuple[str, str]:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.geturl(), r.read().decode('utf-8', 'ignore')


def column_candidates(text: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, re.S):
        href, t = m.group(1).strip(), re.sub(r'<[^>]+>|\s+', ' ', m.group(2)).strip()
        if not href.startswith(('http', '/')) or 'javascript' in href:
            continue
        if not re.search(r'规范性文件|政策文件|政策法规|通知公告|文件库|zcwj|guifan|tzgg', href + t):
            continue
        key = href.split('?')[0]
        if key not in seen and len(t) >= 3:
            seen.add(key)
            out.append((t[:30], href))
    return out


def classify_list(url: str) -> tuple[str, str]:
    try:
        final, html = get(url)
    except Exception as e:
        return 'blocked', f'{type(e).__name__}: {e}'
    if len(html) < 2000:
        return 'shell', '页面过小(空壳/验证/JS 壳)'
    if re.search(r'AuthorizedRead[^"\']*unitbuild', html, re.I) or 'unitbuild.js' in html:
        return 'unitbuild', 'hanweb 页面构建单元，list_format=zj_unit'
    if '<recordset>' in html and '<a ' in html:
        return 'recordset', 'TRS jpage script 内嵌 XML(CDATA) 记录，ListPageParser 通用可解析'
    anchors = re.findall(r'href=["\']([^"\']+)["\']', html)
    detail = [h for h in anchors if re.search(r'/(?:art|content|detail|info|t\d{8}_\d+|20\d{2}/)', h)]
    if len(set(detail)) >= 5:
        return 'html-static', f'静态列表直出({len(set(detail))} 个详情链接)'
    return 'js-render', '列表需前端 JS/模板渲染，需另行适配'


def probe(home: str) -> None:
    print(f'\n{"=" * 70}\n探测 {home}')
    try:
        final, html = get(home)
    except Exception as e:
        print(f'  首页失败: {type(e).__name__}: {e}')
        return
    print(f'  首页 {final} ({len(html)} 字符)')
    cands = column_candidates(html)
    if not cands:
        print('  未发现政策/通知栏目候选(可能 JS 导航)，建议人工核实栏目 URL 后直接探测栏目页')
        return
    for t, href in cands[:8]:
        url = urllib.request.urljoin(final, href)
        kind, why = classify_list(url)
        print(f'  栏目 [{t}] {url}\n        → {kind}: {why}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for u in sys.argv[1:]:
        probe(u)
