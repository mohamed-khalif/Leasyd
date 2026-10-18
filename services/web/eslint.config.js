// ESLint for the web app (npm run lint). Type-aware rules are left to tsc (npm run typecheck).
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

const FORMAT = "Format with src/lib/format.ts (docs/formatting.md), not in pages or components.";
const PAGES = "A page doesn't import another page: move what they share to lib/ or components/.";

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**"] },
  { linterOptions: { reportUnusedDisableDirectives: "error" } },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}", "mock/**/*.ts", "scripts/**/*.mjs", "vite.config.ts"],
    languageOptions: { ecmaVersion: 2022, sourceType: "module" },
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  {
    // Requests go through src/api.ts: the token, error mapping, the 202 -> job -> poll flow, cancel.
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/api.ts", "src/config.ts", "src/**/*.test.{ts,tsx}", "src/test/**"],
    rules: { "no-restricted-globals": ["error", { name: "fetch", message: "Requests go through src/api.ts." }] },
  },
  {
    // Pages don't import other pages (that made an import cycle): shared code lives in lib/,
    // components/ or a page folder such as pages/synthetics/. Synthetics opens its own sub-pages.
    files: ["src/pages/**/*.tsx"],
    rules: { "no-restricted-imports": ["error", { patterns: [{ regex: "^\\./(?!synthetics/)[^/]+$", message: PAGES }] }] },
  },
  {
    // Numbers, dates and sizes are formatted only in src/lib/format.ts (docs/formatting.md).
    files: ["src/pages/**/*.tsx", "src/components/**/*.tsx"],
    ignores: ["src/**/*.test.tsx"],
    rules: {
      "no-restricted-properties": ["error", ...["toFixed", "toPrecision", "toLocaleString", "toLocaleDateString", "toLocaleTimeString"]
        .map((property) => ({ property, message: FORMAT }))],
      "no-restricted-globals": ["error", { name: "fetch", message: "Requests go through src/api.ts." }, { name: "Intl", message: FORMAT }],
    },
  },
  {
    files: ["src/pages/Synthetics.tsx"],
    rules: { "no-restricted-imports": ["error", { patterns: [{ regex: "^\\./(?!synthetics/|CheckDetail$|Maintenance$)[^/]+$", message: PAGES }] }] },
  },
  {
    // Settings' own pages share its building blocks (parts) and menu.
    files: ["src/pages/settings/*.tsx"],
    rules: { "no-restricted-imports": ["error", { patterns: [{ regex: "^\\./(?!parts$|menu$)[^/]+$", message: PAGES }] }] },
  },
  {
    files: ["public/**/*.js"],   // plain browser scripts
    languageOptions: { sourceType: "script", globals: { window: "readonly" } },
  },
  {
    files: ["scripts/**/*.mjs", "mock/**/*.ts", "vite.config.ts"],
    languageOptions: { globals: { process: "readonly", console: "readonly", Buffer: "readonly", URL: "readonly", setTimeout: "readonly" } },
  },
  {
    files: ["scripts/**/*.cjs"],   // CommonJS node scripts (scripts/perf: Playwright from the machine, not a dependency)
    languageOptions: { sourceType: "commonjs",
      globals: { require: "readonly", process: "readonly", console: "readonly", URL: "readonly", location: "readonly", localStorage: "readonly" } },
    rules: { "@typescript-eslint/no-require-imports": "off" },
  },
);
