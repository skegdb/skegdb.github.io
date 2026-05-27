import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://skegdb.github.io',
  output: 'static',
  build: {
    assets: 'assets',
  },
});
