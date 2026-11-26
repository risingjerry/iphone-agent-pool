// A single owned USB connection to WDA's independent MJPEG port. stdout is JPEG
// stream bytes only; this program never binds a listener or touches XCTest.
import http from 'node:http';

// Device-library diagnostics must never corrupt the binary pipe or expose IDs.
for (const method of ['log', 'info', 'warn', 'error', 'debug']) console[method] = () => {};
const [udid, portArgument = '9100', host] = process.argv.slice(2);
if (!/^[A-Za-z0-9-]{8,64}$/.test(udid ?? '') || !/^\d+$/.test(portArgument)) process.exit(1);
const devicePort = Number(portArgument);
if (!Number.isInteger(devicePort) || devicePort < 1024 || devicePort > 65535) process.exit(1);

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
let socket, request, response, agent;
let stopping = false;
const startup = setTimeout(() => stop(1), 5000);
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  clearTimeout(startup);
  response?.destroy();
  request?.destroy();
  socket?.destroy();
  agent?.destroy();
  process.exitCode = code;
  setTimeout(() => process.exit(code), 100).unref();
}
process.on('SIGINT', () => stop());
process.on('SIGTERM', () => stop());
process.stdout.on('error', () => stop());

try {
  if (host) {
    const net = await import('node:net');
    const address = await resolveHost(host, udid);
    socket = await new Promise((resolve, reject) => {
      const connection = net.connect(devicePort, address, () => { connection.off('error', reject); resolve(connection); });
      connection.once('error', reject);
    });
  } else {
    const {default: iosDevice} = await import('appium-ios-device');
    socket = await iosDevice.utilities.connectPort(udid, devicePort);
  }
  if (stopping) socket.destroy();
  else {
    socket.setNoDelay?.(true);
    socket.on('error', () => stop(1));
    // Node's HTTP parser removes chunked-transfer framing before writing stdout.
    // This agent always returns the already connected USB socket: no DNS/TCP hop.
    agent = new http.Agent({keepAlive: false});
    agent.createConnection = () => socket;
    request = http.request({host: '127.0.0.1', port: devicePort, path: '/',
      method: 'GET', agent, headers: {Accept: 'multipart/x-mixed-replace', Connection: 'close'}}, incoming => {
      response = incoming;
      clearTimeout(startup);
      if (incoming.statusCode !== 200) { stop(1); return; }
      incoming.on('error', () => stop(1));
      incoming.on('end', () => stop(1));
      // pipe pauses the response when stdout is full, bounding backpressure.
      incoming.pipe(process.stdout, {end: false});
    });
    // usbmux unpipes its plist reader before handing this socket over, leaving
    // it paused. Resume only after HTTP has attached its response parser; a
    // newly created TCP socket fixture would otherwise hide this USB behavior.
    request.on('socket', assigned => assigned.resume());
    request.setTimeout(5000, () => stop(1));
    request.on('error', () => stop(1));
    request.end();
  }
} catch { stop(1); }
