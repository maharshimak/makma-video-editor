import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

/** Benchmarks are separate from tests so a normal run stays fast. */
export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL(".", import.meta.url)),
    },
  },
  test: {
    include: ["tests/bench/**/*.bench.ts"],
    environment: "node",
  },
});
