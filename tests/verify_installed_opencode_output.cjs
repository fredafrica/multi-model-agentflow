// Read-only source-path verification. No SDK/network/provider request is made.
// This is an explicit integration audit, not part of the Python runtime.
const fs = require('fs');
const crypto = require('crypto');
const assert = require('assert/strict');
const {execFileSync} = require('child_process');

const executable = process.argv[2];
if (!executable) throw new Error('Pass the installed OpenCode executable path');
const version = execFileSync(executable, ['--version'], {encoding: 'utf8'}).trim();
const parsedVersion = version.match(/^(\d+)\.(\d+)\.(\d+)$/);
assert.ok(parsedVersion, 'OpenCode version is not a stable semantic version');
assert.deepEqual(
  parsedVersion.slice(1, 3).map(Number),
  [1, 18],
  'Re-audit the source path for another major/minor series',
);
assert.ok(Number(parsedVersion[3]) >= 29, 'OpenCode version predates the verified baseline');
const bytes = fs.readFileSync(executable);
const source = bytes.toString();
const transform = source.match(/function \w+\(\$,([A-Za-z_$][\w$]*)=M7\)\{return Math\.min\(\$\.limit\.output,\1\)\|\|\1\}/)?.[0];
assert.ok(transform, 'installed generic output-limit transform not found');
assert.ok(source.includes('var M7=32000,'));
assert.ok(source.includes('outputTokenMax:G("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX")'));
assert.match(source, /maxOutputTokens:\w+\.maxOutputTokens\(e\.model,e\.flags\.outputTokenMax\)/);
assert.match(source, /maxOutputTokens:\w+\.params\.maxOutputTokens/);
// The bundled OpenAI-compatible chat SDK passes the provided output limit to
// max_tokens. Execute that exact property expression along with the transform.
const sdk = source.match(/metadataKey:\w+\(this\.providerOptionsName,\w+\),args:\{model:this\.modelId,user:\w+\.user,(max_tokens:(\w+)),temperature:/);
assert.ok(sdk, 'installed compatible chat SDK output mapping not found');
const limit = new Function('M7', `return (${transform})`)(32000);
const wire = new Function(sdk[2], `return ({${sdk[1]}})`);
assert.equal(limit({limit: {output: 65536}}), 32000);
const cases = [[65536, 16000, 16000], [65536, 100000, 65536], [384000, 384000, 384000]];
for (const [capability, authorized, expected] of cases) {
  const effective = Math.min(capability, authorized);
  assert.equal(wire(limit({limit: {output: effective}}, effective)).max_tokens, expected);
}
console.log(JSON.stringify({
  version,
  binary_sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
  extracted_transform: transform,
  sdk_output_property: sdk[1],
  legacy_default_observed: 32000,
  cases: cases.map(([capability, authorized, expected]) => ({capability, authorized, wire_max_tokens: expected})),
  evidence: 'installed-source-path; extracted expressions only; no complete SDK or HTTP invocation',
  real_model_calls: 0,
}, null, 2));
