# FUCC — Fix You, Claude Code

**0.0.3**

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

## 开启后的适用范围

运行 `fucc on --region us` 后，覆盖以下本机入口：

| Claude 使用入口 | FUCC 管理的内容 | 生效方式 |
|---|---|---|
| Chrome / Chromium 中的 Claude 网页 | 系统时区、浏览器首选语言、WebRTC 策略 | 原生安装；完全退出后重开，在 `chrome://policy` 核实策略 |
| Firefox 中的 Claude 网页 | 系统时区、首选语言、WebRTC 地址暴露限制 | 原生安装或已连接 `etc-firefox` 接口的 Snap；完全退出后重开，在 `about:policies` 的 Active / Errors 核实 |
| Edge 中的 Claude 网页 | 系统时区、浏览器首选语言；未管理 WebRTC 策略 | 原生安装；完全退出后重开，在 `edge://policy` 核实策略 |
| Bash 终端中的 Claude Code | `TZ`、`LANG`、`LC_ALL` 及当前用户的 Claude 设置 | 新开 Bash 或加载 `env.sh`，再重启 Claude Code |
| VS Code 中的 Claude Code 插件 | 本机默认用户配置中的环境变量 | 完全退出 VS Code 后，从桌面入口重新启动 |
| 本机 Claude 桌面客户端 | 已识别的原生桌面入口所启动进程的环境变量 | 完全退出后从该入口启动；客户端是否采纳参数需实际核实 |

已有进程不会自动更新环境；完整桌面会话环境更新需要注销后重新登录。`--user-only` 仅更新用户配置，系统时区和浏览器策略保持原状。

Firefox Snap 通过机器策略适配，不改沙箱启动入口。其他 Snap / Flatpak 入口、VS Code 独立配置档（profiles）、Insiders、自定义 `user-data-dir`，以及 SSH、容器、WSL 等远程环境暂未覆盖。项目级或组织管理设置也可能覆盖用户设置。具体覆盖情况可用 `fucc status` 查看。

0.0.2 升级后重跑 `fucc on --region us` 即可接入 Firefox，原有还原备份会保留。接入后请继续使用 0.0.3 管理。WebRTC 设置不能保证透明代理下所有流量经过代理；Firefox 自身的抗指纹保护也可能仍向网页报告 UTC 时区。

## 致谢

- [check-cc](https://github.com/yacuo/check-cc)：提供环境检测思路参考。
- [cc-fix](https://github.com/gongyijie85/cc-fix)：提供开源实现与可恢复配置管理思路。
- [ChatGPT](https://chatgpt.com/)：协助方案讨论、Ubuntu 适配、Python 重构与测试。
