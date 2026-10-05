const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { createRequire } = require("node:module");
const path = require("node:path");
const test = require("node:test");
const braces = require("braces");
const root = path.resolve(__dirname, "../..");
const manifest = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
const lock = JSON.parse(readFileSync(path.join(root, "package-lock.json"), "utf8"));
const patched = lock.packages["node_modules/braces"];

test("every locked braces copy uses the reviewed security patch", () => {
  const archive = manifest.devDependencies.braces;
  assert.match(archive, /^file:vendor\/braces\/[^/]+\.tgz$/);
  assert.equal(manifest.overrides.braces, "$braces");
  assert.equal(patched.name, "@corpuskit/braces");
  assert.equal(patched.resolved, archive);
  const integrity = "sha512-" + createHash("sha512")
    .update(readFileSync(path.join(root, archive.slice("file:".length))))
    .digest("base64");
  assert.equal(patched.integrity, integrity);

  for (const [location, entry] of Object.entries(lock.packages)) {
    if (!/(^|\/)node_modules\/braces$/.test(location)) continue;
    for (const field of ["name", "version", "resolved", "integrity"]) {
      assert.equal(entry[field], patched[field], `${location} must use the patched ${field}`);
    }
  }
});

test("every installed braces consumer resolves the guarded parser", () => {
  for (const [location, entry] of Object.entries(lock.packages)) {
    if (!["dependencies", "devDependencies", "optionalDependencies"]
      .some((field) => Object.hasOwn(entry[field] || {}, "braces"))) continue;
    const consumer = createRequire(path.join(root, location, "package.json"));
    const resolved = consumer("braces/package.json");
    assert.equal(resolved.name, patched.name, `${location || "root"} resolves an unpatched braces copy`);
    assert.equal(resolved.version, patched.version, `${location || "root"} resolves the wrong patch version`);
    const parser = consumer("braces");
    assert.throws(() => parser.compile("{".repeat(4000) + "a,b" + "}".repeat(4000)), {
      name: "SyntaxError", message: /nesting exceeds 128/,
    }, `${location || "root"} must enforce the nesting guard`);
  }
});

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
