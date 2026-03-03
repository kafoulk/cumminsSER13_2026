import nextConfig from "eslint-config-next";

const config = [
  ...nextConfig,
  {
    ignores: [".next/**", "out/**", "node_modules/**"],
  },
];

export default config;
