"use strict";

// 通达信沪深自选股文本：市场位(上海1/深圳0) + 六位代码，CRLF，无BOM。
// 北交所/其他市场尚未完成客户端格式核验，整批拒绝，避免静默漏导或误导。
function buildTdxEbk(records) {
  if (!Array.isArray(records) || records.length === 0) {
    throw new Error('当前筛选结果为空，请先选择需要导出的股票。');
  }
  const codes = [], seen = new Set(), unsupported = [];
  for (const row of records) {
    const symbol = row.symbol, exchange = row.market?.exchange;
    const validCode = typeof symbol === 'string' && /^\d{6}$/.test(symbol);
    const sh = validCode && exchange === 'sh' && /^(?:600|601|603|605|688|689|900)\d{3}$/.test(symbol);
    const sz = validCode && exchange === 'sz' && /^(?:000|001|002|003|300|301|200)\d{3}$/.test(symbol);
    if (!sh && !sz) { unsupported.push(String(symbol ?? '代码缺失')); continue; }
    const code = (sh ? '1' : '0') + symbol;
    if (!seen.has(code)) { seen.add(code); codes.push(code); }
  }
  if (unsupported.length) {
    throw new Error(`有 ${unsupported.length} 条记录的通达信市场格式尚未支持或代码无效（${unsupported.slice(0, 5).join('、')}${unsupported.length > 5 ? '…' : ''}）。请先筛选沪深股票后导出；本次未生成文件。`);
  }
  return {text: codes.join('\r\n') + '\r\n', count: codes.length};
}

if (typeof module !== 'undefined' && module.exports) module.exports = {buildTdxEbk};
