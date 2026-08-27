import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        paper: {
          DEFAULT: "var(--paper)",
          deep: "var(--paper-deep)",
          edge: "var(--paper-edge)",
        },
        ink: {
          DEFAULT: "var(--ink)",
          soft: "var(--ink-soft)",
          faint: "var(--ink-faint)",
        },
        cinnabar: {
          DEFAULT: "var(--cinnabar)",
          soft: "var(--cinnabar-soft)",
        },
        chalk: {
          DEFAULT: "var(--chalk-green)",
          soft: "var(--chalk-green-soft)",
        },
        board: "var(--board)",
        warn: {
          DEFAULT: "var(--amber-warn)",
          soft: "var(--amber-warn-soft)",
        },
        rule: "var(--rule)",
      },
    },
  },
  plugins: [],
};
export default config;
