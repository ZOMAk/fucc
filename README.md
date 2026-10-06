# FUCC — Fix You, Claude Code

**0.0.5**

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
| VS Code 中的 Claude Code 插件 | 本机默认用户配置中的 Claude 扩展环境变量 | 重启 Claude 会话；必要时完全退出 VS Code 后重开 |
| 本机 Claude 桌面客户端 | 已识别的原生桌面入口所启动进程的环境变量 | 完全退出后从该入口启动；客户端是否采纳参数需实际核实 |

已有进程不会自动更新环境，需要重启相应应用。`--user-only` 仅更新用户配置，系统时区和浏览器策略保持原状。

0.0.5 优先使用原生配置，不再向 `environment.d` 写入桌面会话环境，也不再包装浏览器和 VS Code 的桌面入口。原生浏览器采用系统时区；已继承固定 `TZ` 的进程仍可能覆盖它。Claude Desktop 按需保留启动脚本。

从旧版升级后再次运行 `fucc on`，会还原旧会话文件及浏览器、VS Code 桌面覆盖，保留首次备份供 `off` 恢复；未知外部修改仍会阻止覆盖。

需要管理员权限的变更会先完成 sudo 认证，再开始写配置；认证时取消不会进入半关闭状态。此前中断的关闭可以再次运行 `fucc off` 继续，或执行 `fucc recover`；未知外部修改仍会阻止恢复。

Claude Desktop 的自动生成入口在 FUCC 开启期间作为自定义入口保留，关闭后还原原文件。入口已经与开启前备份完全一致时，`off` 可继续还原；其他未知修改仍会提示冲突。

## 致谢

- [check-cc](https://github.com/yacuo/check-cc)：提供环境检测思路参考。
- [cc-fix](https://github.com/gongyijie85/cc-fix)：提供开源实现与可恢复配置管理思路。
- [ChatGPT](https://chatgpt.com/)：协助方案讨论、Ubuntu 适配、Python 重构与测试。
