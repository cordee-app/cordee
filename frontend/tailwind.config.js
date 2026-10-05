/**
 * Cordée design tokens — Organic direction (cream / terracotta / sage).
 *
 * DROP-IN REPLACEMENT for the AIngel config: every token KEY is unchanged
 * (ink, accent, ai, provider, surface, border, text, status, danger…), only
 * the VALUES change, so no className in any .tsx file needs editing.
 *  - accent  → terracotta (primary actions, selection)
 *  - ai      → terracotta too (the Guide = the old "AIngel" persona)
 *  - status.done / success → sage
 *  - ink     → warm near-black for the top bar and tabs
 * Spacing, fontSize and zIndex are deliberately unchanged (layout safety).
 */

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        ink: {
          DEFAULT: "#201e1d",
          soft: "#645c50",
          dark: { DEFAULT: "#f9f4ed", soft: "#c0b6a5" },
        },
        accent: {
          DEFAULT: "#c67139",
          strong: "#b2622d",
          deep: "#8c491a",
          soft: "#fff2eb",
          tint: "#ffe1d0",
          dark: { DEFAULT: "#f6a06b", strong: "#d67f48", deep: "#c67139", soft: "#402310", tint: "#2e2015" },
        },
        ai: {
          DEFAULT: "#c67139",
          soft: "#fff2eb",
          tint: "#fbf6ee",
          dark: { DEFAULT: "#f6a06b", soft: "#402310", tint: "#2e2015" },
        },
        provider: {
          claude: "#c67139",
          mistral: "#b8742f",
          openai: "#3f7f6a",
          scaleway: "#7a5aa6",
          ollama: "#645c50",
        },
        surface: {
          base: "#f5ead8",
          panel: "#ebddc5",
          raised: "#fbf6ee",
          subtle: "#eee7db",
          muted: "#f9f4ed",
          dark: { base: "#1b1916", panel: "#22201c", raised: "#2b2823", subtle: "#332f29", muted: "#1f1d19" },
        },
        border: {
          DEFAULT: "#dcd3c4",
          subtle: "#e6dccb",
          muted: "#e1d7c6",
          strong: "#c0b6a5",
          dark: { DEFAULT: "#3a352e", subtle: "#2e2a24", muted: "#34302a", strong: "#474238" },
        },
        text: {
          DEFAULT: "#201e1d",
          soft: "#474238",
          muted: "#645c50",
          faint: "#82796a",
          dark: { DEFAULT: "#f9f4ed", soft: "#dcd3c4", muted: "#c0b6a5", faint: "#a19786" },
        },
        status: {
          running: { DEFAULT: "#3f628f", bg: "#e3ebf5", dark: { DEFAULT: "#9fb8dc", bg: "#26303d" } },
          pending: { DEFAULT: "#645c50", bg: "#eee7db", dark: { DEFAULT: "#c0b6a5", bg: "#332f29" } },
          done:    { DEFAULT: "#56633f", bg: "#e1eecc", dark: { DEFAULT: "#aebf92", bg: "#2a3120" } },
          failed:  { DEFAULT: "#a3402f", bg: "#f7e2dc", dark: { DEFAULT: "#e79a8a", bg: "#3d221c" } },
        },
        danger: "#a3402f",
        dangerStrong: "#86331f",
        warning: "#b2622d",
        success: "#56633f",
        info: "#3f628f",
      },
      spacing: { 1: "1px", 3: "3px", 5: "5px", 7: "7px", 9: "9px", 10: "10px", 13: "13px", 40: "40px" },
      borderRadius: {
        DEFAULT: "8px",
        xs: "6px",
        sm: "8px",
        md: "10px",
        lg: "16px",
        xl: "20px",
        "2xl": "28px",
      },
      boxShadow: {
        subtle: "0 1px 2px rgba(46,43,37,0.14)",
        medium: "0 3px 10px rgba(46,43,37,0.16)",
        strong: "0 12px 32px rgba(46,43,37,0.22)",
        side: "-1px 0 14px rgba(46,43,37,0.10)",
        focus: "0 0 0 2px rgba(198,113,57,0.35)",
      },
      fontSize: { "2xs": "11px", xs: "11px", "sm+": "12px", "md-": "13px" },
      zIndex: { header: 100, modal: 1000, toast: 1100 },
      fontFamily: {
        sans: ["Figtree", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        heading: ["Caprasimo", "Georgia", "serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
