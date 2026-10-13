import type { Config } from "tailwindcss";

/**
 * The palette is built for long sessions in a dark room looking at footage:
 * near-black neutrals so the picture is the brightest thing on screen, one
 * restrained accent, and semantic colours for track kinds.
 */
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#07080a",
          900: "#0b0d10",
          850: "#101317",
          800: "#15181d",
          750: "#1b1f26",
          700: "#22272f",
          600: "#2d333d",
          500: "#3b424e",
          400: "#5b6472",
          300: "#8b95a5",
          200: "#b9c1cd",
          100: "#e3e8ef",
        },
        accent: {
          DEFAULT: "#e0a33c",
          soft: "#f0c274",
          dim: "#8a6524",
        },
        video: "#3f7fd4",
        audio: "#3fae7f",
        textclip: "#b06fd8",
        adjust: "#d4643f",
        danger: "#e2544b",
      },
      fontFamily: {
        sans: ["var(--font-ui)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      fontSize: {
        "2xs": ["10px", "13px"],
        xs: ["11px", "15px"],
        sm: ["12px", "17px"],
        base: ["13px", "19px"],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 8px 24px rgba(0,0,0,0.5)",
        pop: "0 12px 40px rgba(0,0,0,0.65)",
      },
      transitionTimingFunction: {
        swift: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
    },
  },
  plugins: [],
};

export default config;
