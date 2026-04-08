#!/bin/bash

# 当前目录下虚拟环境名称
ENV_NAME="myenv"

# 检查虚拟环境是否存在
if [ ! -d "$ENV_NAME" ]; then
    echo "⚡ 虚拟环境 $ENV_NAME 不存在，正在创建..."
    python3 -m venv "$ENV_NAME"

    if [ $? -ne 0 ]; then
        echo "❌ 创建虚拟环境失败，请检查 Python 是否安装"
        exit 1
    fi
    echo "✅ 虚拟环境创建完成"
fi

# 激活虚拟环境
echo "⚡ 激活虚拟环境 $ENV_NAME"
source "$ENV_NAME/bin/activate"

# 安装依赖
if [ -f "requirements.txt" ]; then
    echo "⚡ 安装依赖 requirements.txt"
    pip install --upgrade pip
    pip install -r requirements.txt

    if [ $? -ne 0 ]; then
        echo "❌ 依赖安装失败，请检查 requirements.txt"
        exit 1
    fi
    echo "✅ 依赖安装完成"
else
    echo "⚠️ 当前目录没有 requirements.txt，跳过安装依赖"
fi

# 运行 bot.py
echo "⚡ 运行 bot.py"
python3 bot.py

# 脚本结束，虚拟环境仍然激活