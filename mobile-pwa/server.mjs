import express from 'express';
import path from 'path';
import { Readable } from 'node:stream';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const app = express();
const port = Number(process.env.PARKINGAI_PWA_PORT || 4173);
const host = process.env.PARKINGAI_PWA_HOST || '127.0.0.1';
const apiOrigin = process.env.PARKINGAI_API_URL;

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('PARKINGAI_PWA_PORT must be a valid TCP port.');
}
if (apiOrigin) {
  const parsed = new URL(apiOrigin);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error('PARKINGAI_API_URL must be an HTTP(S) origin without embedded credentials.');
  }
}

app.disable('x-powered-by');
app.use(express.json({ limit: '1mb' }));
app.use((req, res, next) => {
  res.setHeader('Cache-Control', 'no-store');
  next();
});

app.use('/api/v1', async (req, res) => {
  if (!apiOrigin) {
    return res.status(503).json({ detail: 'PARKINGAI_API_URL is not configured.' });
  }

  const headers = new Headers();
  for (const name of ['authorization', 'content-type', 'accept', 'idempotency-key']) {
    const value = req.get(name);
    if (value) headers.set(name, value);
  }

  try {
    const target = new URL(req.originalUrl, apiOrigin);
    const upstream = await fetch(target, {
      method: req.method,
      headers,
      body: ['GET', 'HEAD'].includes(req.method) ? undefined : JSON.stringify(req.body ?? {}),
      signal: AbortSignal.timeout(30000),
      redirect: 'manual',
    });

    res.status(upstream.status);
    for (const name of ['content-type', 'cache-control', 'content-disposition']) {
      const value = upstream.headers.get(name);
      if (value) res.setHeader(name, value);
    }

    if (!upstream.body) return res.end();
    Readable.fromWeb(upstream.body).pipe(res);
  } catch {
    console.error('ParkingAI API proxy request failed.');
    if (!res.headersSent) res.status(502).json({ detail: 'ParkingAI API is unavailable.' });
    else res.destroy();
  }
});

app.use(express.static(path.join(__dirname, 'dist'), {
  extensions: ['html'],
  index: 'index.html',
  maxAge: 0,
}));

app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'dist', 'index.html'));
});

app.listen(port, host, () => {
  console.log(`ParkingAI PWA listening on ${host}:${port}`);
});
