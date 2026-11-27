// Claude Desktop provides Node. The installation owns MCP tools and all access decisions.
'use strict';
const http = require('node:http');
const https = require('node:https');
const { once } = require('node:events');

const MAX_INPUT = 128 * 1024;
const MAX_OUTPUT = 6 * 1024 * 1024;

function post(url, key, version, body) {
  return new Promise((resolve, reject) => {
    const headers = { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream', 'Content-Length': body.length };
    if (version) headers['MCP-Protocol-Version'] = version;
    const transport = url.protocol === 'http:' ? http : https;
    const request = transport.request(url, { method: 'POST', headers, agent: false }, response => {
      if (response.statusCode === 202) { response.resume(); resolve(null); return; }
      if (response.statusCode !== 200 && response.statusCode !== 400) {
        const error = response.statusCode === 401 ? 'Agent key expired, revoked or invalid.'
          : 'Leti Cook returned HTTP ' + response.statusCode + '.';
        reject(new Error(error));
        response.destroy();
        return;
      }
      if (response.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
        reject(new Error('Invalid MCP response.')); response.destroy(); return;
      }
      let size = 0;
      const chunks = [];
      response.on('data', chunk => {
        size += chunk.length;
        if (size > MAX_OUTPUT) { request.destroy(); reject(new Error('MCP response too large.')); }
        else chunks.push(chunk);
      });
      response.on('error', () => reject(new Error('MCP response interrupted.')));
      response.on('end', () => {
        try {
          const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          const valid = item => item?.jsonrpc === '2.0' && Object.hasOwn(item, 'id')
            && Object.hasOwn(item, 'result') !== Object.hasOwn(item, 'error');
          if (Array.isArray(result) ? !result.length || !result.every(valid) : !valid(result)) throw new Error();
          resolve(result);
        } catch { reject(new Error('Invalid MCP response.')); }
      });
    });
    const timer = setTimeout(() => { reject(new Error('MCP request timed out.')); request.destroy(); }, 45000);
    request.on('close', () => clearTimeout(timer));
    request.on('error', () => reject(new Error('Cannot reach Leti Cook. Check the installation address and tunnel.')));
    request.end(body);
  });
}

async function* messages(input) {
  let parts = [], size = 0;
  for await (const chunk of input) {
    let start = 0;
    while (start < chunk.length) {
      const newline = chunk.indexOf(10, start);
      const part = chunk.subarray(start, newline < 0 ? chunk.length : newline);
      size += part.length;
      if (size <= MAX_INPUT) parts.push(part);
      if (newline < 0) break;
      if (size) yield size <= MAX_INPUT ? Buffer.concat(parts) : null;
      parts = []; size = 0; start = newline + 1;
    }
  }
  if (size) yield size <= MAX_INPUT ? Buffer.concat(parts) : null;
}

function error(id, code, message) { return { jsonrpc: '2.0', id, error: { code, message } }; }

async function main() {
  const url = new URL(process.env.LETI_MCP_URL);
  const key = process.env.LETI_AGENT_KEY || '';
  if (!['http:', 'https:'].includes(url.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
      || url.href !== url.origin + '/mcp') throw new Error();
  if (key.length !== 50 || !/^lic_ro_[A-Za-z0-9_-]{43}$/.test(key)) throw new Error();
  let version;
  for await (const body of messages(process.stdin)) {
    let message, reply;
    if (!body) reply = error(null, -32600, 'MCP message too large.');
    else {
      try { message = JSON.parse(body.toString('utf8')); }
      catch { reply = error(null, -32700, 'Invalid JSON.'); }
      if (!reply) {
        try {
          reply = await post(url, key, version, body);
          if (reply?.error && reply.id == null) {
            if (Array.isArray(message)) {
              const errors = message.filter(item => item?.id != null).map(item => ({ ...reply, id: item.id }));
              if (errors.length) reply = errors;
            } else if (message?.id != null) reply.id = message.id;
          }
          if (message?.method === 'initialize' && /^\d{4}-\d{2}-\d{2}$/.test(reply?.result?.protocolVersion)) {
            version = reply.result.protocolVersion;
          }
        } catch (failure) {
          const batch = Array.isArray(message);
          const errors = (batch ? message : [message]).filter(item => item?.id != null)
            .map(item => error(item.id, -32000, failure.message));
          reply = batch && errors.length ? errors : errors[0];
          if (!reply) process.stderr.write(failure.message + '\n');
        }
      }
    }
    if (reply && !process.stdout.write(JSON.stringify(reply) + '\n')) await once(process.stdout, 'drain');
  }
}
main().catch(() => { process.stderr.write('Check the local MCP address and agent key in Claude Desktop.\n'); process.exitCode = 1; });
