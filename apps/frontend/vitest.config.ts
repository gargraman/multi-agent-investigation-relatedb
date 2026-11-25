import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";

// Unit/component tests only (mocked, fast). The AC2 E2E lives in e2e/ and runs under Playwright.
export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
