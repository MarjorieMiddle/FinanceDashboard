#!/usr/bin/env node
/**
 * Read-only Firestore exporter using only Node.js built-ins.
 *
 * Usage:
 *   node scripts/export-firestore.mjs --credentials /absolute/path/service-account.json
 *   node scripts/export-firestore.mjs --credentials /absolute/path/service-account.json --output data/firestore-export.json
 */
import { createPrivateKey, createSign } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';

const args = process.argv.slice(2);
const option = name => {
  const index = args.indexOf(name);
  return index === -1 ? undefined : args[index + 1];
};
const credentialsPath = option('--credentials');
const outputPath = resolve(option('--output') || 'data/firestore-export.json');

if (!credentialsPath || args.includes('--help') || args.includes('-h')) {
  console.error('Usage: node scripts/export-firestore.mjs --credentials /absolute/path/service-account.json [--output data/firestore-export.json]');
  process.exit(args.includes('--help') || args.includes('-h') ? 0 : 1);
}

const base64url = value => Buffer.from(typeof value === 'string' ? value : JSON.stringify(value)).toString('base64url');
const apiError = async response => {
  let detail = '';
  try { detail = (await response.json()).error?.message || ''; } catch { /* response was not JSON */ }
  return `${response.status} ${response.statusText}${detail ? `: ${detail}` : ''}`;
};

function serviceAccountToken(credentials) {
  const issuedAt = Math.floor(Date.now() / 1000);
  const header = { alg: 'RS256', typ: 'JWT' };
  const claims = {
    iss: credentials.client_email,
    scope: 'https://www.googleapis.com/auth/datastore',
    aud: 'https://oauth2.googleapis.com/token',
    iat: issuedAt,
    exp: issuedAt + 3600,
  };
  const unsigned = `${base64url(header)}.${base64url(claims)}`;
  const signer = createSign('RSA-SHA256');
  signer.update(unsigned);
  signer.end();
  const signature = signer.sign(createPrivateKey(credentials.private_key), 'base64url');
  return `${unsigned}.${signature}`;
}

function decodeValue(value) {
  if ('nullValue' in value) return null;
  if ('booleanValue' in value) return value.booleanValue;
  if ('integerValue' in value) return Number(value.integerValue);
  if ('doubleValue' in value) return value.doubleValue;
  if ('timestampValue' in value) return value.timestampValue;
  if ('stringValue' in value) return value.stringValue;
  if ('bytesValue' in value) return { __firestoreType: 'bytes', base64: value.bytesValue };
  if ('referenceValue' in value) return { __firestoreType: 'reference', path: value.referenceValue };
  if ('geoPointValue' in value) return { __firestoreType: 'geoPoint', ...value.geoPointValue };
  if ('arrayValue' in value) return (value.arrayValue.values || []).map(decodeValue);
  if ('mapValue' in value) return decodeFields(value.mapValue.fields || {});
  return { __firestoreType: 'unknown', value };
}
const decodeFields = fields => Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, decodeValue(value)]));
const encodePath = path => path.split('/').map(encodeURIComponent).join('/');

const credentials = JSON.parse(await readFile(resolve(credentialsPath), 'utf8'));
if (!credentials.project_id || !credentials.client_email || !credentials.private_key) {
  throw Error('The credentials file must be a Google service-account JSON key.');
}
const assertion = serviceAccountToken(credentials);
const tokenResponse = await fetch('https://oauth2.googleapis.com/token', {
  method: 'POST',
  headers: { 'content-type': 'application/x-www-form-urlencoded' },
  body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer', assertion }),
});
if (!tokenResponse.ok) throw Error(`Could not authorize the service account (${await apiError(tokenResponse)}).`);
const accessToken = (await tokenResponse.json()).access_token;
const database = `projects/${credentials.project_id}/databases/(default)`;
const documentRoot = `${database}/documents`;
const request = async (url, init = {}) => {
  const response = await fetch(url, { ...init, headers: { authorization: `Bearer ${accessToken}`, ...(init.headers || {}) } });
  if (!response.ok) throw Error(`Firestore request failed (${await apiError(response)}).`);
  return response.json();
};

async function collectionIds(parent) {
  const all = [];
  let pageToken;
  do {
    const body = { pageSize: 100 };
    if (pageToken) body.pageToken = pageToken;
    const result = await request(`https://firestore.googleapis.com/v1/${parent}:listCollectionIds`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
    });
    all.push(...(result.collectionIds || []));
    pageToken = result.nextPageToken;
  } while (pageToken);
  return all.sort();
}

async function documentsIn(collectionPath) {
  const all = [];
  let pageToken;
  do {
    const url = new URL(`https://firestore.googleapis.com/v1/${documentRoot}/${encodePath(collectionPath)}`);
    url.searchParams.set('pageSize', '100');
    if (pageToken) url.searchParams.set('pageToken', pageToken);
    const result = await request(url);
    all.push(...(result.documents || []));
    pageToken = result.nextPageToken;
  } while (pageToken);
  return all;
}

let documentCount = 0;
async function exportCollection(collectionPath) {
  const documents = await documentsIn(collectionPath);
  const result = [];
  for (const document of documents) {
    const path = document.name.split('/documents/')[1];
    const id = path.split('/').at(-1);
    documentCount += 1;
    process.stdout.write(`\rExported ${documentCount} document${documentCount === 1 ? '' : 's'}…`);
    const subcollections = {};
    for (const childCollection of await collectionIds(document.name)) {
      subcollections[childCollection] = await exportCollection(`${path}/${childCollection}`);
    }
    result.push({ id, path, data: decodeFields(document.fields || {}), subcollections });
  }
  return result;
}

const collections = {};
for (const collection of await collectionIds(documentRoot)) collections[collection] = await exportCollection(collection);
const exportData = {
  format: 'finance-dashboard-firestore-export/v1',
  projectId: credentials.project_id,
  exportedAt: new Date().toISOString(),
  collections,
};
await mkdir(dirname(outputPath), { recursive: true });
await writeFile(outputPath, `${JSON.stringify(exportData, null, 2)}\n`, { mode: 0o600 });
console.log(`\nDone: ${documentCount} documents saved to ${outputPath}`);
