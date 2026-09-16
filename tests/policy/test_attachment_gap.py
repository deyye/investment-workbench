"""附件"材料缺口"判据（`quality.is_attachment_gap`）。

口径：**没拿到可用正文**才算缺口。两个易错点各留一条回归：

- `partial`（已出文本，只是转换过程留了提示）不算缺口；
- **打包件**在同条目其他附件已给出正文时不算缺口——湖北每篇都带一个 `<id>.zip`，
  解开就是同页那些 wps/pdf，我们不解压它，但材料并不缺。

这两条都属于"把可用材料当成缺失"：判据一严，明明齐了的材料会被报成有缺口，
站点也就从"接通"变成"验收不通过"。
"""
from policy_collector.quality import (count_attachment_failures, count_attachment_gaps,
                                      is_attachment_gap)


def att(name, status, text="", sha="x", url=""):
    return {"name": name, "parse_status": status, "parsed_text": text,
            "sha256": sha, "url": url or f"https://a.gov.cn/{name}"}


def test_partial_with_text_is_not_a_gap():
    assert is_attachment_gap(att("a.wps", "partial", "正文" * 100)) is False


def test_pack_is_not_a_gap_when_a_sibling_has_text():
    pack = att("6006470.zip", "unsupported", "")
    sibs = [pack, att("1.wps", "partial", "六万字正文")]
    assert is_attachment_gap(pack, sibs) is False
    assert count_attachment_gaps(sibs) == 0


def test_pack_is_not_a_gap_even_alone():
    """打包件不是独立材料，而是"本条内容的打包下载"——单独出现也不算缺口。

    实测依据（湖北 `hubei_zcwj`，逐个拆开核对）：zip 里是正文的 PDF 版
    （成品油调价）、同页其他附件的副本（招标文件），甚至有空包。
    """
    pack = att("only.zip", "unsupported", "")
    assert is_attachment_gap(pack, [pack]) is False
    assert count_attachment_gaps([pack]) == 0


def test_pack_does_not_mask_other_gaps():
    """豁免只作用于打包件本身，同一行里别的真缺口照数。"""
    pack = att("x.zip", "unsupported", "")
    real = att("a.pdf", "needs_ocr", "")
    assert is_attachment_gap(pack, [pack, real]) is False
    assert is_attachment_gap(real, [pack, real]) is True
    assert count_attachment_gaps([pack, real]) == 1


def test_undownloaded_is_failure_not_gap():
    """没下下来的归 attachments_failed，不能在 unparsed 里重复计一次。"""
    a = att("a.pdf", "failed", "", sha="")
    assert is_attachment_gap(a, [a]) is False
    assert count_attachment_failures([a]) == 1
    assert count_attachment_gaps([a]) == 0


def test_plain_missing_text_is_a_gap():
    a = att("a.pdf", "needs_ocr", "")
    assert is_attachment_gap(a, [a]) is True
    assert count_attachment_gaps([a]) == 1
