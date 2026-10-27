import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';
import tls from 'node:tls';

const PROXY = { host: '127.0.0.1', port: 7890 };

function connect(urlStr) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const port = u.port || 443;
    const req = http.request({
      host: PROXY.host, port: PROXY.port, method: 'CONNECT',
      path: `${u.hostname}:${port}`,
    });
    req.on('connect', (res, socket) => {
      if (res.statusCode !== 200) return reject(new Error('CONNECT failed ' + res.statusCode));
      const t = tls.connect({ socket, servername: u.hostname, rejectUnauthorized: false }, () => resolve(t));
      t.on('error', reject);
    });
    req.on('error', reject);
    req.end();
  });
}

async function get(urlStr, redirects = 0) {
  if (redirects > 5) throw new Error('too many redirects');
  const u = new URL(urlStr);
  const sock = await connect(urlStr);
  const body = await new Promise((resolve, reject) => {
    const r = https.request({
      host: u.hostname, path: u.pathname + u.search, method: 'GET', createConnection: () => sock,
      headers: { 'user-agent': 'Mozilla/5.0', 'accept': '*/*' },
    }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        return resolve({ redirect: new URL(res.headers.location, urlStr).toString() });
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, buf: Buffer.concat(chunks) }));
    });
    r.on('error', reject);
    r.end();
  });
  if (body.redirect) return get(body.redirect, redirects + 1);
  return body;
}

const out = process.argv[2];
const urls = process.argv.slice(3);
fs.mkdirSync(out, { recursive: true });
for (const url of urls) {
  try {
    const r = await get(url);
    let name = new URL(url).pathname.replace(/^\/+/, '').replace(/[\/\\]/g, '_') || 'index.html';
    if (name === 'README.md') name = 'README.md';
    fs.writeFileSync(`${out}/${name}`, r.buf);
    console.log(`OK ${r.status} ${r.buf.length} ${url} -> ${name}`);
  } catch (e) {
    console.log(`FAIL ${url} :: ${e.message}`);
  }
}
