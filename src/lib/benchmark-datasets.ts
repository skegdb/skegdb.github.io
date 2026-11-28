export const benchmarkDatasets = {
  'arxiv-instructorxl': {
    label: 'arXiv / INSTRUCTOR',
    note: 'arXiv text embedded with INSTRUCTOR-XL.',
    links: [['arXiv API', 'https://info.arxiv.org/help/api/index.html'], ['INSTRUCTOR-XL model card', 'https://huggingface.co/hkunlp/instructor-xl']],
  },
  'gemini-001': {
    label: 'Gemini embeddings',
    note: 'A prepared export of Gemini embedding-001 vectors. The run record does not identify the original text collection.',
    links: [['Gemini embedding models', 'https://ai.google.dev/gemini-api/docs/embeddings']],
  },
  'openai3-large': {
    label: 'OpenAI embeddings',
    note: 'A prepared export of OpenAI text-embedding-3-large vectors. The run record does not identify the original text collection.',
    links: [['text-embedding-3-large model', 'https://platform.openai.com/docs/models/text-embedding-3-large']],
  },
  'wiki-cohere-shuf': {
    label: 'Wikipedia / Cohere',
    note: 'Wikipedia text embedded with Cohere and shuffled before indexing.',
    links: [['Wikimedia data dumps', 'https://dumps.wikimedia.org/']],
  },
  'mxbai-100k': {
    label: 'Mixedbread 100k',
    note: 'A prepared corpus of 100,000 mxbai-embed-large-v1 vectors. The macOS profile study uses the same workload. The run record does not identify the original text collection.',
    links: [['Mixedbread model card', 'https://huggingface.co/mixedbread-ai/mxbai-embed-large-v1']],
  },
  'glove104': {
    label: 'GloVe 104',
    note: 'ANN-Benchmarks GloVe 100-dimensional word vectors with four zero coordinates appended. The harness stopped before completing churn and restart.',
    links: [['GloVe dataset in ANN-Benchmarks', 'https://ann-benchmarks.com/glove-100-angular_10_angular.html']],
  },
  ...Object.fromEntries([1, 3, 5].map(n => [`msmarco-${n}m`, {
    label: `MS MARCO ${n}M`,
    note: `Benchmark export of ${n} million passages from MS MARCO.`,
    links: [['Microsoft MS MARCO passage dataset', 'https://github.com/microsoft/MSMARCO-Passage-Ranking']],
  }])),
} as Record<string, { label: string; note: string; links: string[][] }>;
