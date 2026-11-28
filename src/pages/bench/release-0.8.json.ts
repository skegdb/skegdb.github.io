import data from '~/data/bench/release-0.8.json';

export function GET() {
  return new Response(JSON.stringify(data, null, 2) + '\n', {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}
