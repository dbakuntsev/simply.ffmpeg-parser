/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Space Grotesk", "system-ui", "sans-serif"],
        mono: ["IBM Plex Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        ink: "#141014",
        muted: "#5e5760",
        panel: "#ffffff",
        edge: "#e2d9cf",
      },
      boxShadow: {
        panel: "0 18px 40px rgba(20, 16, 20, 0.12)",
      },
    },
  },
  plugins: [],
};
