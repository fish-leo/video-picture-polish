#!/usr/bin/env python3
"""
alh UI server — ALH Pro Mac 工作站 后端
- 本地文件浏览(素材盘 GB 级视频不需要上传)
- 任务系统: 去重/补帧/一键/超分/抠图, 后台线程 + 实时进度/日志
- Range 媒体流(浏览器直接预览源视频与结果)
"""
import os, sys, re, json, time, uuid, mimetypes, subprocess, threading, signal
from pathlib import Path
from flask import Flask, request, jsonify, Response, send_from_directory, abort

BASE = Path.home() / "Projects" / "alh-pro-mac"
ALH = BASE / "alh.py"
DEDUP = BASE / "scripts" / "dedup.py"
PY = sys.executable  # 由 .venv python 启动 → 用同一解释器调子任务
UI_DIR = Path(__file__).parent
PORT = 8456

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv", ".m4v", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

# ---------- 任务管理 ----------
TASKS = {}
TASKS_LOCK = threading.Lock()
OUT_SUFFIX = {"dedup": "_dedup", "rife": "_rife", "pipe": "_enhanced"}

def new_id():
    return uuid.uuid4().hex[:8]

class Task:
    def __init__(self, op, path, out, args):
        self.id = new_id()
        self.op = op
        self.path = path
        self.out = out
        self.args = args
        self.status = "queued"          # queued/running/done/error/aborted
        self.progress = 0.0
        self.stage = "排队中…"
        self.log = []
        self.created = time.time()
        self.done_at = None
        self.result = None
        self.error = None
        self.proc = None

    def add_log(self, line):
        line = line.rstrip("\n")
        if line:
            self.log.append(line)
            if len(self.log) > 300:
                self.log = self.log[-300:]

    def to_dict(self):
        return {
            "id": self.id, "op": self.op, "path": self.path, "out": self.out,
            "status": self.status, "progress": round(self.progress, 1),
            "stage": self.stage, "log": self.log[-60:],
            "created": self.created, "done_at": self.done_at,
            "result": self.result, "error": self.error,
            "args": self.args,
        }

def out_path_for(video_path, op, suffix=None):
    p = Path(video_path)
    sfx = suffix or OUT_SUFFIX[op]
    return str(p.with_name(p.stem + sfx + p.suffix))

def img_out_for(img_path, op, args):
    p = Path(img_path)
    if op == "img-up":
        scale = int(args.get("scale", 4))
        ext = ".jpg" if args.get("jpg") else ".png"
        return str(p.with_name(p.stem + f"_x{scale}" + ext))
    return str(p.with_name(p.stem + "_rmbg.png"))

def parse_engine_progress(task, line):
    """从引擎输出解析进度"""
    m = re.search(r"(\d{1,3}(?:\.\d+)?)%", line)
    if m and task.op in ("img-up",):
        task.progress = min(99.0, float(m.group(1)))
        task.stage = f"AI 超分中… {float(m.group(1)):.0f}%"
        return
    if task.op == "rife":
        m1 = re.search(r"拆帧", line);  m2 = re.search(r"第(\d+)轮: (\d+) 帧", line)
        m3 = re.search(r"✅", line);    m4 = re.search(r"合帧|Encoding", line)
        if m1: task.progress, task.stage = 8, "拆帧…"
        elif m2:
            rd, nf = int(m2.group(1)), int(m2.group(2))
            rounds = task.args.get("rounds", 2)
            task.progress = min(80, 15 + (rd / rounds) * 65)
            task.stage = f"AI 补帧中 第{rd}/{rounds}轮 ({nf} 帧)…"
        elif m4: task.progress, task.stage = 88, "合帧编码…"
        elif m3: task.progress, task.stage = 100, "完成"
    elif task.op == "pipe":
        m0 = re.search(r"第1步", line);  m1 = re.search(r"第(\d+)轮: (\d+) 帧", line)
        m2 = re.search(r"去重完成", line); m3 = re.search(r"✅", line)
        m4 = re.search(r"补帧 ([\d.]+)→目标([\d.]+)fps", line)
        if m0: task.progress, task.stage = 4, "去重: 分析重复帧…"
        elif m2: task.progress, task.stage = 20, "去重完成, 准备补帧…"
        elif m4:
            task.stage = f"补帧 {m4.group(1)}→目标{m4.group(2)}fps…"
        elif m1:
            rd = int(m1.group(1))
            task.progress = min(85, 30 + rd * 12)
            task.stage = f"AI 补帧中 第{rd}轮 ({m1.group(2)} 帧)…"
        elif m3: task.progress, task.stage = 100, "完成"
    elif task.op == "dedup":
        if re.search(r"去重完成|✅", line):
            task.progress, task.stage = 100, "完成"
        elif re.search(r"检测|分析", line):
            task.progress, task.stage = 40, "检测重复帧…"

def run_task(tid):
    t = TASKS[tid]
    t.status = "running"
    t.stage = "启动引擎…"
    rounds = t.args.get("rounds")
    cmd = [PY, "-u", str(ALH)]  # -u: Python 无缓冲, 否则 print 卡在管道里进度不实时
    if t.op == "dedup":
        cmd += ["dedup", t.path, "-o", t.out]
    elif t.op == "rife":
        cmd += ["rife", t.path, "-o", t.out, "-x", str(t.args["x"])]
        if t.args.get("crf"): cmd += ["--crf", str(t.args["crf"])]
    elif t.op == "pipe":
        cmd += ["pipe", t.path, "-o", t.out, "--target", str(t.args.get("target", 60))]
        if t.args.get("crf"): cmd += ["--crf", str(t.args["crf"])]
    elif t.op == "img-up":
        cmd += ["img-up", t.path, "-s", str(t.args.get("scale", 4)), "-o", t.out]
        if t.args.get("anime"): cmd += ["--anime"]
        if t.args.get("anime_style"): cmd += ["--anime-style"]
        if t.args.get("jpg"): cmd += ["--jpg"]
        if t.args.get("noise"): cmd += ["--noise", str(t.args["noise"])]
    elif t.op == "img-rmbg":
        cmd += ["img-rmbg", t.path, "-o", t.out, "-m", t.args.get("model", "u2net")]
        if t.args.get("alpha"): cmd += ["-a"]
    t.add_log("$ " + " ".join(cmd))
    try:
        t.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, start_new_session=True)
        for line in t.proc.stdout:
            t.add_log(line)
            parse_engine_progress(t, line)
        code = t.proc.wait()
        if code == 0 and os.path.exists(t.out):
            t.status, t.progress, t.stage = "done", 100, "完成"
            t.result = {"out": t.out, "size": os.path.getsize(t.out)}
            t.add_log(f"✅ 完成: {t.out}")
        elif code == -signal.SIGTERM or code == -9:
            t.status, t.stage = "aborted", "已取消"
        else:
            t.status, t.stage, t.error = "error", "失败", "退出码 " + str(code)
    except Exception as e:
        t.status, t.stage, t.error = "error", "失败", str(e)
    t.done_at = time.time()

# ---------- 工具函数 ----------
FFPROBE = "/opt/homebrew/bin/ffprobe"
def video_info(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate,duration,codec_name,nb_frames",
                        "-of", "json", path], capture_output=True, text=True)
    try:
        s = json.loads(r.stdout)["streams"][0]
    except Exception:
        return None
    n, d = map(int, s.get("r_frame_rate", "0/1").split("/"))
    s["fps"] = round(n / d, 3) if d else 0
    s["duration"] = round(float(s.get("duration", 0) or 0), 1)
    ha = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0",
                         "-show_entries", "stream=index", "-of", "csv=p=0", path],
                        capture_output=True, text=True)
    s["audio"] = bool(ha.stdout.strip())
    return s

def human_size(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024: return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024

def safe_path(p):
    p = os.path.abspath(os.path.expanduser(p))
    return p if os.path.exists(p) else None

# ---------- 路由 ----------
@app.get("/")
def index():
    return send_from_directory(UI_DIR, "index.html")

@app.get("/api/health")
def health():
    eng = {
        "realesrgan": os.path.exists(BASE / "engines/realesrgan-ncnn-vulkan-v0.2.0-macos/realesrgan-ncnn-vulkan"),
        "waifu2x": os.path.exists(BASE / "engines/waifu2x-ncnn-vulkan-20250915-macos/waifu2x-ncnn-vulkan"),
        "rife": os.path.exists(BASE / "engines/rife-ncnn-vulkan-20221029-macos/rife-ncnn-vulkan"),
        "rembg": os.path.exists(BASE / ".venv/bin/rembg"),
        "ffmpeg": os.path.exists("/opt/homebrew/bin/ffmpeg"),
    }
    return jsonify({"ok": all(eng.values()), "engines": eng, "port": PORT})

@app.get("/api/roots")
def roots():
    """通用快捷入口: 家/下载/桌面/影片 + 自动发现外置卷(/Volumes/*)。
    本地私有快捷入口可放 ~/Projects/alh-pro-mac/local_roots.json
    (gitignore, 不入库): [{"label":"我的素材", "path":"/Volumes/X/..."}]"""
    roots = []
    home = str(Path.home())
    for label, p in [
        ("家目录", home),
        ("下载", str(Path.home() / "Downloads")),
        ("桌面", str(Path.home() / "Desktop")),
        ("影片", str(Path.home() / "Movies")),
    ]:
        if os.path.isdir(p):
            roots.append({"label": label, "path": p})
    # 自动发现外置卷
    vols = Path("/Volumes")
    if vols.is_dir():
        for v in sorted(vols.iterdir()):
            if v.is_dir() and not v.name.startswith("."):
                roots.append({"label": f"外置盘 · {v.name}", "path": str(v)})
    # 私有快捷入口 (本地可选)
    local_cfg = BASE / "local_roots.json"
    if local_cfg.exists():
        try:
            extra = json.loads(local_cfg.read_text())
            for item in extra:
                if os.path.isdir(item.get("path", "")):
                    roots.append({"label": item.get("label", "快捷入口"),
                                  "path": item["path"]})
        except Exception:
            pass
    return jsonify(roots)

@app.get("/api/fs")
def fs():
    p = request.args.get("path", "").strip()
    if not p:
        p = str(Path.home())  # 默认家目录
    p = safe_path(p)
    if not p or not os.path.isdir(p):
        return jsonify({"error": "目录不存在"}), 404
    dirs, files = [], []
    try:
        for name in sorted(os.listdir(p)):
            if name.startswith("."): continue
            full = os.path.join(p, name)
            if os.path.isdir(full):
                dirs.append({"name": name + "/", "path": full, "kind": "dir"})
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in VIDEO_EXT:
                    try:
                        sz = os.path.getsize(full)
                        files.append({"name": name, "path": full, "kind": "video",
                                      "size": sz, "size_h": human_size(sz),
                                      "mtime": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(full)))})
                    except OSError: pass
                elif ext in IMAGE_EXT:
                    try:
                        sz = os.path.getsize(full)
                        files.append({"name": name, "path": full, "kind": "image",
                                      "size": sz, "size_h": human_size(sz),
                                      "mtime": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(full)))})
                    except OSError: pass
    except PermissionError:
        return jsonify({"error": "无权限"}), 403
    parent = os.path.dirname(p)
    return jsonify({"current": p, "parent": parent if parent != p else None,
                    "dirs": dirs, "files": files})

@app.post("/api/vinfo")
def vinfo():
    p = safe_path((request.json or {}).get("path", ""))
    if not p: return jsonify({"error": "路径无效"}), 400
    info = video_info(p)
    if info is None: return jsonify({"error": "不是有效视频"}), 400
    return jsonify(info)

@app.post("/api/probe")
def probe():
    """快检重复帧(默认抽样前30秒), 返回报告"""
    body = request.json or {}
    p = safe_path(body.get("path", ""))
    if not p: return jsonify({"error": "路径无效"}), 400
    sample = body.get("sample", 30)
    cmd = [PY, "-u", str(DEDUP), "probe", p, "--json"]
    if sample: cmd += ["--sample", str(sample)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return jsonify({"error": r.stderr[-400:] or "分析失败"}), 500
        rep = json.loads(r.stdout)
        rep.pop("new_frame_mask", None)
        return jsonify(rep)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "分析超时"}), 504

@app.post("/api/task")
def create_task():
    body = request.json or {}
    op, p = body.get("op"), safe_path(body.get("path", ""))
    if op not in OUT_SUFFIX and op not in ("img-up", "img-rmbg"):
        return jsonify({"error": "未知操作"}), 400
    if not p: return jsonify({"error": "路径无效"}), 400
    ext = os.path.splitext(p)[1].lower()
    if op in ("img-up", "img-rmbg") and ext not in IMAGE_EXT:
        return jsonify({"error": "需要图片文件"}), 400
    if op in OUT_SUFFIX and ext not in VIDEO_EXT:
        return jsonify({"error": "需要视频文件"}), 400
    # 输出: 源目录同后缀; 可选 out 覆盖
    args = body.get("args", {})
    if op in OUT_SUFFIX:
        out = body.get("out") or out_path_for(p, op)
    else:
        out = body.get("out") or img_out_for(p, op, args)
    if op == "rife":
        x = int(args.get("x", 2))
        args["rounds"] = x.bit_length() - 1
    t = Task(op, p, out, args)
    with TASKS_LOCK:
        TASKS[t.id] = t
    threading.Thread(target=run_task, args=(t.id,), daemon=True).start()
    return jsonify(t.to_dict())

@app.get("/api/task/<tid>")
def task_status(tid):
    t = TASKS.get(tid)
    if not t: return jsonify({"error": "任务不存在"}), 404
    return jsonify(t.to_dict())

@app.get("/api/tasks")
def tasks_list():
    with TASKS_LOCK:
        recent = sorted(TASKS.values(), key=lambda t: t.created, reverse=True)[:8]
        return jsonify([t.to_dict() for t in recent])

@app.post("/api/task/<tid>/abort")
def abort_task(tid):
    t = TASKS.get(tid)
    if not t: return jsonify({"error": "任务不存在"}), 404
    if t.proc and t.status == "running":
        try: os.killpg(os.getpgid(t.proc.pid), signal.SIGTERM)
        except Exception: pass
        t.status, t.stage = "aborted", "正在取消…"
    return jsonify({"ok": True})

@app.get("/api/media")
def media():
    """Range 媒体流(视频拖动/图片预览)"""
    p = safe_path(request.args.get("path", ""))
    if not p or not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    size = os.path.getsize(p)
    mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
    rng = request.headers.get("Range")
    start, end = 0, size - 1
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            if m.group(1): start = int(m.group(1))
            if m.group(2): end = min(int(m.group(2)), size - 1)
    if start >= size: return Response(status=416)
    length = end - start + 1
    headers = {"Content-Range": f"bytes {start}-{end}/{size}",
               "Accept-Ranges": "bytes", "Cache-Control": "no-store",
               "Content-Length": str(length)}
    def gen():
        with open(p, "rb") as f:
            f.seek(start)
            remain = length
            while remain > 0:
                chunk = f.read(min(1 << 20, remain))
                if not chunk: break
                remain -= len(chunk)
                yield chunk
    return Response(gen(), status=206 if rng else 200,
                    mimetype=mime, headers=headers)

@app.post("/api/reveal")
def reveal():
    p = safe_path((request.json or {}).get("path", ""))
    if not p: return jsonify({"error": "路径无效"}), 400
    subprocess.Popen(["open", "-R", p])
    return jsonify({"ok": True})

if __name__ == "__main__":
    print(f"\n  ⚡ ALH Pro · Mac 工作站  http://localhost:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)
