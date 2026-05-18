import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The codebase uses `.js` for JSX (CRA convention). Vite's default treats
// `.js` as plain JavaScript; tell esbuild and the React plugin to parse JSX
// in `.js` too so we don't have to rename every component.
const JS_JSX = /\.(jsx?|tsx?)$/;

export default defineConfig({
  plugins: [
    react({
      include: JS_JSX,
    }),
  ],
  resolve: {
    // Carbon's SCSS emits `url(~@ibm/plex/...)` to load IBM Plex woff2 files —
    // a webpack-loader convention Vite doesn't recognize. Stripping the `~`
    // lets Vite resolve them as ordinary node_modules paths during the CSS
    // url() rewrite, so the fonts are bundled instead of being left as
    // unresolved `~@ibm/plex/...` strings in the output stylesheet.
    alias: [{ find: /^~(.+)$/, replacement: "$1" }],
  },
  server: {
    port: 3000,
    open: false,
  },
  build: {
    outDir: "build",
    sourcemap: true,
  },
  esbuild: {
    loader: "jsx",
    include: /src\/.*\.jsx?$/,
    exclude: [],
  },
  optimizeDeps: {
    esbuildOptions: {
      loader: { ".js": "jsx" },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/setupTests.js"],
    css: false,
    // Playwright owns everything under e2e/. Vitest's default include picks
    // up *.spec.js too, which would try to import @playwright/test in a
    // jsdom environment and fail at collection time.
    exclude: ["node_modules/**", "build/**", "e2e/**"],
  },
});
