/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        // brand red used throughout the app
        "cummins-red": "#D32F2F",
        // override the built-in slate-900 color so that existing
        // `bg-slate-900` classes render as our "slate black".
        slate: {
          900: "#0f1115",
        },
      },

      fontFamily: {
        // headings should use the Cummins brand font.  A real font file
        // can be placed in public/fonts and referenced via @font-face in
        // globals.css; for now we fall back gracefully to sans-serif.
        heading: ['"Cummins Brand"', "sans-serif"],
        // body text uses bold Roboto
        body: ["Roboto", "sans-serif"],
      },
    },
  },
  plugins: [],
};
