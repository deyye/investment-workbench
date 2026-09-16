"""发文字号书写形态、审批阶段分类的回归测试。

两个来源：

1. 发文字号。国标 GB/T 9704-2012 §7.2.5 要求年份用六角括号「〔〕」加半角数字，
   但现实里至少三种变形会出现：源 PDF 文本层直接写成半角「[]」（实测庆元一份可研
   批文即如此，值==原文，不是抽取改的）；版头为图片层时只能靠 OCR，而实测 macOS
   Vision **读不出六角括号**——公文 3 号字下把〔2025〕读成【2025〕（左右各错一个）；
   部分发布系统输出全角方头括号「【】」。旧写法只认〔[与〕]，这些形态一律失配，
   发文字号变空，并连带打坏版头带定位与项目单位锚点（同一行正则三处复用）。
   处理原则：**主字段留原文**（文号要与纸质件核对，改写写法会掩盖来源问题），
   另给归一化值只供机器比对。

2. 审批阶段。「项目核准的批复」原先归入「待确认」，把「不属于三阶段审批体系」和
   「判定不出来」混为一谈。核准/备案是企业投资项目的另一条轨道，不经
   建议书→可研→初设三关，单列成类。
"""
import re, tempfile, unittest
from pathlib import Path
import fitz
from app.extract import (extract, classify_stage, normalize_doc_number,
                         DOC_NUMBER_RE, STAGES)
from app.review import update
from app.queue import project_progress
from app.compare import rows_for
# 两种运行方式都要能用：`discover -s tests` 时 tests/ 在 sys.path 上，
# 而 `-m unittest tests.test_docnum_stage` 需要走包路径。
try:
    from tests.approval.test_v2 import document, cell
except ImportError:
    from tests.approval.test_v2 import document, cell

TITLE_CS = '关于瑞安市安阳街道广场社区管网改造工程初步设计的批复'
TITLE_LX = '关于龙泉北互通连接线工程项目立项申请的批复'
TITLE_HZ = '关于瑞安市马陶线中压燃气工程项目核准的批复'


def _pdf(td, doc_number, title='', name='doc.pdf', extra=()):
    """最小可用公文：发文字号行 + 标题行 + 正文。全部走文本层，不触发 OCR。"""
    path = Path(td) / name
    doc = fitz.open()
    page = doc.new_page()
    y = 70
    for line in [doc_number, title, '你单位报送的申请报告及相关附件收悉。', *extra]:
        if line:
            page.insert_text((80, y), line, fontname='china-s', fontsize=11)
        y += 30
    doc.save(path)
    doc.close()
    return path


class DocNumberFormatTests(unittest.TestCase):
    """发文字号：原文照留，另给归一化值；写法不符国标时提示。"""

    def _extract(self, number, title=TITLE_CS):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, number, title)
            return extract(p, p.name, '0' * 32, False)

    def test_every_bracket_form_matches_the_anchor_regex(self):
        # 这条正则同时给版头带定位与项目单位锚点用，失配的代价不只是字段为空。
        for s in ['瑞发改投〔2026〕87号', '瑞发改投[2026]87号', '瑞发改投［2026］87号',
                  '瑞发改投【2026】87号', '瑞发改投（2026）87号', '瑞发改投【2026〕87号']:
            self.assertRegex(s, DOC_NUMBER_RE, '未匹配：' + s)

    def test_canonical_number_kept_verbatim_without_warning(self):
        r = self._extract('瑞发改投〔2026〕87号')
        c = r['fields']['发文字号']
        self.assertEqual(c['value'], '瑞发改投〔2026〕87号')
        self.assertEqual(c['normalized'], '瑞发改投〔2026〕87号')
        self.assertFalse([w for w in r['warnings'] if '发文字号' in w])

    def test_halfwidth_brackets_kept_verbatim_but_flagged(self):
        # 源 PDF 文本层写成半角方括号——实测庆元一份可研批文就是这样。
        r = self._extract('庆发改投[2025]148号')
        c = r['fields']['发文字号']
        self.assertEqual(c['value'], '庆发改投[2025]148号', '原文不得被改写')
        self.assertEqual(c['normalized'], '庆发改投〔2025〕148号', '比对值应折成规范形')
        self.assertTrue([w for w in r['warnings'] if '发文字号' in w])

    def test_ocr_style_brackets_kept_verbatim_but_flagged(self):
        # macOS Vision 在公文 3 号字下读出的混合形态：左括号错、右括号对。
        r = self._extract('庆发改投【2025〕148号')
        c = r['fields']['发文字号']
        self.assertEqual(c['value'], '庆发改投【2025〕148号')
        self.assertEqual(c['normalized'], '庆发改投〔2025〕148号')
        self.assertTrue([w for w in r['warnings'] if '发文字号' in w])

    def test_fullwidth_digits_are_normalised(self):
        self.assertEqual(normalize_doc_number('瑞发改投〔２０２６〕８７号'), '瑞发改投〔2026〕87号')
        self.assertEqual(normalize_doc_number(' 瑞发改投 〔2026〕 87号 '), '瑞发改投〔2026〕87号')

    def test_text_without_a_doc_number_stays_missing(self):
        # 放宽括号不能变成放宽结构：没有「4 位年份 + 序号 + 号」就不算文号。
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, '本件无文号', TITLE_CS)
            r = extract(p, p.name, '0' * 32, False)
        self.assertIsNone(r['fields']['发文字号']['value'])

    def test_number_line_is_not_confused_with_a_year_in_the_body(self):
        # 括号放宽后（尤其加入圆括号）要防止把正文里的「（2026）」当成文号。
        # 判定靠 fullmatch：整行必须是「代字+括号年份+序号+号」的完整形态。
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, '瑞发改投〔2026〕87号', TITLE_CS,
                     extra=['会议于（2026）年第3次召开。', '（2026）年度计划已下达。'])
            r = extract(p, p.name, '0' * 32, False)
        self.assertEqual(r['fields']['发文字号']['value'], '瑞发改投〔2026〕87号')


class StageClassificationTests(unittest.TestCase):
    """阶段分类：政府投资项目三阶段 + 企业投资项目核准/备案 + 待确认。"""

    def test_the_three_approval_stages(self):
        self.assertEqual(classify_stage('关于某某项目初步设计的批复'), '初步设计')
        self.assertEqual(classify_stage('关于某某项目可行性研究报告的批复'), '可行性研究')
        self.assertEqual(classify_stage('关于某某项目项目建议书的批复'), '建议书/立项')

    def test_proposal_written_as_lixiang_is_the_same_stage(self):
        # 同一审批事项各地措辞不同：庆元写「项目建议书」，龙泉写「立项申请」。
        # 归不到一起，同一项目的三份批文就跨阶段对不上。
        self.assertEqual(classify_stage(TITLE_LX), '建议书/立项')

    def test_approval_category_is_its_own_stage_not_pending(self):
        # 企业投资项目核准不经过三关，不能混进「待确认」。
        self.assertEqual(classify_stage(TITLE_HZ), '核准/备案')
        self.assertEqual(classify_stage('关于某某项目备案的批复'), '核准/备案')

    def test_title_without_any_stage_word_stays_pending(self):
        self.assertEqual(classify_stage('关于瑞安市某某工程项目的批复'), '待确认')
        self.assertEqual(classify_stage(''), '待确认')

    def test_pending_is_last_in_the_stage_order(self):
        # compare.py 用 STAGES 下标排序，兜底下标取 len(STAGES)-1；若「待确认」不在末位，
        # 认不出的阶段会被排到已知阶段中间。
        self.assertEqual(STAGES[-1], '待确认')

    def test_lixiang_warning_still_emitted(self):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, '龙发改投资〔2022〕182号', TITLE_LX)
            r = extract(p, p.name, '0' * 32, False)
        self.assertEqual(r['stage'], '建议书/立项')
        self.assertTrue([w for w in r['warnings'] if '立项申请' in w])

    def test_approval_category_explains_why_it_is_separate(self):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, '瑞发改投〔2026〕91号', TITLE_HZ)
            r = extract(p, p.name, '0' * 32, False)
        self.assertEqual(r['stage'], '核准/备案')
        self.assertTrue([w for w in r['warnings'] if '企业投资项目' in w])
        self.assertFalse([w for w in r['warnings'] if '立项申请' in w], '不应串到立项那条提示')


class StageIntegrationTests(unittest.TestCase):
    """新增一档阶段后，人工确认、待办、对照排序三条链路的联动。"""

    def test_human_confirmation_accepts_the_new_stage(self):
        d = document()
        update(d, dict(kind='stage', name='审批阶段', value='核准/备案'))
        self.assertEqual(d['stage'], '核准/备案')

    def test_new_stage_does_not_ask_for_human_review(self):
        # 核准/备案是已定论的归类（标题明写核准/备案），不该像「待确认」那样占用人工作业。
        d = document(); d['stage'] = '核准/备案'
        p = project_progress([d], rows_for([d])[1])
        self.assertEqual(p['pending'], 0)
        self.assertFalse([i for i in p['documents'][0]['items'] if i['kind'] == 'stage'])

    def test_pending_stage_still_asks_for_human_review(self):
        d = document(); d['stage'] = '待确认'
        p = project_progress([d], rows_for([d])[1])
        self.assertEqual(p['pending'], 1)
        self.assertEqual(p['documents'][0]['items'][0]['kind'], 'stage')

    def test_unknown_stage_sorts_after_known_stages(self):
        known = document('a', stage='初步设计')
        unknown = document('b', stage='从未见过的阶段')
        docs, _ = rows_for([unknown, known])
        self.assertEqual([d['id'][0] for d in docs], ['a', 'b'])

    def test_approval_category_sorts_after_the_three_stages(self):
        cs = document('a', stage='初步设计')
        hz = document('b', stage='核准/备案')
        docs, _ = rows_for([hz, cs])
        self.assertEqual([d['id'][0] for d in docs], ['a', 'b'])


if __name__ == '__main__':
    unittest.main()
