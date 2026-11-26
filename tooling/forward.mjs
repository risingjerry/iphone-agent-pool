import net from 'node:net';
import iosDevice from 'appium-ios-device';

const [udid, localArgument = '18100', deviceArgument = '8100', host] = process.argv.slice(2);
if (!/^[A-Za-z0-9-]{8,64}$/.test(udid ?? '')) throw new Error('Provide a valid device UDID.');
function port(value) {
  if (!/^\d+$/.test(value)) throw new Error('Ports must be integers.');
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1024 || number > 65535) throw new Error('Ports must be 1024-65535.');
  return number;
}
const localPort = port(localArgument);
const devicePort = port(deviceArgument);
// host "tunnel" = the CoreDevice tunnel Xcode keeps to the phone over USB or
// Wi-Fi; it bypasses VPNs on the phone. Its address changes per connection.
async function resolveHost(host, udid) {
  if (host !== 'tunnel') return host;
  const {execFile} = await import('node:child_process');
  const {mkdtemp, readFile, rm} = await import('node:fs/promises');
  const {tmpdir} = await import('node:os');
  const {join} = await import('node:path');
  const dir = await mkdtemp(join(tmpdir(), 'iphone-use-'));
  try {
    await new Promise((resolve, reject) => execFile('xcrun', ['devicectl', 'device', 'info', 'details', '--device', udid, '-q', '--json-output', join(dir, 'd.json')], {timeout: 15000}, error => error ? reject(error) : resolve()));
    const address = JSON.parse(await readFile(join(dir, 'd.json'), 'utf8')).result?.connectionProperties?.tunnelIPAddress;
    if (!address) throw new Error('CoreDevice tunnel is not up; the phone must be on the same Wi-Fi or USB.');
    return address;
  } finally { await rm(dir, {recursive: true, force: true}); }
}
// Optional device_host (an address, or "tunnel") replaces usbmux. The tunnel
// address is looked up once (devicectl costs ~0.3 s) and again only when a
// connect fails, e.g. after the phone reconnects and gets a new tunnel.
let cachedAddress;
function dial(address) {
  return new Promise((resolve, reject) => {
    // A stale tunnel address may not refuse, so bound the attempt.
    const timer = setTimeout(() => socket.destroy(new Error('connect timeout')), 3000);
    const socket = net.connect(devicePort, address, () => { clearTimeout(timer); socket.off('error', reject); resolve(socket); });
    socket.once('error', error => { clearTimeout(timer); reject(error); });
  });
}
async function connectDevice() {
  if (!host) return iosDevice.utilities.connectPort(udid, devicePort);
  if (cachedAddress) {
    try { return await dial(cachedAddress); } catch { cachedAddress = undefined; }
  }
  cachedAddress = await resolveHost(host, udid);
  return dial(cachedAddress);
}
const sockets = new Set();
let stopping = false;
const server = net.createServer(async client => {
  client.setNoDelay(true);
  sockets.add(client);
  client.on('error', () => client.destroy());
  client.on('close', () => sockets.delete(client));
  try {
    const remote = await connectDevice();
    if (stopping || client.destroyed) { remote.destroy(); return; }
    remote.setNoDelay?.(true);
    sockets.add(remote);
    remote.on('error', error => { console.error(`device socket: ${error.message}`); client.destroy(); });
    remote.on('close', () => { sockets.delete(remote); client.destroy(); });
    client.on('close', () => remote.destroy());
    client.pipe(remote).pipe(client);
  } catch (error) {
    console.error(`device connect failed: ${error.message}`);
    client.destroy();
  }
});
server.on('error', error => { console.error(`USB forward failed: ${error.message}`); process.exitCode = 1; stop(); });
server.listen(localPort, '127.0.0.1', () => console.log(`WDA USB forward listening at 127.0.0.1:${localPort}`));
function stop() {
  if (stopping) return;
  stopping = true;
  for (const socket of sockets) socket.destroy();
  server.close(() => process.exit(process.exitCode ?? 0));
  setTimeout(() => process.exit(process.exitCode ?? 0), 1000).unref();
}
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
