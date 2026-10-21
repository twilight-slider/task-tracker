// Bounded stdio to Unix socket adapter; runs as UID 1000, never loads Train Driver code.
import net from 'node:net';

const socketPath = '/run/td/helper.sock';
const maxBytes = 2 * 1024 * 1024;
async function forward(line) {
  try {
    const response = await new Promise((resolve,reject) => {
      const client = net.createConnection(socketPath);
      const chunks = [];
      let bytes = 0;
      client.setTimeout(12000, () => client.destroy(new Error('timeout')));
      client.on('connect', () => client.write(line));
      client.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > maxBytes) client.destroy(new Error('oversize'));
        else chunks.push(chunk);
      });
      client.on('end', () => resolve(Buffer.concat(chunks).toString('utf8').trim()));
      client.on('error', reject);
    });
    if (response) process.stdout.write(response + '\n');
  } catch (error) {
    process.stderr.write(`Train Driver helper transport: ${error.code || error.name}\n`);
    process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:null,error:{code:-32000,message:'helper unavailable'}}) + '\n');
  }
}
let buffered = Buffer.alloc(0);
let discarding = false;
for await (const chunk of process.stdin) {
  buffered = Buffer.concat([buffered, chunk]);
  for (;;) {
    const newline = buffered.indexOf(10);
    if (newline < 0) break;
    const line = buffered.subarray(0, newline + 1);
    buffered = buffered.subarray(newline + 1);
    if (discarding || line.length > maxBytes) process.stdout.write('{"error":"oversize"}\n');
    else await forward(line);
    discarding = false;
  }
  if (buffered.length > maxBytes) { buffered = Buffer.alloc(0); discarding = true; }
}
