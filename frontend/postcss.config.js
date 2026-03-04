module.exports = {
  plugins: {
    // Use a standard PostCSS nesting plugin instead of the unsupported
    // "tailwindcss/nesting" path. The version of Tailwind in this project
    // doesn't export that subpath, which leads to the ERR_PACKAGE_PATH_NOT_EXPORTED
    // error seen in the console. Install `postcss-nesting` as a dev
    // dependency (`npm install -D postcss-nesting`) if nested rules are needed.

    // If you don't need nesting you can remove this entry altogether.
    "postcss-nesting": {},
    tailwindcss: {},
    autoprefixer: {},
  },
};
