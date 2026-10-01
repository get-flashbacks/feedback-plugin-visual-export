'use strict';
// Behavioural coverage for the export pre-flight capability gate. screen.js is
// a browser IIFE with no exports, so extract the gate and its constant from the
// real source and drive them directly -- that keeps these assertions tied to
// shipped code rather than a reimplementation that could drift.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

// The JS suite runs from the repository root (see .github/workflows), so keep
// this fixture literal rather than building a path from external input.
const source = fs.readFileSync('screen.js', 'utf8');

function block(signature) {
    const start = source.indexOf(signature);
    assert.ok(start >= 0, `missing ${signature}`);
    const open = source.indexOf('{', start);
    let depth = 1;
    let end = open + 1;
    while (depth && end < source.length) {
        if (source[end] === '{') depth++;
        else if (source[end] === '}') depth--;
        end++;
    }
    assert.equal(depth, 0, `${signature} must be balanced`);
    return source.slice(start, end);
}

// `SPLIT_BRIDGE` is a statement, not a braced block: slice to the terminating
// semicolon so the extraction cannot run past it into the function below.
function statement(signature) {
    const start = source.indexOf(signature);
    assert.ok(start >= 0, `missing ${signature}`);
    const end = source.indexOf(';', start);
    assert.ok(end > start, `${signature} must be terminated`);
    return source.slice(start, end + 1);
}

// Build a frameDriverProblem bound to a stub host. `highway` stands in for the
// feedBack core global; the split object stands in for whichever Splitscreen
// global the plugin picked up.
function gateFor(highway) {
    const constant = statement("const SPLIT_BRIDGE =");
    const fn = block('function frameDriverProblem(');
    return new Function('window', `${constant}\n${fn}\nreturn frameDriverProblem;`)(
        { highway }
    );
}

// The bridge as Split Screen v1.14.8 shipped it.
function fullBridge() {
    return {
        isActive: () => true,
        beginOfflineRender() {},
        renderFrameAt() { return true; },
        endOfflineRender() {},
    };
}

// A pre-bridge build: splits and is active, but exposes no offline hooks.
function preBridge() {
    return { isActive: () => true, renderFrameAt() { return true; } };
}

test('a host without renderFrameAt is rejected on its own, not as a Split Screen problem', () => {
    const problem = gateFor(undefined)(fullBridge(), true);
    assert.match(problem, /highway\.renderFrameAt\(\)/);
    assert.match(problem, /f7c761c/);
    // The core gap takes precedence and must not blame the plugin.
    assert.doesNotMatch(problem, /Split Screen/);
});

test('a host that merely lacks the function type is still rejected', () => {
    const problem = gateFor({ renderFrameAt: 'not a function' })(fullBridge(), true);
    assert.match(problem, /highway\.renderFrameAt\(\)/);
});

test('an up-to-date host exports without Split Screen installed', () => {
    assert.equal(gateFor({ renderFrameAt() {} })(undefined, false), null);
});

test('Split Screen is only required while a split layout is being exported', () => {
    // Inactive splitscreen still exposes the bridge, but a stale build must not
    // block a single-instance export.
    assert.equal(gateFor({ renderFrameAt() {} })(preBridge(), false), null);
});

test('an active pre-bridge Split Screen is reported as a Split Screen version problem', () => {
    const problem = gateFor({ renderFrameAt() {} })(preBridge(), true);
    assert.match(problem, /Split Screen is active/);
    assert.match(problem, /beginOfflineRender/);
    assert.match(problem, /endOfflineRender/);
    assert.match(problem, /1\.14\.8/);
    // The host is fine here, so the message must say so.
    assert.match(problem, /this feedBack host is new enough/);
});

test('a partial bridge names only the hooks that are actually absent', () => {
    const problem = gateFor({ renderFrameAt() {} })(
        { isActive: () => true, renderFrameAt() { return true; }, beginOfflineRender() {} },
        true
    );
    assert.match(problem, /endOfflineRender/);
    assert.doesNotMatch(problem, /beginOfflineRender/);
});

test('a bridge missing renderFrameAt alone is also rejected', () => {
    const problem = gateFor({ renderFrameAt() {} })(
        { isActive: () => true, beginOfflineRender() {}, endOfflineRender() {} },
        true
    );
    assert.match(problem, /renderFrameAt/);
});

test('an active Split Screen with the complete bridge exports', () => {
    assert.equal(gateFor({ renderFrameAt() {} })(fullBridge(), true), null);
});

test('the core gap is reported even when Split Screen is also too old', () => {
    const problem = gateFor(undefined)(preBridge(), true);
    assert.match(problem, /highway\.renderFrameAt\(\)/);
});