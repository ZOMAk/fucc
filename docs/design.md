# FUCC 0.0.2 实现说明

| 模块 | 职责 |
|---|---|
| cli / output | argparse、稳定 JSON/退出码、ASCII 与 CJK 宽度 |
| runtime | 统一用户事务，嵌套 Bash/机器恢复 |
| bash | 兼容 v1、动态时区 v2 状态、不可变 daily 快照、保护/恢复日志及加载块 |
| apps / jsonc | 会话、launcher、设置及桌面覆盖；JSONC token 精确编辑 |
| files | 安全快照、期望值比较、原子替换与 fsync |
| machine / helper | 固定机器目录/命令、UID 授权、策略冲突与提权 |
| state | JCS 校验封装、前代、dirfd/nofollow、Linux 进程锁 |
| transaction | v1/v2 资源绑定、原始值、两代写前日志与补偿 |
| detection / network | 只读检测、ZoneInfo、IP 多源与凭据隐藏 |
| regions / paths / errors | 地区数据、美国 IANA 时区选择、HOME/XDG 和稳定错误 |

产品名、命令与状态路径仍为 `fucc`，源码目录为 `fucc2`，Conda 环境为 `fucc`，版本保持 `0.0.2`。兼容 0.0.1 的 v1 状态；美国非默认时区增加 v2 状态保护。checked envelope 的 SHA-256 输入遵循原 ECMAScript 数字格式和 UTF-16 属性排序，以 JCS 实现；拒绝不安全数字、非有限数、孤立 surrogate 和非 JSON 值。

统一 coordinator 为资源绑定 id/identity，首次开启记录 original，切换仅改 desired。Bash baseline 为 daily，统一本机 off 会关闭以前单独开启的 Bash。桌面从保存绑定恢复集合，卸载不会丢原始快照。

写资源前将同一 journal 发布到当前与前代。apply 失败逆序补偿到 before；restore 中断正序继续到 original。恢复只接受已知 before/desired，不能覆盖未知外部修改。丢失已初始化状态不会重采基线，未知格式拒绝写入。

机器 helper 固定操作 `/var/lib/fucc`、timedatectl 和策略目录，不从环境读取替代目标。用户只读机器公开摘要；原始机器数据放私有目录。全局 action 锁串行化不同用户，UID 和 owner 资源验证恢复身份。

提权用绝对 sudo、当前 Python、`-I` 和包内绝对 helper。helper 自行定位已安装包，屏蔽调用环境 PYTHONPATH/PYTHONINSPECT 等注入。用户文件不通过 sudo 写入。

配置对齐、机器时区、浏览器策略接受和应用内部环境是不同事实。status 验证前两者；后两者要求重启与应用内部核实。检测不请求 Claude 账号或访问接口，评分不是可用性判定。

## 美国时区选择

`on --region us` 在开启事务前并行查询 IP 服务。服务提供的州名、城市和 UTC 偏移只用于展示；配置采用校验通过的美国 IANA 时区标识，允许已知别名转换到规范标识，并通过 `ZoneInfo` 验证可加载性。

选择顺序为显式 `--timezone`、可信美国 IP 时区、已保存的美国目标、纽约默认值。多来源须一致且全部确认美国，已有时区值须为同一有效美国时区；单来源成功也可选择。查询失败、时区缺失或无效、来源不一致、非美国出口均保留已保存的美国目标，否则回退 `America/New_York`。回退原因公开输出。

地区仍为 `us`，`LANG`、`LC_ALL` 保持 `en_US.UTF-8`，浏览器语言策略不随美国城市改变。所选时区进入 Bash、原生设置、会话环境、桌面启动器以及完整模式下的机器配置；`--user-only` 保留机器配置。夏令时使用 IANA 规则，不保存固定 UTC 偏移。

`status/off/recover` 在状态读取或事务结束后查询 IP，展示即时观察，不重新选择目标。恢复依据保存的 journal 和 committed desired。`check/run` 优先沿用同地区已保存时区；没有保存目标时可为该次只读检测或子进程选择 IP 时区，不改变持久配置。`check/run --timezone` 可覆盖当次目标。

JSON 顶层 `network` 与配置 `status` 分开，增加 `timezone` 和逐来源 `timezoneSources`；`on` 的 `timezoneSelection` 给出目标、来源与原因。表格增加 `IP timezone`、`Timezone selection`。IP、城市和整份情报不持久化，选用的目标时区随受管配置保存。联网失败触发保留或默认回退，不单独改变事务结果、配置健康或退出码。

## 动态时区状态与兼容

| 内容 | 动态状态格式 | 保护方式 |
|---|---|---|
| Bash 状态 | `fucc-state-v2`、`schemaVersion: 2` | 在动态配置写入前，两代均写入 v2；目标保存显式时区 |
| 外层用户事务 | `fucc-local-transaction-v2`、`version: 2` | 两代发布同一 v2 journal 后再写资源 |
| 机器私有事务 | `fucc-local-transaction-v2`、`version: 2` | 保存 timezone 资源的 desired 与最初 original |
| 机器公开摘要 | `fucc-machine-public-v2` | 保存非默认目标时区，读回与恢复使用私有已提交 desired |

旧 TypeScript 引擎仅识别 v1，不会把 v2 动态目标按 `us` 重建成纽约；v2 两代保护使其拒绝更新和恢复。Python 默认 v1 事务实例读到活动或中断的 v2 时保持 v2，未知版本拒绝写入。切换时区保留首次开启前的 original，失败逆序补偿到上一次已提交目标。

通用事务完全恢复到空状态、或初次 apply 失败并完整补偿后，可双代写回空的 v1。Bash 在关闭后仍保留 v2，保留与其原始快照及日志关联的保护；因此使用过动态时区后，整个项目不能假定与旧 TypeScript 版本互操作，应继续用本 Python 版本管理。历史 v1 默认配置仍可迁移、恢复；不删除备份或强制转换状态。

测试以旧模块验证 v1 跨语言格式与恢复、v2 拒绝旧版写入；fixture 验证 IP 时区选择、回退、夏令时、城市切换补偿、注释保留、外部修改、锁竞争和写入/fsync 故障。运行时无 Node 依赖。
