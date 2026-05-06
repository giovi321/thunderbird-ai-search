import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://giovi321.github.io',
  base: '/thunderbird-ai-search',
  integrations: [
    starlight({
      title: 'Thunderbird AI Search',
      description: 'Semantic email search for Thunderbird — local, private, no cloud.',
      logo: {
        src: './src/assets/logo.svg',
        replacesTitle: false,
      },
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/giovi321/thunderbird-ai-search',
        },
      ],
      editLink: {
        baseUrl: 'https://github.com/giovi321/thunderbird-ai-search/edit/main/docs/',
      },
      sidebar: [
        { label: 'Home', link: '/' },
        {
          label: 'Getting Started',
          items: [
            { label: 'Installation', link: '/getting-started/installation/' },
            { label: 'Configuration', link: '/getting-started/configuration/' },
          ],
        },
        {
          label: 'Guides',
          items: [
            { label: 'Gmail Setup', link: '/guides/gmail/' },
            { label: 'How Indexing Works', link: '/guides/indexing/' },
            { label: 'Reverse Proxy', link: '/guides/reverse-proxy/' },
            { label: 'Custom Certificate', link: '/guides/custom-certificate/' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'API Endpoints', link: '/reference/api/' },
            { label: 'Troubleshooting', link: '/reference/troubleshooting/' },
          ],
        },
      ],
    }),
  ],
});
