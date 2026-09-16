"""公开批文回归集暴露的缺陷修复回归测试。

来源：47 份温州市瑞安市政府主动公开批复跑出的全量基线（见 outputs/公开批文回归集_建成与基线报告.md）。
每项修复对应基线里的一处缺陷，用合成 PDF 复刻触发条件，防止回归。
"""
import io, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import fitz
from PIL import Image
from app.extract import (extract, parse_pdf, ORG, header_band, refine_header_mark, INK_BLANK_RATIO)

# 合成一份「红头/标题为图片」的公文：正文从发文字号开始，标题不在文本层。
BODY = [
    '你单位《关于要求审批瑞安市安阳街道广场社区管网改造工程初步设计的申请报告》及相关附件收悉。',
    '我局于2026年7月21日发起会商，有关职能部门对该工程初步设计文本进行了审查并反馈意见。',
    '一、建设内容：新建污水管网1501m，截污纳管管道1250米，污水检查井58座。',
    '二、建设地点：项目位于瑞安市安阳街道广场社区。',
    '三、项目投资估算为1234.56万元。',
]


def _page(doc, lines, header_image=False, doc_number=True):
    page = doc.new_page()
    y = 60
    if header_image:
        buf = io.BytesIO()
        # 宽幅版头图片（实际政务站群输出的红头图约 3712x1313）；窄图不占版头，不应触发标记。
        Image.new('RGB', (800, 100), (200, 30, 30)).save(buf, 'PNG')
        page.insert_image(fitz.Rect(60, 60, 540, 140), stream=buf.getvalue())
        y = 320
    if doc_number:
        page.insert_text((80, y), '瑞发改投〔2026〕87号', fontname='china-s', fontsize=12)
        y += 30
    for text in lines:
        page.insert_text((80, y), text, fontname='china-s', fontsize=12)
        y += 28
    return page


def _pdf(td, lines, name='doc.pdf', header_image=False, doc_number=True):
    path = Path(td) / name
    doc = fitz.open()
    _page(doc, lines, header_image=header_image, doc_number=doc_number)
    doc.save(path)
    return path


class AdresseeTests(unittest.TestCase):
    """收件人（主送机关）抽取：位置锚点 + 后缀表。"""

    def _extract(self, addressee):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, [addressee, *BODY])
            return extract(p, p.name, '0' * 32, False)

    def test_addressee_after_doc_number_not_at_offset_zero(self):
        # 正文首行是发文字号，收件人不在偏移 0；按标题偏移或 re.match(text[0:]) 会失配。
        r = self._extract('瑞安市安阳街道办事处：')
        c = r['fields']['项目单位']
        self.assertEqual(c['value'], '瑞安市安阳街道办事处')
        self.assertEqual(c['method'], 'addressee')
        self.assertEqual(c['status'], 'needs_review')  # 收件人=项目单位属角色推断
        self.assertEqual(c['evidence'][0]['page'], 1)
        self.assertTrue(c['evidence'][0]['bbox'])  # 高亮定位需要 bbox

    def test_addressee_without_doc_number_still_found(self):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市上望街道办事处：', *BODY], doc_number=False)
            r = extract(p, p.name, '0' * 32, False)
            self.assertEqual(r['fields']['项目单位']['value'], '瑞安市上望街道办事处')

    def test_addressee_suffix_middle_school(self):
        self.assertEqual(self._extract('瑞安市塘下镇鲍田中学：')['fields']['项目单位']['value'], '瑞安市塘下镇鲍田中学')

    def test_addressee_suffix_peoples_court(self):
        self.assertEqual(self._extract('瑞安市人民法院：')['fields']['项目单位']['value'], '瑞安市人民法院')

    def test_addressee_not_confused_with_body_org(self):
        # 正文里出现的建设单位不应被当成收件人。
        r = self._extract('瑞安市上望街道办事处：')
        self.assertNotIn('编制单位', r['fields']['项目单位']['value'])


class FilenameFallbackTests(unittest.TestCase):
    """红头与标题为图片层的公文：标题从附件名兜底，并据此判定阶段。"""

    NAME = '瑞发改投（2026）87号（关于瑞安市安阳街道广场社区管网改造工程初步设计的批复）.pdf'

    def _run(self):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市安阳街道办事处：', *BODY], name=self.NAME, header_image=True)
            return extract(p, p.name, '0' * 32, False)

    def test_title_falls_back_to_filename(self):
        c = self._run()['fields']['标题']
        self.assertEqual(c['value'], '关于瑞安市安阳街道广场社区管网改造工程初步设计的批复')
        self.assertEqual(c['method'], 'filename')
        self.assertEqual(c['status'], 'needs_review')  # 非原文直取，必须可复核

    def test_stage_recovered_from_fallback_title(self):
        self.assertEqual(self._run()['stage'], '初步设计')

    def test_project_name_flagged_not_silently_extracted(self):
        c = self._run()['fields']['项目名称']
        self.assertEqual(c['value'], '瑞安市安阳街道广场社区管网改造工程')
        self.assertEqual(c['status'], 'needs_review')  # 与项目单位同一口径，不再一个标一个不标
        self.assertIn(c['method'], ('title-inference', 'filename'))

    def test_approval_type_title_yields_project_name(self):
        # 企业投资项目「核准」类批复标题不含三种阶段词，也必须能推出项目名称。
        name = '瑞发改投（2026）28号（关于瑞安市江南片区天然气门站建设项目核准的批复）.pdf'
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市发展和改革局：', *BODY], name=name, header_image=True)
            c = extract(p, p.name, '0' * 32, False)['fields']['项目名称']
            self.assertEqual(c['value'], '瑞安市江南片区天然气门站建设项目')


class InvestmentBasisTests(unittest.TestCase):
    """投资口径词表：地方批复主流的「投资估算」写法。"""

    def _value(self, text):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市安阳街道办事处：', text, *BODY])
            return extract(p, p.name, '0' * 32, False)['fields']['总投资/匡算/估算/概算']['value']

    def test_investment_estimate_phrase(self):
        self.assertEqual(self._value('四、项目投资估算为1234.56万元。'), '项目投资估算为1234.56万元')

    def test_estimate_total_investment_phrase(self):
        self.assertEqual(self._value('四、项目估算总投资2998万元。'), '项目估算总投资2998万元')

    def test_approval_capital_phrase(self):
        self.assertEqual(self._value('四、项目概算总投资2975.73万元。'), '项目概算总投资2975.73万元')


class MetricUnitTests(unittest.TestCase):
    """建设指标：市政管网量词与小写单位。"""

    def _metrics(self, lines):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市安阳街道办事处：', *lines])
            return extract(p, p.name, '0' * 32, False)['metrics']

    def test_lowercase_metre_unit(self):
        values = [m['value'] for m in self._metrics(['一、建设内容：新建污水管网1501m。'])]
        self.assertIn('1501m', values)

    def test_pipeline_metrics_not_empty(self):
        values = [m['value'] for m in self._metrics(['一、建设内容：新建截污纳管管道1250米，污水检查井58座。'])]
        self.assertTrue(values, '市政管网类批复的建设指标不应为空')
        self.assertIn('1250米', values)

    def test_road_metrics_still_work(self):
        values = [m['value'] for m in self._metrics(['一、建设内容：道路全长4344米，路基宽度36米。'])]
        self.assertIn('4344米', values)
        self.assertIn('36米', values)

    def test_mileage_label(self):
        values = [m['value'] for m in self._metrics(['一、建设内容：实施总里程为5.374km。'])]
        self.assertIn('5.374km', values)

    def test_short_route_length_label(self):
        values = [m['value'] for m in self._metrics(['一、建设内容：路线长100.37m，其中新建道路97.43m。'])]
        self.assertIn('100.37m', values)

    def test_label_with_spec_number_does_not_block_metric(self):
        # 标签与数值之间夹规格号（管径De400长度3700米）不应导致漏抽。
        metrics = self._metrics(['一、建设内容：建设天然气中压管道，管径De400长度3700米。'])
        self.assertIn('3700米', [m['value'] for m in metrics])
        self.assertEqual([m['name'] for m in metrics if m['value'] == '3700米'], ['长度'])


class DurationTests(unittest.TestCase):
    """建设周期：除"建设工期为"，地方批复也用"工程实施周期24个月"等表述。"""

    def _value(self, text):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市安阳街道办事处：', text])
            return extract(p, p.name, '0' * 32, False)['fields']['建设周期']['value']

    def test_implementation_cycle_phrase(self):
        self.assertEqual(self._value('七、建设工期：工程实施周期24个月。'), '24个月')

    def test_classic_construction_period_phrase(self):
        self.assertEqual(self._value('七、建设工期：项目建设工期为14个月。'), '14个月')

    def test_absent_duration_stays_empty(self):
        # 多数批复不含工期，此字段应老实留空，不得借用别的数字。
        self.assertIsNone(self._value('一、建设内容：新建污水管道1501m。'))


class HeaderOcrTests(unittest.TestCase):
    """版头区定向 OCR：红头与标题在图片层时，从版头带读出（而非只靠附件名推断）。"""

    # 真实 OCR 输出形态：含浙江政务服务网水印、被拆成两行的标题、以及识别噪声行。
    OCR_TEXT = ('浙江政务服务网\n投资在线平台\n瑞安市发展和改革局文件\n工程审批系统\n'
                '瑞发改投〔2026〕87号\n关于瑞安市安阳街道广场社区管网改造工程\n初步设计的批复\n合嘛\n')

    def _run(self, name='doc.pdf', ocr=None, ocr_returns=None):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市安阳街道办事处：', *BODY], name=name, header_image=True)
            with patch('app.extract.vision_ocr.recognize', return_value=ocr_returns), patch('app.extract.vision_ocr.available', return_value=True), patch.object(fitz.Page, 'get_textpage_ocr', side_effect=RuntimeError('exercise fallback')):
                return extract(p, p.name, '0' * 32, False)

    def test_ocr_recovers_header_mark_text(self):
        r = self._run(ocr_returns=(self.OCR_TEXT, 'macos-vision'))
        c = r['fields']['发文机关标志']
        self.assertEqual(c['value'], '瑞安市发展和改革局文件')  # 此前该字段覆盖率 0%
        self.assertEqual(c['method'], 'macos-vision')
        self.assertEqual(c['status'], 'needs_review')  # OCR 结果必须可复核
        self.assertEqual(c['evidence'][0]['page'], 1)
        self.assertTrue(c['evidence'][0]['bbox'])

    def test_ocr_joins_wrapped_title_lines(self):
        r = self._run(ocr_returns=(self.OCR_TEXT, 'macos-vision'))
        self.assertEqual(r['fields']['标题']['value'],
                         '关于瑞安市安阳街道广场社区管网改造工程初步设计的批复')

    def test_watermark_filtered_out(self):
        r = self._run(ocr_returns=(self.OCR_TEXT, 'macos-vision'))
        for noise in ['浙江政务服务网', '投资在线平台', '工程审批系统']:
            self.assertNotIn(noise, r['fields']['标题']['value'])

    def test_generic_filename_uses_ocr_title(self):
        # 附件名是通用名（"初步设计批复文件.pdf"）时不含标题，OCR 是唯一来源。
        r = self._run(name='初步设计批复文件.pdf', ocr_returns=(self.OCR_TEXT, 'macos-vision'))
        self.assertEqual(r['fields']['标题']['method'], 'macos-vision')
        self.assertIn('安阳街道广场社区管网改造工程', r['fields']['标题']['value'])

    def test_disagreement_prefers_filename_and_warns(self):
        # OCR 有识别误差（如「项目建议书」→「项日建议书」）。附件名是无损文本，
        # 不一致时采信附件名，但必须把分歧记成告警暴露出来。
        noisy = self.OCR_TEXT.replace('初步设计', '初浅设计')
        name = '瑞发改投（2026）87号（关于瑞安市安阳街道广场社区管网改造工程初步设计的批复）.pdf'
        r = self._run(name=name, ocr_returns=(noisy, 'macos-vision'))
        self.assertEqual(r['fields']['标题']['method'], 'filename')
        self.assertEqual(r['fields']['标题']['value'],
                         '关于瑞安市安阳街道广场社区管网改造工程初步设计的批复')
        self.assertEqual(r['stage'], '初步设计')
        self.assertTrue(any('OCR' in w and '不一致' in w for w in r['warnings']))

    def test_agreement_yields_no_warning(self):
        name = '瑞发改投（2026）87号（关于瑞安市安阳街道广场社区管网改造工程初步设计的批复）.pdf'
        r = self._run(name=name, ocr_returns=(self.OCR_TEXT, 'macos-vision'))
        self.assertFalse(any('不一致' in w for w in r['warnings']))

    def test_ocr_still_supplies_header_mark_when_title_from_filename(self):
        name = '瑞发改投（2026）87号（关于瑞安市安阳街道广场社区管网改造工程初步设计的批复）.pdf'
        r = self._run(name=name, ocr_returns=(self.OCR_TEXT, 'macos-vision'))
        c = r['fields']['发文机关标志']  # 红头只有 OCR 一个来源，不受标题来源影响
        self.assertEqual(c['value'], '瑞安市发展和改革局文件')
        self.assertEqual(c['method'], 'macos-vision')

    def test_ocr_recovers_stage(self):
        r = self._run(ocr_returns=(self.OCR_TEXT, 'macos-vision'))
        self.assertEqual(r['stage'], '初步设计')

    def test_filename_fallback_when_ocr_yields_nothing(self):
        name = '瑞发改投（2026）87号（关于瑞安市安阳街道广场社区管网改造工程初步设计的批复）.pdf'
        r = self._run(name=name, ocr_returns=(None, None))
        self.assertEqual(r['fields']['标题']['method'], 'filename')
        self.assertEqual(r['fields']['发文机关标志']['status'], 'needs_review')
        self.assertEqual(r['fields']['发文机关标志']['method'], 'vision-required')


class HeaderBandTests(unittest.TestCase):
    """版头带定位：必须锚定结构元素（发文字号/主送机关），不能用页高比例。

    回归背景：第一版按"页高 40% 上半区"扫主送机关，命中 0/47——这类公文顶部红头是图片，
    正文可晚至页高 61% 处才起始，被固定比例整段截掉。
    """

    def _page(self, td):
        doc = fitz.open()
        page = doc.new_page()
        buf = io.BytesIO()
        Image.new('RGB', (800, 100), (200, 30, 30)).save(buf, 'PNG')
        page.insert_image(fitz.Rect(60, 60, 540, 140), stream=buf.getvalue())
        # 正文从页高约 61%（y=515）处才起始，远超常见的 40% 阈值。
        page.insert_text((80, 515), '瑞安市安阳街道办事处：', fontname='china-s', fontsize=12)
        for i, text in enumerate(BODY):
            page.insert_text((80, 545 + i * 28), text, fontname='china-s', fontsize=12)
        path = Path(td) / 'late-body.pdf'
        doc.save(path)
        return path

    def test_band_reaches_below_40_percent(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._page(td)
            pages, lines, text, offsets, _ = parse_pdf(path)
            band = header_band(pages, offsets)
            self.assertGreater(band.y1, pages[0]['height'] * 0.5)  # 越过 40%/50% 比例
            self.assertAlmostEqual(band.y1, 515, delta=12)  # 恰好在主送机关顶部

    def test_addressee_found_when_body_starts_late(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._page(td)
            r = extract(path, 'doc.pdf', '0' * 32, False)
            self.assertEqual(r['fields']['项目单位']['value'], '瑞安市安阳街道办事处')


class HeaderMarkValidationTests(unittest.TestCase):
    """OCR 红头校验：用文本层印发机关截去水印残留，对不上则告警而非擅改。"""

    def test_trims_watermark_prefix(self):
        mark, note = refine_header_mark('龙资在线平台瑞安市发展和改革局文件', '瑞安市发展和改革局办公室')
        self.assertEqual(mark, '瑞安市发展和改革局文件')
        self.assertIn('截去识别噪声', note)

    def test_consistent_value_untouched(self):
        mark, note = refine_header_mark('瑞安市发展和改革局文件', '瑞安市发展和改革局办公室')
        self.assertEqual(mark, '瑞安市发展和改革局文件')
        self.assertIsNone(note)

    def test_mismatch_flagged_not_rewritten(self):
        mark, note = refine_header_mark('龙泉市发展和改革局文件', '龙泉市财政局办公室')
        self.assertEqual(mark, '龙泉市发展和改革局文件')  # 不擅改
        self.assertIn('不一致', note)

    def test_no_imprint_no_trim(self):
        self.assertEqual(refine_header_mark('龙资在线平台瑞安市发展和改革局文件', None)[0],
                         '龙资在线平台瑞安市发展和改革局文件')

    def _merged_line_doc(self, td, merged):
        doc = fitz.open()
        page = doc.new_page()
        buf = io.BytesIO()
        Image.new('RGB', (800, 100), (200, 30, 30)).save(buf, 'PNG')
        page.insert_image(fitz.Rect(60, 60, 540, 140), stream=buf.getvalue())
        for i, text in enumerate(['瑞安市安阳街道办事处：', *BODY]):
            page.insert_text((80, 300 + i * 28), text, fontname='china-s', fontsize=12)
        page.insert_text((420, 760), '瑞安市发展和改革局办公室', fontname='china-s', fontsize=10)  # 版记
        path = Path(td) / 'doc.pdf'
        doc.save(path)
        ocr = '浙江政务服务网\n%s\n关于瑞安市安阳街道广场社区管网改造工程初步设计的批复\n' % merged
        with patch('app.extract.vision_ocr.recognize', return_value=(ocr, 'macos-vision')), patch('app.extract.vision_ocr.available', return_value=True), patch.object(fitz.Page, 'get_textpage_ocr', side_effect=RuntimeError('exercise fallback')):
            return extract(path, 'doc.pdf', '0' * 32, False)

    def test_first_line_of_defence_strips_known_watermark(self):
        # 第一道防线：行内去噪认得水印关键词，直接剥掉，不必惊动人工。
        with tempfile.TemporaryDirectory() as td:
            r = self._merged_line_doc(td, '龙资在线平台瑞安市发展和改革局文件')
        self.assertEqual(r['fields']['发文机关标志']['value'], '瑞安市发展和改革局文件')
        self.assertFalse(any('截去识别噪声' in w for w in r['warnings']))

    def test_second_line_of_defence_trims_unknown_noise(self):
        # 第二道防线：OCR 把水印误读成词表之外的字样时，用印发机关校验并截断。
        with tempfile.TemporaryDirectory() as td:
            r = self._merged_line_doc(td, '龙资服务大厅瑞安市发展和改革局文件')
        self.assertEqual(r['fields']['发文机关标志']['value'], '瑞安市发展和改革局文件')
        self.assertEqual(r['fields']['印发机关']['value'], '瑞安市发展和改革局办公室')
        self.assertTrue(any('截去识别噪声' in w for w in r['warnings']))


class WholePageOcrFallbackTests(unittest.TestCase):
    """整页无文本层时的回退：区分「真空白页」与「有内容但识别不出」。

    实测校准：真实空白页（仅版式水印）墨迹占比 8e-05，正常内容页 0.041–0.054，
    阈值 INK_BLANK_RATIO=0.004 居中——空白页不再报"需复核"，扫描页仍报。
    """

    def _blank(self, td):
        path = Path(td) / 'blank.pdf'
        doc = fitz.open()
        doc.new_page()
        doc.save(path)
        return path

    def _inked(self, td):
        path = Path(td) / 'inked.pdf'
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(80, 200, 520, 700), fill=(0, 0, 0))  # 有墨迹但无文字
        doc.save(path)
        return path

    def test_blank_page_marked_blank_without_alarm(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._blank(td)
            with patch('app.extract.vision_ocr.recognize', return_value=(None, None)), patch('app.extract.vision_ocr.available', return_value=True), patch.object(fitz.Page, 'get_textpage_ocr', side_effect=RuntimeError('exercise fallback')):
                pages, lines, text, offsets, warnings = parse_pdf(path)
        self.assertEqual(pages[0]['text_state'], 'blank')
        self.assertFalse(any('需复核' in w for w in warnings))

    def test_inked_page_without_text_still_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._inked(td)
            with patch('app.extract.vision_ocr.recognize', return_value=(None, None)), patch('app.extract.vision_ocr.available', return_value=True), patch.object(fitz.Page, 'get_textpage_ocr', side_effect=RuntimeError('exercise fallback')):
                pages, lines, text, offsets, warnings = parse_pdf(path)
        self.assertEqual(pages[0]['text_state'], 'unreadable')
        self.assertTrue(any('需复核' in w for w in warnings))

    def test_ocr_supplied_text_is_used(self):
        # 必须用**有墨迹**的页面：空白页即便 OCR 返回文本也应判空白并丢弃（见
        # test_agent_loop.BlankBeforeOcrTests），用空白页测这条会自相矛盾。
        scanned = ('浙江政务服务网\n瑞安市发展和改革局\n项目总投资2998万元\n项目建设工期为24个月\n')
        with tempfile.TemporaryDirectory() as td:
            path = self._inked(td)
            with patch('app.extract.vision_ocr.recognize', return_value=(scanned, 'macos-vision')), patch('app.extract.vision_ocr.available', return_value=True), patch.object(fitz.Page, 'get_textpage_ocr', side_effect=RuntimeError('exercise fallback')):
                pages, lines, text, offsets, warnings = parse_pdf(path)
        self.assertEqual(pages[0]['text_method'], 'ocr-vision')
        self.assertEqual(pages[0]['text_state'], 'readable')
        self.assertIn('2998', text)
        self.assertNotIn('浙江政务服务网', text)  # 水印须过滤

    def test_unavailable_backend_keeps_warning(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._blank(td)
            with patch('app.extract.vision_ocr.available', return_value=False):
                pages, lines, text, offsets, warnings = parse_pdf(path)
        self.assertEqual(pages[0]['text_state'], 'unreadable')
        self.assertTrue(any('需复核' in w for w in warnings))

    def test_blank_page_does_not_downgrade_fields(self):
        # 空白页属"确实没有内容"，不应把整份文档的空字段降级为 uncertain。
        with tempfile.TemporaryDirectory() as td:
            path = self._blank(td)
            with patch('app.extract.vision_ocr.recognize', return_value=(None, None)), patch('app.extract.vision_ocr.available', return_value=True), patch.object(fitz.Page, 'get_textpage_ocr', side_effect=RuntimeError('exercise fallback')):
                r = extract(path, path.name, '0' * 32, False)
        self.assertEqual(r['fields']['总投资/匡算/估算/概算']['status'], 'missing')

    def test_ink_threshold_sits_between_measured_populations(self):
        # 守住阈值：真实空白页约 8e-05、内容页约 0.04–0.054，阈值必须在两者之间。
        self.assertGreater(INK_BLANK_RATIO, 8e-05 * 10)
        self.assertLess(INK_BLANK_RATIO, 0.04 / 5)


class HeaderImageTests(unittest.TestCase):
    """发文机关标志：红头是图片时不得静默留空。"""

    def _field(self, header_image):
        with tempfile.TemporaryDirectory() as td:
            p = _pdf(td, ['瑞安市安阳街道办事处：', *BODY], header_image=header_image)
            r = extract(p, p.name, '0' * 32, False)
            return r['fields']['发文机关标志'], r['warnings']

    def test_image_header_marked_for_vision_review(self):
        c, warnings = self._field(True)
        self.assertIsNone(c['value'])
        self.assertEqual(c['status'], 'needs_review')
        self.assertEqual(c['method'], 'vision-required')
        self.assertTrue(any('视觉模型' in w for w in warnings))

    def test_no_false_alarm_without_header_image(self):
        # 没有版头图片时，抽不到就应老实标 missing，不能一律报"需视觉复核"。
        c, _ = self._field(False)
        self.assertEqual(c['status'], 'missing')

    def test_text_header_still_extracted(self):
        with tempfile.TemporaryDirectory() as td:
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((80, 60), '瑞安市发展和改革局文件', fontname='china-s', fontsize=12)
            page.insert_text((80, 100), '瑞发改投〔2026〕87号', fontname='china-s', fontsize=12)
            for i, t in enumerate(['瑞安市安阳街道办事处：', *BODY]):
                page.insert_text((80, 140 + i * 28), t, fontname='china-s', fontsize=12)
            p = Path(td) / 'text-header.pdf'
            doc.save(p)
            c = extract(p, p.name, '0' * 32, False)['fields']['发文机关标志']
            self.assertEqual(c['value'], '瑞安市发展和改革局文件')
            self.assertEqual(c['status'], 'extracted')


class OrgSuffixTests(unittest.TestCase):
    """后缀表本身的最小保证。"""

    def test_expected_suffixes_present(self):
        for suffix in ['街道办事处', '管委会', '人民政府', '中学', '法院', '医院', '学校']:
            self.assertTrue(any(suffix in alt for alt in ORG.split('|')), suffix)


if __name__ == '__main__':
    unittest.main()
