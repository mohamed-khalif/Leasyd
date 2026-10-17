// ESLint for the web app (npm run lint). Type-aware rules are left to tsc (npm run typecheck).
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

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
    files: ["public/**/*.js"],   // plain browser scripts
    languageOptions: { sourceType: "script", globals: { window: "readonly" } },
  },
  {
    files: ["scripts/**/*.mjs", "mock/**/*.ts", "vite.config.ts"],
    languageOptions: { globals: { process: "readonly", console: "readonly", Buffer: "readonly", URL: "readonly", setTimeout: "readonly" } },
  },
);
