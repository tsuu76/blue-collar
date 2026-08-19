import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds this one widget into the EXISTING Flask static folder as a plain
// ES module + CSS file with stable (non-hashed) names, so dashboard.html
// can reference them directly — no manifest, no Node process at runtime,
// no changes to how Flask serves static/. `emptyOutDir: false` because
// src/dashboard/static/ already holds style.css, cardswap-card-content.css,
// and company-fallback.svg for the rest of the (untouched) dashboard.
// NOTE: CardSwap.jsx has its own plain `import './CardSwap.css'` (left
// untouched — it's the official file). Vite still extracts that into a
// `cardswap-react.css` asset here even though main.jsx no longer links
// it (see main.jsx's shadow-DOM note for why) — it imports both CSS
// files again via `?inline` instead. That leftover .css file is unused
// and safe to delete after a build; it is NOT referenced by dashboard.html.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../../src/dashboard/static",
    emptyOutDir: false,
    rollupOptions: {
      input: "src/main.jsx",
      output: {
        entryFileNames: "cardswap-react.js",
        chunkFileNames: "cardswap-react-[name].js",
        assetFileNames: (assetInfo) =>
          assetInfo.name && assetInfo.name.endsWith(".css")
            ? "cardswap-react.css"
            : "cardswap-react-[name][extname]",
      },
    },
  },
});
