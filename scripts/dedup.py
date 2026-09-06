#!/usr/bin/env python3
"""
Video/Picture Polish - 重复帧检测/去除模块 (视频去重)
原理: 动画/3D渲染视频常以"一拍二/一拍三"输出(每张内容帧重复N次),
     直接补帧会产生粘滞感。去重后拿到干净内容帧序列, 再补帧才顺滑。

用法:
  python3 dedup.py probe <video>          # 只检测, 输出报告
  python3 dedup.py fix <video> -o out.mp4 # 去重输出新视频
"""
import subprocess, sys, os, json, argparse, tempfile, shutil
import numpy as np

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
SCALE_W = 160  # 检测用代理分辨率(足够判断帧差异, 大幅加速)

def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)

def probe_video(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
             "-of", "json", path])
    s = json.loads(r.stdout)["streams"][0]
    fr = s.get("r_frame_rate", "0/1")
    num, den = map(int, fr.split("/"))
    s["fps"] = num / den if den else 0
    return s

def read_frames_raw(path, max_frames=None):
    """ffmpeg 抽灰度低分辨率帧, yield numpy arrays"""
    vf = f"scale={SCALE_W}:-2,format=gray"
    if max_frames:
        vf += f",trim=duration={max_frames/fps_hint}" if False else ""
    cmd = [FFMPEG, "-v", "error", "-i", path, "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # 先探测实际输出尺寸
    info = probe_video(path)
    iw, ih = info["width"], info["height"]
    h = int(ih * SCALE_W / iw) // 2 * 2
    framesize = SCALE_W * h
    n = 0
    while True:
        if max_frames and n >= max_frames:
            break
        buf = p.stdout.read(framesize)
        if len(buf) < framesize:
            break
        yield np.frombuffer(buf, dtype=np.uint8).reshape(h, SCALE_W).astype(np.int16)
        n += 1
    p.stdout.close()
    p.wait()

def analyze(path, sample_seconds=None):
    """返回: 帧差异序列 + 去重判定 + 统计报告"""
    info = probe_video(path)
    fps = info["fps"]
    diffs = []
    prev = None
    max_frames = int(fps * sample_seconds) if sample_seconds else None
    for i, frame in enumerate(read_frames_raw(path, max_frames)):
        if prev is not None:
            d = float(np.mean(np.abs(frame - prev)))
            diffs.append(d)
        prev = frame
    return info, diffs

def detect(path, sample_seconds=None, diff_threshold=0.8):
    """核心: 检测每帧是否为重复帧(与前一帧几乎相同)
    阈值逻辑: 真实重复帧(编码复制)差异≈0-0.3; 慢速内容运动通常>1.5。
    默认0.8 区分两者; 若判出>90%重复(可疑), 降阈值0.5重试。
    """
    info, diffs = analyze(path, sample_seconds)
    fps = info["fps"]
    def classify(th):
        repeated = [d < th for d in diffs]
        return [True] + [not r for r in repeated]
    is_new = classify(diff_threshold)
    # 异常保护: 几乎全重复说明阈值仍过高(静态/极慢源或测试图案), 降阈值重试
    for th in (0.8, 0.5):
        if sum(is_new) / max(len(is_new), 1) < 0.1:
            is_new = classify(th)
            diff_threshold = th
        else:
            break
    # 统计重复节奏(连续重复次数分布): 找每个内容帧后跟的重复帧数
    runs = []
    i = 0
    n = len(is_new)
    while i < n:
        if is_new[i]:
            j = i + 1
            while j < n and not is_new[j]:
                j += 1
            runs.append(j - i)  # 该内容帧总共出现的次数
            i = j
        else:
            i += 1
    from collections import Counter
    run_counter = Counter(runs)
    total_frames = len(is_new)
    content_frames = sum(is_new)
    dup_frames = total_frames - content_frames
    report = {
        "video": os.path.basename(path),
        "width": info["width"], "height": info["height"],
        "nominal_fps": round(fps, 3),
        "total_frames_analyzed": total_frames,
        "content_frames": content_frames,
        "duplicate_frames": dup_frames,
        "dup_ratio": round(dup_frames / max(total_frames, 1), 3),
        "est_content_fps": round(fps * content_frames / max(total_frames, 1), 3),
        "rhythm": {str(k): v for k, v in sorted(run_counter.items())},
        "new_frame_mask": is_new,
        "threshold": round(th, 2),
    }
    return report

def fix(path, out_path, threshold=0.8):
    """去重输出: 保留每个内容帧的第一次出现, 重新编码"""
    report = detect(path, diff_threshold=threshold)
    is_new = report["new_frame_mask"]
    if not is_new or all(is_new):
        print(f"[dedup] 未检测到重复帧({report['content_frames']}/{report['total_frames_analyzed']}), 原样复制")
        run([FFMPEG, "-v", "error", "-i", path, "-c", "copy", "-y", out_path])
        return report
    # 用 select filter 按帧号抽取
    keep = [i for i, v in enumerate(is_new) if v]
    expr = "+".join(f"eq(n\\,{i})" for i in keep)
    # 保持原时间基: 抽取后帧率 = 内容帧率。用 -vsync vfr 输出可变帧率
    r = run([FFMPEG, "-v", "error", "-i", path,
             "-vf", f"select='{expr}'", "-vsync", "vfr",
             "-c:v", "libx264", "-crf", "16", "-preset", "medium",
             "-c:a", "copy", "-y", out_path])
    if r.returncode != 0:
        print("[dedup] ffmpeg 错误:", r.stderr.decode()[-500:])
    report["out_path"] = out_path
    report["frames_kept"] = len(keep)
    return report

def main():
    ap = argparse.ArgumentParser(description="视频重复帧检测/去除")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("probe", help="检测并输出报告")
    p1.add_argument("video")
    p1.add_argument("--json", action="store_true")
    p1.add_argument("--sample", type=float, default=None, help="只分析前N秒(快检)")
    p2 = sub.add_parser("fix", help="去重输出新视频")
    p2.add_argument("video")
    p2.add_argument("-o", "--out", required=True)
    p2.add_argument("--threshold", type=float, default=5.0)
    a = ap.parse_args()
    if a.cmd == "probe":
        rep = detect(a.video, a.sample)
        if a.json:
            print(json.dumps({k: v for k, v in rep.items() if k != "new_frame_mask"}, ensure_ascii=False))
        else:
            print(f"文件        : {rep['video']} ({rep['width']}x{rep['height']})")
            print(f"标称帧率    : {rep['nominal_fps']} fps")
            print(f"分析帧数    : {rep['total_frames_analyzed']}")
            print(f"内容帧数    : {rep['content_frames']} (去重后)")
            print(f"重复帧数    : {rep['duplicate_frames']} ({rep['dup_ratio']*100:.0f}%)")
            print(f"估计真实帧率: {rep['est_content_fps']} fps")
            print(f"节奏分布    : {rep['rhythm']}  (键=每内容帧出现次数, 2=拍二/3=拍三)")
            print(f"判定阈值    : {rep['threshold']}")
    elif a.cmd == "fix":
        rep = fix(a.video, a.out, a.threshold)
        print(f"[dedup] 去重完成: {rep['video']}")
        print(f"  内容帧 {rep['content_frames']}/{rep['total_frames_analyzed']}, 重复率 {rep['dup_ratio']*100:.0f}%")
        print(f"  估计真实帧率 {rep['est_content_fps']} fps, 节奏 {rep['rhythm']}")
        print(f"  输出: {a.out}")

if __name__ == "__main__":
    main()
