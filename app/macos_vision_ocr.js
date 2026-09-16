// macOS 原生 Vision OCR（零安装依赖，支持简体中文）
// 用法：osascript -l JavaScript macos_vision_ocr.js <图片路径>
// 输出：识别到的文本行，按视觉顺序以换行分隔
ObjC.import('Foundation');
ObjC.import('Vision');

function recognize(path) {
  const url = $.NSURL.fileURLWithPath($(path));
  const request = $.VNRecognizeTextRequest.alloc.init;
  request.recognitionLevel = $.VNRequestTextRecognitionLevelAccurate;
  request.recognitionLanguages = ['zh-Hans', 'en-US'];
  request.usesLanguageCorrection = true;

  const handler = $.VNImageRequestHandler.alloc.initWithURLOptions(url, $());
  handler.performRequestsError($([request]), $());

  const results = request.results;
  if (!results || results.count === 0) return '';
  const lines = [];
  for (let i = 0; i < results.count; i++) {
    const candidates = results.objectAtIndex(i).topCandidates(1);
    if (candidates.count > 0) lines.push(ObjC.unwrap(candidates.objectAtIndex(0).string));
  }
  return lines.join('\n');
}

function run(argv) {
  if (argv.length < 1) throw new Error('缺少图片路径参数');
  return recognize(argv[0]);
}
