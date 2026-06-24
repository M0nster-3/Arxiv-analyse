#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -d ".venv" ]; then
    echo "虚拟环境已存在: .venv/"
    echo "激活: source .venv/bin/activate"
    exit 0
fi

echo "创建虚拟环境..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "==============================="
echo "  初始化完成"
echo "==============================="
echo "  source .venv/bin/activate"
echo "  python main.py init"
