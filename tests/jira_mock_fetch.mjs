globalThis.fetch = async (url, options) => {
  if (!url.startsWith('https://jira.example.test/rest/api/2/') ||
      options?.headers?.authorization !== `Bearer ${process.env.JIRA_API_TOKEN}`) {
    throw new Error('unexpected Jira request');
  }
  if (url.includes('/issue/ABC-404?')) return { status: 404, ok: false };
  if (url.includes('/issue/ABC-1?')) return {
    status: 200, ok: true, json: async () => ({ key: 'ABC-1', fields: { summary: 'Synthetic' } })
  };
  if (url.includes('/search?') && url.includes('fail')) return { status: 500, ok: false };
  if (url.includes('/search?')) return {
    status: 200, ok: true, json: async () => ({ issues: [{ key: 'ABC-1' }], total: 1 })
  };
  throw new Error('unexpected Jira path');
};
