# FUCC — Fix You, Claude Code

**0.0.2** 

## 安装使用

梯子节点选US的。命令行中文件路径跳转到该项目文件夹内，然后：

```bash
# 安装
conda create -n fucc python=3.12 pip
conda activate fucc
python -m pip install -r requirements.txt
python -m pip install -e .
# 检查安装
fucc --version
fucc status

# 默认根据美国出口选择时区
fucc on --region us

# 当前 Bash 加载；
. "$HOME/.config/fucc/env.sh"

# 还原首次开启前的配置
fucc off

# 恢复中断事务
fucc recover
```

## 致谢

- [check-cc](https://github.com/yacuo/check-cc)：提供环境检测思路参考。
- [cc-fix](https://github.com/gongyijie85/cc-fix)：提供开源实现与可恢复配置管理思路。
- [ChatGPT](https://chatgpt.com/)：协助方案讨论、Ubuntu 适配、Python 重构与测试。
