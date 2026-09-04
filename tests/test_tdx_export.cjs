const {test} = require('node:test');
const assert = require('node:assert/strict');
const {buildTdxEbk} = require('../sequoia_x/reporting/tdx_export.js');
const row = (symbol, exchange) => ({symbol, market:{exchange}});

test('encodes market and preserves zeros, order, CRLF with no header or BOM', () => {
  const {text,count}=buildTdxEbk([row('000001','sz'),row('600000','sh'),row('688485','sh'),row('301234','sz')]);
  assert.equal(text,'0000001\r\n1600000\r\n1688485\r\n0301234\r\n');
  assert.equal(count,4);
  assert.equal(Buffer.from(text).length,36);
  assert.ok([...Buffer.from(text)].every(b=>b<128));
});
test('includes Shanghai/Shenzhen B shares with correct market',()=>{
  assert.equal(buildTdxEbk([row('900901','sh'),row('200002','sz')]).text,'1900901\r\n0200002\r\n');
});
test('deduplicates without reordering',()=>{
  const result=buildTdxEbk([row('600000','sh'),row('000001','sz'),row('600000','sh')]);
  assert.equal(result.count,2);
  assert.equal(result.text,'1600000\r\n0000001\r\n');
});
test('rejects unsupported or ambiguous markets without exporting a partial list',()=>{
  for(const r of [row('920001','bj'),row('830001','unknown'),row('600000','sz'),row('000001','sh')]){
    assert.throws(()=>buildTdxEbk([row('000001','sz'),r]),/本次未生成文件/);
  }
});
test('rejects empty, numeric, missing and malformed codes',()=>{
  assert.throws(()=>buildTdxEbk([]),/为空/);
  for(const symbol of [1,'000001\n1600000','12345','1234567',null,'ABCDEF']){
    assert.throws(()=>buildTdxEbk([row(symbol,'sz')]),/无效/);
  }
});
