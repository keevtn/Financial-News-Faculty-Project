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
        "fin-bg":     "#0a0e1a",
        "fin-card":   "#0f1629",
        "fin-border": "#1e2d4a",
        "fin-accent": "#00d4aa",
      },
    },
  },
  plugins: [],
};
export default config;
