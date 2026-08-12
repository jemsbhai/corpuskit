import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const rootDirectory = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(rootDirectory, "src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: [
      "src/lib/auth/**/*.test.ts",
      "src/lib/browser-auth.test.ts",
      "src/proxy.test.ts",
      "src/app/auth/auth-routes.test.ts",
      "src/app/api/v1/**/route.test.ts",
    ],
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary"],
      include: [
        "src/lib/auth/**/*.ts",
        "src/lib/browser-auth.ts",
        "src/proxy.ts",
        "src/app/auth/**/route.ts",
        "src/app/api/v1/**/route.ts",
      ],
      thresholds: {
        branches: 90,
        functions: 90,
        lines: 90,
        statements: 90,
        "src/lib/auth/record-cipher.ts": { branches: 100 },
        "src/lib/auth/security.ts": { branches: 100 },
        "src/lib/auth/service.ts": { branches: 100 },
        "src/lib/auth/session-store.ts": { branches: 100 },
      },
    },
  },
});
