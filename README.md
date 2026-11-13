# Codex Context Provider

让本地 Codex 切换模型供应商后，继续打开原来的对话与上下文。

这个仓库同时提供可安装的 Codex skill 和可独立运行的 Python 命令行工具。它源于一次本地历史会话修复，处理供应商标识变更后出现的 `modelProviderMismatch`、`Model provider … not found` 等问题。通过同步 SQLite 索引和历史文件中的 provider 元数据，保留原会话 ID 和已有对话正文。

## 安装 skill

在 Codex 中使用 skill installer 安装本仓库根目录。

```text
$skill-installer Install the skill from https://github.com/lcq666666/codex-context-provider
```

也可以在 PowerShell 中手动安装到个人 skills 目录。

```powershell
git clone https://github.com/lcq666666/codex-context-provider.git "$env:USERPROFILE\.agents\skills\codex-context-provider"
```

macOS / Linux 可使用以下命令。

```sh
git clone https://github.com/lcq666666/codex-context-provider.git ~/.agents/skills/codex-context-provider
```

重新打开 Codex 后调用。

```text
$codex-context-provider 检查我切换供应商后打不开的历史对话，并准备迁移。
```

## 独立运行

需要 Python 3.11 或更新版本。下面的命令在仓库根目录执行。Windows 可以将 `python` 换成 `py -3.14`。

普通 `.jsonl` 使用 Python 标准库。`.jsonl.zst` 可直接使用 Python 3.14 的标准库，较早版本需要额外安装 `zstandard`。

```sh
python -m pip install zstandard
```

### 1. 预览

```sh
python scripts/provider_history.py --all
```

默认读取 `$CODEX_HOME`，未设置时使用 `~/.codex`。目标 provider 默认取 `config.toml` 的顶层 `model_provider`，缺省值为 `openai`。使用配置 profile、启动参数或其他覆盖时，请显式指定实际生效的 provider。

```sh
python scripts/provider_history.py --thread THREAD_ID --target shared
python scripts/provider_history.py --home /path/to/codex-home --all --target shared
```

输出包括扫描数量、需要变更的数量、目标 provider 和受影响会话 ID。预览会验证选中记录的历史文件，只读取文件和数据库。

### 2. 关闭写入进程后应用

先完全关闭使用该 Codex home 的桌面客户端、CLI 和 app-server，再在独立终端执行。`--offline` 表示执行者已确认写入进程停止。脚本的互斥锁只能协调本脚本的多个实例。应用前脚本还会检查是否仍有 Codex 写进程在跑，检测到就直接停下并列出进程，请关闭后再试；这项检查没有绕过选项。检测不到写进程时（例如系统没有 `ps` 或 `tasklist`），仍以 `--offline` 的确认为准。

```sh
python scripts/provider_history.py --all --target shared --apply --offline
```

`--all` 包含已归档的索引会话。存在多个 `state_*.sqlite` 时，通过 `--db /path/to/state_5.sqlite` 显式选定。

### 3. 检查与继续对话

重复预览，确认 `changes` 为 `0`，然后启动 Codex 并打开原对话。继续发送消息时，Codex 会通过当前配置的供应商处理该会话上下文。

### 4. 回滚或中断恢复

应用时会输出本地备份目录，其中包含 `manifest.json`、SQLite 一致性快照、历史文件原件和迁移后的暂存文件。

```sh
python scripts/provider_history.py --restore /path/to/backup/manifest.json --apply --offline
```

恢复会还原清单中记录的历史文件、provider 字段，以及迁移时修正过的索引路径（改回原值）。数据库中的其他会话按当前状态保留。中断后的恢复可重复执行。若迁移后历史文件已经产生新内容，恢复会停止并提示需要逐条检查，避免覆盖新对话。

进程被强制终止可能留下 `.provider-history.lock`。确认相应迁移进程已结束后，移除该锁文件，再使用清单恢复。`preparing` 状态表示备份尚未完成，尚未开始修改原文件。

## 持续切换时保持同一 provider ID

会话索引关联 provider ID。切换 API 服务时，维持一个稳定 ID，可减少反复迁移。下面展示一个自定义 provider 配置，按实际服务兼容情况填写模型、地址和认证环境变量。

```toml
model = "YOUR_MODEL"
model_provider = "shared"

[model_providers.shared]
name = "Shared provider"
base_url = "https://api.example.com/v1"
env_key = "SHARED_API_KEY"
wire_api = "responses"
```

在支持该配置的 Codex 版本中，切换到另一个兼容 API 时继续使用 `shared`，调整相应的连接配置。该工具本身只迁移历史元数据。供应商连接、认证、模型兼容性由 Codex 配置决定。使用内置 `openai` 时，依据当前官方文档配置其连接参数。

配置字段与 provider 行为可参考 [Codex 官方高级配置](https://developers.openai.com/codex/config-advanced)。安装与技能机制可参考 [Codex 官方技能说明](https://developers.openai.com/codex/skills)。

## 数据与兼容范围

- 同步 `threads.model_provider` 和第一条 `session_meta.payload.model_provider`。保持会话 ID、正文原始字节、未压缩历史文件总长度和正文起始偏移。
- 适用于含 `id`、`model_provider`、`rollout_path` 字段的本地 `threads` 表，以及 `.jsonl` 和 `.jsonl.zst` 历史文件。数据库结构会在运行时检查。
- 新 provider ID 超出原始元数据行的可用空间时，迁移会停止。使用较短的兼容 provider ID，可以保持既有历史偏移有效。
- 历史文件缺失、首行结构变化或会话 ID 不匹配时，需要先处理对应问题。索引仍指向 `.jsonl`、但只有同名 `.jsonl.zst` 存在时，预览和迁移会自动使用 `.zst` 文件，应用时把索引路径修正为 `.zst`，恢复时改回原值；两个文件都不存在时仍需先修路径。
- SQLite 事务与文件替换之间存在跨资源中断窗口。预写清单、备份和可重复恢复用于处理这个窗口。正常运行异常会尝试还原已替换的文件。
- 本地备份包含完整私密会话，请按本地隐私数据保存。脚本执行过程中只操作本地文件，无网络请求。
- 本工具针对本地会话元数据。云端会话、跨设备同步，以及不同供应商对上下文和工具调用格式的处理，需要各自系统支持。

## 开发验证

```sh
python -m unittest discover -s tests -v
```

测试使用临时的合成会话与数据库，覆盖预览、迁移、归档记录、压缩文件、幂等、回滚及部分失败恢复。
