import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'Gauntlet',
  description: 'Behavior testing framework for AI agents',

  head: [
    ['meta', { property: 'og:title', content: 'Gauntlet — Behavior testing for AI agents' }],
    ['meta', { property: 'og:description', content: 'Run your AI agents through the gauntlet. YAML specs, function call assertions, multi-turn testing.' }],
  ],

  themeConfig: {
    nav: [
      { text: 'Guide', link: '/getting-started/introduction' },
      { text: 'Reference', link: '/reference/cli' },
      { text: 'Architecture', link: '/architecture/overview' },
      { text: 'Examples', link: '/examples/hello-world' },
    ],

    sidebar: [
      {
        text: 'Getting Started',
        items: [
          { text: 'Introduction', link: '/getting-started/introduction' },
          { text: 'Installation', link: '/getting-started/installation' },
          { text: 'Quick Start', link: '/getting-started/quickstart' },
          { text: 'Core Concepts', link: '/getting-started/concepts' },
        ],
      },
      {
        text: 'Guides',
        items: [
          { text: 'Writing Specs', link: '/guides/writing-specs' },
          { text: 'SDK Integration', link: '/guides/sdk-integration' },
          { text: 'Assertions', link: '/guides/assertions' },
          { text: 'Multi-Turn Testing', link: '/guides/multi-turn' },
          { text: 'Guardrail Validation', link: '/guides/guardrails' },
          { text: 'Agent Setup', link: '/guides/agent-setup' },
          { text: 'CI Integration', link: '/guides/ci-integration' },
        ],
      },
      {
        text: 'Reference',
        items: [
          { text: 'CLI Commands', link: '/reference/cli' },
          { text: 'Spec Format', link: '/reference/spec-format' },
          { text: 'SDK API', link: '/reference/sdk-api' },
          { text: 'Protocols', link: '/reference/protocols' },
          { text: 'Domain Models', link: '/reference/domain-models' },
          { text: 'Configuration', link: '/reference/configuration' },
        ],
      },
      {
        text: 'Architecture',
        items: [
          { text: 'Overview', link: '/architecture/overview' },
          { text: 'Execution Model', link: '/architecture/execution-model' },
          { text: 'Trace Boxes', link: '/architecture/trace-boxes' },
          { text: 'Design Decisions', link: '/architecture/design-decisions' },
          { text: 'Extending Gauntlet', link: '/architecture/extending' },
        ],
      },
      {
        text: 'Examples',
        items: [
          { text: 'Hello World', link: '/examples/hello-world' },
          { text: 'Order Support Agent', link: '/examples/order-support' },
          { text: 'Guardrail Suite', link: '/examples/guardrail-suite' },
        ],
      },
      { text: 'Comparison', link: '/comparison' },
      { text: 'Roadmap', link: '/roadmap' },
    ],

    socialLinks: [
      { icon: 'github', link: 'https://github.com/gauntlet-ai/gauntlet' },
    ],

    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Copyright 2026 Gauntlet Contributors',
    },

    search: {
      provider: 'local',
    },

    editLink: {
      pattern: 'https://github.com/gauntlet-ai/gauntlet/edit/main/site/:path',
      text: 'Edit this page on GitHub',
    },
  },
})
