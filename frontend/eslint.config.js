import js from "@eslint/js";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";

// ESLint 9 flat config. Replaces the legacy `.eslintrc.json`. Three blocks:
//
//   1. Ignores (kept narrow — `build/`, `node_modules/`, vendor caches).
//   2. App source rules: react + hooks + a11y, with the same warning/error
//      gradient the legacy config had. `fetchpriority` is whitelisted on
//      `react/no-unknown-property` because React 18/19 still emit it
//      lowercase to the DOM despite the rule's camelCase preference.
//   3. Test files: declare vitest's globals so `describe / it / vi / expect`
//      don't trip `no-undef`.
export default [
  {
    ignores: ["build/**", "dist/**", "node_modules/**", ".vite/**", "coverage/**"],
  },
  js.configs.recommended,
  {
    files: ["src/**/*.{js,jsx}"],
    plugins: {
      react,
      "react-hooks": reactHooks,
      "jsx-a11y": jsxA11y,
    },
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    settings: {
      react: { version: "detect" },
    },
    rules: {
      ...react.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",
      "react/no-unknown-property": ["error", { ignore: ["fetchpriority"] }],
      // The CRA preset disallowed alert/confirm/prompt by default; the code
      // has explicit `eslint-disable-next-line no-alert` comments at the
      // intentional confirm() spots. Keep the rule on so those disables
      // remain meaningful (and so a stray alert() can't slip in).
      "no-alert": "error",
      "react-hooks/exhaustive-deps": "warn",
      "no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },
  {
    files: ["src/**/__tests__/**/*.{js,jsx}", "src/**/*.test.{js,jsx}", "src/setupTests.js"],
    languageOptions: {
      globals: {
        describe: "readonly",
        it: "readonly",
        test: "readonly",
        expect: "readonly",
        beforeEach: "readonly",
        afterEach: "readonly",
        beforeAll: "readonly",
        afterAll: "readonly",
        vi: "readonly",
      },
    },
  },
  // Playwright E2E suite. Runs in Node, not the browser; imports come from
  // @playwright/test instead of vitest's globals. Allow Node globals and
  // soften a few rules that don't fit Node-side test scaffolding.
  {
    files: ["e2e/**/*.js", "playwright.config.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.node,
        ...globals.browser,
        Buffer: "readonly",
      },
    },
    rules: {
      "no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
      "no-empty-pattern": "off",
    },
  },
];
