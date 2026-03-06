const plugins = {};

// Allow either nesting plugin so local environments with drifted node_modules
// still boot. Prefer postcss-nesting, fallback to postcss-nested.
try {
  require.resolve("postcss-nesting");
  plugins["postcss-nesting"] = {};
} catch {
  try {
    require.resolve("postcss-nested");
    plugins["postcss-nested"] = {};
  } catch {
    // Continue without nesting if neither plugin exists.
  }
}

plugins.tailwindcss = {};
plugins.autoprefixer = {};

module.exports = { plugins };
