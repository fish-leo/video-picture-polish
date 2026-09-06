#!/bin/bash
# ⚡ ALH Pro · Mac 工作站 启动器 (双击运行)
# 自动: 清理旧进程 → 起服务 → 等就绪 → 打开浏览器
cd "$(dirname "$0")"
cd ../ui 2>/dev/null || cd ~/Projects/alh-pro-mac/ui

PY=~/Projects/alh-pro-mac/.venv/bin/python
PORT=8456

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   ALH Pro · Mac 工作站                    ║"
echo "  ║   图片超分 / 抠图 / 视频去重 / AI补帧     ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# 清理占用端口的旧进程
OLD=$(lsof -ti:$PORT 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "  检测到旧服务进程, 正在清理…"
  kill -9 $OLD 2>/dev/null
  sleep 0.6
fi

# 起服务
"$PY" ui_server.py &
SRV=$!

# 等端口就绪 (最多 10 秒)
for i in $(seq 1 33); do
  curl -s -o /dev/null http://127.0.0.1:$PORT/api/health && break
  sleep 0.3
done

# 打开浏览器
open "http://127.0.0.1:$PORT"

echo ""
echo "  ✅ 工作站已启动 → http://127.0.0.1:$PORT"
echo "  ⏹ 关闭本窗口即停止服务。"
echo ""
echo "  ── 服务日志 ──"
wait $SRV
