import { defineConfig } from 'vitepress'

const description =
  'Evals for AI agents that behave like tests: check the tool calls of the agent you ship, with a YAML spec and a CI exit code.'

export default defineConfig({
  title: 'highbar',
  description,
  // Served by GitHub Pages at https://vinimlo.github.io/highbar/
  base: '/highbar/',

  head: [
    ['meta', { property: 'og:title', content: 'highbar: evals for AI agents that behave like tests' }],
    ['meta', { property: 'og:description', content: description }],
  ],

  themeConfig: {
    nav: [
      { text: 'Guide', link: '/getting-started/introduction' },
      { text: 'Reference', link: '/reference/spec-format' },
      { text: 'Comparison', link: '/comparison' },
    ],

    sidebar: [
      {
        text: 'Getting started',
        items: [
          { text: 'Introduction', link: '/getting-started/introduction' },
          { text: 'Quick start', link: '/getting-started/quickstart' },
          { text: 'Core concepts', link: '/getting-started/concepts' },
        ],
      },
      {
        text: 'Guides',
        items: [
          { text: 'Writing specs', link: '/guides/writing-specs' },
          { text: 'Connecting your agent', link: '/guides/connecting-your-agent' },
          { text: 'Attach mode and sessions', link: '/guides/attach-mode' },
          { text: 'Assertions', link: '/guides/assertions' },
          { text: 'Multi-turn tests', link: '/guides/multi-turn' },
          { text: 'Guardrails', link: '/guides/guardrails' },
          { text: 'Variations', link: '/guides/variations' },
          { text: 'Repetition and CI gates', link: '/guides/repetition' },
          { text: 'Troubleshooting', link: '/guides/troubleshooting' },
        ],
      },
      {
        text: 'Reference',
        items: [
          { text: 'CLI', link: '/reference/cli' },
          { text: 'Spec format', link: '/reference/spec-format' },
          { text: 'Report', link: '/reference/report' },
          { text: 'Trace contract v1', link: '/reference/trace-contract' },
          { text: 'Python SDK', link: '/reference/python-sdk' },
          { text: 'Python API', link: '/reference/python-api' },
        ],
      },
      {
        text: 'Examples',
        items: [
          { text: 'Quickstart agent', link: '/examples/quickstart' },
          { text: 'OpenAI-compatible agent', link: '/examples/openai-agent' },
        ],
      },
      {
        items: [
          { text: 'Comparison', link: '/comparison' },
          { text: 'Roadmap', link: '/roadmap' },
        ],
      },
    ],

    socialLinks: [{ icon: 'github', link: 'https://github.com/vinimlo/highbar' }],

    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Copyright 2026 highbar contributors',
    },

    search: {
      provider: 'local',
    },

    editLink: {
      pattern: 'https://github.com/vinimlo/highbar/edit/main/site/:path',
      text: 'Edit this page on GitHub',
    },
  },
})
