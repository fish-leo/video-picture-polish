#!/bin/bash
# ⚙️ ALH Pro Mac — 引擎/模型下载安装脚本
# 从官方 GitHub Release 下载 (约 1GB), 结构对齐 engines/ 布局
set -e
cd "$(dirname "$0")/.."          # 项目根
mkdir -p engines && cd engines
echo "下载引擎到 engines/ ..."

dl_unzip() { # $1=url $2=dest_dir
  local f=$(basename "$1")
  echo "→ $f"
  curl -sL --retry 3 -o "$f" "$1"
  mkdir -p "$2"
  unzip -q -o "$f" -d "$2"
  rm "$f"
}

# 1. Real-ESRGAN ncnn-vulkan (macOS universal) — 图片/视频超分
dl_unzip "https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases/download/v0.2.0/realesrgan-ncnn-vulkan-v0.2.0-macos.zip" .
# 模型包在另一个 release (binary zip 的 models/ 为空): zip 内含 realesrgan-ncnn-vulkan-20220424-ubuntu/models/
echo "→ realesrgan models (单独包)"
curl -sL --retry 3 -o _models.zip "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
unzip -q -o _models.zip -d _models_pkg
mkdir -p realesrgan-ncnn-vulkan-v0.2.0-macos/models
cp -r _models_pkg/*/models/* realesrgan-ncnn-vulkan-v0.2.0-macos/models/
rm -rf _models_pkg _models.zip
ls realesrgan-ncnn-vulkan-v0.2.0-macos/models/ | grep -q realesrgan-x4plus && echo "  ✓ 模型就位"

# 2. waifu2x ncnn-vulkan (macOS) — 动漫超分
dl_unzip "https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20250915/waifu2x-ncnn-vulkan-20250915-macos.zip" .

# 3. RIFE ncnn-vulkan (macOS) — 视频补帧 (含多模型)
dl_unzip "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-macos.zip" .

echo ""
echo "✅ 引擎就绪:"
ls -d */
echo ""
echo "下一步: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
