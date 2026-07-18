const app = require('express')();
const server = require('http').createServer(app);
const io = require('socket.io')(server);
const bodyParser = require('body-parser');

const crypto = require('crypto');
const socketMap = new Map();
const bridgeSecret = process.env.BRIDGE_SECRET || '';

app.use(bodyParser.json({ limit: '64kb' }));

app.get('/', (req, res) => {
  res.send('Hello World!');
});

function authorized(req) {
  const supplied = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
  return bridgeSecret && supplied.length === bridgeSecret.length &&
    crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(bridgeSecret));
}

app.get('/health', (req, res) => {
  if (!authorized(req)) return res.status(401).json({ ok: false });
  res.json({ ok: true });
});

app.post('/request', async (req, res) => {
  if (!authorized(req)) {
    res.status(401).send("桥接鉴权失败");
    return;
  }
  const workspaceId = req.body?.workspace_id;
  const request = req.body?.request;
  if (!/^[a-f0-9-]{36}$/i.test(workspaceId || '')) {
    res.status(400).send("工作空间无效");
    return;
  }
  if (request?.url !== "https://edith.xiaohongshu.com/api/sns/web/v1/feed" || request?.method !== "POST") {
    res.status(403).send("只允许请求小红书笔记详情接口");
    return;
  }
  const socket = socketMap.get(workspaceId);
  if (!socket) {
    res.status(412).send("当前工作空间浏览器尚未连接");
    return;
  }
  try {
    const response = await socket.timeout(10000).emitWithAck("request", request);
    if (response.error) {
      res.status(response.status || 500).send(response.error);
    } else {
      if (Buffer.byteLength(JSON.stringify(response), 'utf8') > 2 * 1024 * 1024) {
        return res.status(502).send("详情响应过大");
      }
      res.send(response);
    }
  } catch (err) {
    console.error('request error', err);
    res.status(500).send(err.message);
  }
});

app.post('/cookies', async (req, res) => {
  res.status(403).send("Cookie写入功能已禁用");
});

io.on('connection', (socket) => {
  const workspaceId = String(socket.handshake.auth?.workspace || '');
  const supplied = String(socket.handshake.auth?.token || '');
  const authorized = bridgeSecret && supplied.length === bridgeSecret.length &&
    crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(bridgeSecret));
  if (!authorized || !/^[a-f0-9-]{36}$/i.test(workspaceId)) {
    socket.disconnect(true);
    return;
  }
  const previous = socketMap.get(workspaceId);
  if (previous && previous.id !== socket.id) previous.disconnect(true);
  console.log('工作空间客户端连接:', workspaceId, socket.id);
  socketMap.set(workspaceId, socket);
  socket.on('disconnect', () => {
    console.log('工作空间客户端断开:', workspaceId, socket.id);
    if (socketMap.get(workspaceId)?.id === socket.id) socketMap.delete(workspaceId);
  });
});

const port = process.env.PORT || 3000;

server.listen(port, "127.0.0.1", () => {
  console.log(`Server started on http://127.0.0.1:${port}`);
});
