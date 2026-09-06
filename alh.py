#!/usr/bin/env python3
"""
alh — ALH Pro Mac 等效工具链 (统一入口)
图片超分 / AI抠图 / 视频去重 / 视频补帧(RIFE) / 视频超分

用法:
  alh probe <video>                     视频体检(帧率/重复帧报告)
  alh dedup <video> -o out.mp4          去除重复帧(动画拍二/拍三)
  alh rife <video> -x 2|4|8 -o out.mp4  RIFE AI补帧(2x默认)
  alh pipe <video> [-x 4] -o out.mp4    去重→补帧 推荐流程
  alh img-up <img> [-s 4] [--anime]     图片AI超分(Real-ESRGAN/waifu2x)
  alh img-rmbg <img> [-m u2net]         图片AI抠图(rembg)
  alh video-up <video> [-s 4]           视频AI超分(逐帧, 慢, 慎用)
"""
import os, sys, subprocess, argparse, shutil, tempfile, glob, json

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "Projects", "alh-pro-mac")
ENGINES = os.path.join(BASE, "engines")
REALESRGAN = os.path.join(ENGINES, "realesrgan-ncnn-vulkan-v0.2.0-macos", "realesrgan-ncnn-vulkan")
REALESRGAN_MODELS = os.path.join(ENGINES, "realesrgan-ncnn-vulkan-v0.2.0-macos", "models")
W2X = os.path.join(ENGINES, "waifu2x-ncnn-vulkan-20250915-macos", "waifu2x-ncnn-vulkan")
W2X_DIR = os.path.join(ENGINES, "waifu2x-ncnn-vulkan-20250915-macos")
RIFE = os.path.join(ENGINES, "rife-ncnn-vulkan-20221029-macos", "rife-ncnn-vulkan")
RIFE_MODELS = os.path.join(ENGINES, "rife-ncnn-vulkan-20221029-macos")
VENV_PY = os.path.join(BASE, ".venv", "bin", "python")
REMBG = os.path.join(BASE, ".venv", "bin", "rembg")
DEDUP = os.path.join(BASE, "scripts", "dedup.py")
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

def run(cmd, verbose=False, **kw):
    if verbose: print("  $", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0 and verbose:
        print(r.stderr[-800:])
    return r

def ensure_video(v):
    if not os.path.exists(v): sys.exit(f"文件不存在: {v}")
    if not shutil.which("ffmpeg"): sys.exit("需要 ffmpeg (/opt/homebrew/bin/ffmpeg)")

def probe_video(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration,codec_name",
             "-of", "json", path])
    s = json.loads(r.stdout)["streams"][0]
    n, d = map(int, s.get("r_frame_rate", "0/1").split("/"))
    s["fps"] = n / d if d else 0
    return s

def has_audio(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", path])
    return bool(r.stdout.strip())

# ---------- 图片超分 ----------
def img_up(args):
    ensure_video(args.input)
    # 输出路径: --jpg 时目标即为 .jpg, 引擎先写临时 png
    if args.jpg and not args.out:
        args.out = os.path.splitext(args.input)[0] + f"_x{args.scale}.jpg"
    out = args.out or os.path.splitext(args.input)[0] + f"_x{args.scale}.png"
    engine_out = out + ".src.png" if args.jpg else out
    if args.anime:
        # waifu2x 固定 2x 模型; 动漫插画专用
        if args.scale != 2:
            print(f"[img-up] waifu2x 仅支持 2x, 已强制 2x")
            args.scale = 2
        cmd = [W2X, "-i", args.input, "-o", engine_out, "-s", str(args.scale),
               "-m", os.path.join(W2X_DIR, "models-cunet")]
        if args.noise: cmd += ["-n", str(args.noise)]
    else:
        model = "realesr-animevideov3" if args.anime_style else "realesrgan-x4plus"
        if args.scale not in (1, 2, 3, 4):
            sys.exit("Real-ESRGAN 仅支持 1-4x")
        cmd = [REALESRGAN, "-i", args.input, "-o", engine_out, "-n", model, "-s", str(args.scale),
               "-m", REALESRGAN_MODELS, "-f", "png"]
    print(f"[img-up] {'waifu2x-cunet(动漫)' if args.anime else model} {args.scale}x → {out}")
    r = run(cmd)
    if r.returncode != 0 or not os.path.exists(engine_out):
        print(r.stderr[-800:]); sys.exit("超分失败")
    if args.jpg:
        run([FFMPEG, "-y", "-v", "error", "-i", engine_out, "-q:v", "2", out])
        os.remove(engine_out)
    if not os.path.exists(out):
        sys.exit("输出转换失败")
    print(f"[img-up] ✅ {out}")

# ---------- 抠图 ----------
def img_rmbg(args):
    if not os.path.exists(REMBG): sys.exit("rembg 环境未装好, 先跑: ~/Projects/alh-pro-mac/.venv/bin/pip install rembg")
    out = args.out or os.path.splitext(args.input)[0] + "_rmbg.png"
    cmd = [REMBG, "i", "-m", args.model, args.input, out]
    if args.alpha: cmd = [REMBG, "i", "-a", "-m", args.model, args.input, out]
    print(f"[img-rmbg] 模型 {args.model} → {out}")
    r = run(cmd, timeout=900)
    if r.returncode != 0 or not os.path.exists(out):
        print(r.stderr[-800:]); sys.exit("抠图失败")
    print(f"[img-rmbg] ✅ {out}")

# ---------- 去重 ----------
def dedup(args):
    ensure_video(args.input)
    cmd = [sys.executable, DEDUP, "fix", args.input, "-o", args.out]
    if args.sample: cmd += ["--sample", str(args.sample)]
    r = run(cmd, timeout=3600*6)
    if r.returncode != 0: print(r.stderr[-800:]); sys.exit("去重失败")
    print(r.stdout)

# ---------- RIFE 补帧 ----------
def rife(args):
    ensure_video(args.input)
    x = args.x  # 总倍率, 必须是2的幂(每轮2x)
    if x < 2 or (x & (x - 1)): sys.exit("-x 必须是2的幂 (2/4/8...)")
    info = probe_video(args.input)
    out_fps = info["fps"] * x
    tmp = tempfile.mkdtemp(prefix="alh_rife_")
    try:
        fin, fout = os.path.join(tmp, "in"), os.path.join(tmp, "out")
        os.makedirs(fin); os.makedirs(fout)
        print(f"[rife] 拆帧 {info['width']}x{info['height']}@{info['fps']:.2f}fps ...")
        run([FFMPEG, "-y", "-v", "error", "-i", args.input, os.path.join(fin, "%08d.png")])
        n = len(glob.glob(os.path.join(fin, "*.png")))
        print(f"[rife] {n} 帧 → {x}x 补帧 (目标 {out_fps:.1f}fps)")
        rounds = x.bit_length() - 1  # 2x 轮数
        cur_in, cur_out = fin, fout
        for rd in range(rounds):
            cur_out = os.path.join(tmp, f"out{rd}")
            os.makedirs(cur_out)
            r = run([RIFE, "-i", cur_in, "-o", cur_out, "-m", os.path.join(RIFE_MODELS, "rife-v2.3")], timeout=3600*12)
            if r.returncode != 0: print(r.stderr[-500:]); sys.exit(f"RIFE 第{rd+1}轮失败")
            m = len(glob.glob(os.path.join(cur_out, "*.png")))
            print(f"[rife] 第{rd+1}轮: {m} 帧")
            cur_in = cur_out
        # 合帧 + 音频
        aac = has_audio(args.input)
        cmd = [FFMPEG, "-y", "-v", "error", "-framerate", f"{out_fps:.6f}"]
        cmd += ["-i", os.path.join(cur_in, "%08d.png")]
        if aac:
            au = os.path.join(tmp, "audio.m4a")
            run([FFMPEG, "-y", "-v", "error", "-i", args.input, "-vn", "-acodec", "copy", au])
            cmd += ["-i", au, "-c:a", "copy"]
        cmd += ["-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset, "-pix_fmt", "yuv420p", args.out]
        r = run(cmd)
        if r.returncode != 0: print(r.stderr[-500:]); sys.exit("合帧失败")
        print(f"[rife] ✅ {args.out} ({out_fps:.1f}fps)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------- 去重→补帧 推荐流程 ----------
def pipe(args):
    ensure_video(args.input)
    dedup_out = os.path.splitext(args.out)[0] + "_dedup.mp4"
    # 1. 去重
    print(f"═══ 第1步: 去重 ═══")
    r = run([sys.executable, DEDUP, "fix", args.input, "-o", dedup_out], timeout=3600*6)
    if r.returncode != 0: print(r.stderr[-800:]); sys.exit("去重失败")
    print(r.stdout)
    import re
    m = re.search(r"估计真实帧率 ([0-9.]+) fps", r.stdout)
    content_fps = float(m.group(1)) if m else None
    # 2. 补帧到目标 fps
    import math
    if content_fps and content_fps >= args.target * 0.9:
        print(f"[pipe] 内容已 {content_fps:.1f}fps (≥目标{args.target}), 无需补帧, 输出去重版")
        os.rename(dedup_out, args.out)
        print(f"[pipe] ✅ {args.out}")
        return
    print(f"═══ 第2步: 补帧 {content_fps}→目标{args.target}fps ═══")
    ratio = args.target / content_fps if content_fps else 2
    x = 2 ** max(1, math.ceil(math.log2(max(ratio, 2))))  # 2的幂且≥2
    info = probe_video(dedup_out)
    tmp = tempfile.mkdtemp(prefix="alh_pipe_")
    try:
        fin = os.path.join(tmp, "in"); os.makedirs(fin)
        run([FFMPEG, "-y", "-v", "error", "-i", dedup_out, os.path.join(fin, "%08d.png")])
        n = len(glob.glob(os.path.join(fin, "*.png")))
        rounds = x.bit_length() - 1
        cur_in = fin
        for rd in range(rounds):
            cur_out = os.path.join(tmp, f"out{rd}"); os.makedirs(cur_out)
            run([RIFE, "-i", cur_in, "-o", cur_out, "-m", os.path.join(RIFE_MODELS, "rife-v2.3")], timeout=3600*12)
            cur_in = cur_out
        # 实际输出帧率 = 内容帧率 * x
        actual_fps = (content_fps or info["fps"]) * x
        aac = has_audio(dedup_out)
        cmd = [FFMPEG, "-y", "-v", "error", "-framerate", f"{actual_fps:.6f}", "-i", os.path.join(cur_in, "%08d.png")]
        if aac:
            au = os.path.join(tmp, "audio.m4a")
            run([FFMPEG, "-y", "-v", "error", "-i", dedup_out, "-vn", "-acodec", "copy", au])
            cmd += ["-i", au, "-c:a", "copy"]
        cmd += ["-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset, "-pix_fmt", "yuv420p", args.out]
        r = run(cmd)
        if r.returncode != 0: print(r.stderr[-500:]); sys.exit("合帧失败")
        print(f"[pipe] ✅ {args.out} (内容{content_fps}fps → {actual_fps:.1f}fps)")
        os.remove(dedup_out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def main():
    ap = argparse.ArgumentParser(description="alh — Mac 视频/图片 AI 增强工具链 (ALH Pro 等效)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="视频体检(编码信息+重复帧报告)")
    p.add_argument("input"); p.add_argument("--sample", type=float, default=None)
    def probe_cmd(a):
        cmd = [sys.executable, DEDUP, "probe", a.input]
        if a.sample: cmd += ["--sample", str(a.sample)]
        r = run(cmd, timeout=3600)
        print(r.stdout or r.stderr)
        sys.exit(r.returncode)
    p.set_defaults(func=probe_cmd)

    p = sub.add_parser("dedup", help="去除重复帧")
    p.add_argument("input"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--sample", type=float, default=None)
    p.set_defaults(func=dedup)

    p = sub.add_parser("rife", help="RIFE AI 补帧")
    p.add_argument("input"); p.add_argument("-o", "--out", required=True)
    p.add_argument("-x", type=int, default=2, help="倍率(2的幂: 2/4/8)")
    p.add_argument("--crf", type=int, default=16); p.add_argument("--preset", default="medium")
    p.set_defaults(func=rife)

    p = sub.add_parser("pipe", help="去重→补帧 一键流程")
    p.add_argument("input"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--target", type=float, default=60, help="目标帧率(默认60)")
    p.add_argument("--crf", type=int, default=16); p.add_argument("--preset", default="medium")
    p.set_defaults(func=pipe)

    p = sub.add_parser("img-up", help="图片 AI 超分")
    p.add_argument("input"); p.add_argument("-s", "--scale", type=int, default=4)
    p.add_argument("--anime", action="store_true", help="用 waifu2x(动漫插画)")
    p.add_argument("--anime-style", action="store_true", help="Real-ESRGAN 动漫模型")
    p.add_argument("--noise", type=int, default=0, help="waifu2x 降噪 0-3")
    p.add_argument("-o", "--out"); p.add_argument("--jpg", action="store_true", help="输出转 jpg")
    p.add_argument("--keep-png", action="store_true")
    p.set_defaults(func=img_up)

    p = sub.add_parser("img-rmbg", help="图片 AI 抠图")
    p.add_argument("input")
    p.add_argument("-m", "--model", default="u2net", help="u2net/isnet-general-use/birefnet-general (默认u2net)")
    p.add_argument("-a", "--alpha", action="store_true", help="输出透明PNG(默认白底)")
    p.add_argument("-o", "--out")
    p.set_defaults(func=img_rmbg)

    p = sub.add_parser("video-up", help="视频 AI 超分(逐帧 Real-ESRGAN, 很慢)")
    p.add_argument("input"); p.add_argument("-s", "--scale", type=int, default=4)
    p.add_argument("-o", "--out"); p.add_argument("--jobs", type=int, default=1)
    p.set_defaults(func=lambda a: sys.exit("video-up 开发中, 先用 img-up + 手动逐帧(见 README)"))

    a = ap.parse_args()
    a.func(a)

if __name__ == "__main__":
    main()
