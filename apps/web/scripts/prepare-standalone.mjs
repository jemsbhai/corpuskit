import { cp, mkdir } from "node:fs/promises";
import path from "node:path";

const webRoot = process.cwd();
const standaloneWeb = path.join(webRoot, ".next", "standalone", "apps", "web");
const standaloneNext = path.join(standaloneWeb, ".next");

await mkdir(standaloneNext, { recursive: true });
await cp(
  path.join(webRoot, ".next", "static"),
  path.join(standaloneNext, "static"),
  {
    recursive: true,
    force: true,
  },
);
