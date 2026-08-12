import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const rootDirectory = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: { alias: { "@": path.resolve(rootDirectory, "src") } },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "json-summary"],
      include: [
        "src/components/advanced-workbench.tsx",
        "src/components/project-context.tsx",
        "src/lib/advanced.ts",
        "src/lib/api-client.ts",
        "src/lib/artifacts.ts",
        "src/lib/coverage-lab.ts",
        "src/lib/g2p.ts",
        "src/lib/generation.ts",
        "src/lib/jobs.ts",
        "src/lib/selection.ts",
        "src/lib/workbench-input.ts",
      ],
      thresholds: {
        perFile: true,
        branches: 90,
        functions: 75,
        lines: 80,
        statements: 80,
      },
    },
  },
});
