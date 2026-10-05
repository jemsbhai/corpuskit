const assert = require("node:assert/strict");
const test = require("node:test");
const braces = require("braces");

test("ordinary alternatives and ranges remain compatible", () => {
  assert.deepEqual(braces.expand("a/{b,c}/{1..3}"), ["a/b/1", "a/b/2", "a/b/3", "a/c/1", "a/c/2", "a/c/3"]);
  assert.equal(braces.compile("a/{b,c}"), "a/(b|c)");
  assert.equal(braces.stringify(braces.parse("a/{b,c}")), "a/{b,c}");
});

test("deep brace and parenthesis patterns fail with a controlled syntax error", () => {
  for (const [open, close] of [["{", "}"], ["(", ")"]]) {
    const pattern = open.repeat(4000) + "a,b" + close.repeat(4000);
    for (const method of ["parse", "compile", "expand", "stringify"]) {
      assert.throws(() => braces[method](pattern), { name: "SyntaxError", message: /nesting exceeds 128/ });
    }
  }
});

test("caller-supplied deep ASTs cannot bypass traversal guards", () => {
  for (const method of ["compile", "expand", "stringify"]) {
    let ast = { type: "text", value: "x" };
    for (let i = 0; i < 10000; i++) ast = { type: "root", nodes: [ast] };
    assert.throws(() => braces[method](ast), { name: "SyntaxError", message: /nesting exceeds 128/ });
  }
});
