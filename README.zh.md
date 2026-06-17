# Agent Skill Manager

一个本地 skills 管理后台，用来管理 Codex、Claude Code、OpenClaw、Hermes 和公共 skills 目录里的 Agent skills。

中文 | [English](./README.md)

我做这个工具的原因很简单：本地 skills 越来越多之后，管理会变得很麻烦。你很难一眼看出来哪些还在用，哪些是重复的，哪些是系统内置的，哪些很久没用过，哪些可以更新，哪些删除前需要谨慎一点。

Agent Skill Manager 做的事情就是把这些信息拉到一个本地后台里。它会扫描本地目录，按名称合并不同平台里的同一个 skill，展示版本、来源、状态、健康分、使用次数，并提供更新、停用、删除和导出报告这些日常管理操作。

![Agent Skill Manager 管理后台](https://raw.githubusercontent.com/chemny/agent-skill-manager/main/assets/agent-skill-manager-dashboard.png)

## 可以用它做什么？

- 在一个表格里查看所有本地 skills，不用挨个翻隐藏目录。
- 查看每个 skill 属于哪个 Agent 平台：Codex、Claude Code、OpenClaw、Hermes 或公共目录。
- 找出重复 skills 和本地版本不一致的副本。
- 从本地 session logs 中导入真实的 30 天使用次数。
- 根据状态、metadata 和文件结构计算健康分。
- 添加和管理自己的 skills 来源目录。
- 从 GitHub、GitLab、Gitee、skills.sh 或 zip 链接安装新的 skills。
- 导出一份简单的管理报告。
- 检查可能的更新，并在本地版本不一致时统一到较新版本。
- 对 skills 做停用或删除，删除前会确认并备份。

## 适合谁？

这个工具主要适合本地 skills 已经比较多的人。

如果你符合下面几种情况，它会比较有用：

- 同时使用多个 Agent 平台；
- 经常安装、修改或自己写 skills；
- 想清理旧 skills，但不想靠猜；
- 想快速知道某个 skill 到底装在哪些目录；
- 需要一个中英文都能用的本地管理界面。

如果你只有很少几个 skills，直接用文件管理器可能就够了。

## 它是怎么工作的？

Agent Skill Manager 会维护一份自己的本地 registry。扫描时，它会读取常见的本地 skills 目录，找到像 skill 的文件夹，把同名副本合并，尽量导入使用记录，然后把结果写入 SQLite。

管理后台只是这份本地 registry 的可视化入口。每次重新扫描，安装副本、使用次数、健康分、来源标签和更新提示都会一起刷新。更新和删除会操作真实本地文件；停用会把可编辑 skill 移到对应的 `.disabled` 目录，启用会再移回原目录。

默认会扫描这些目录：

- 公共目录：`~/.agents/skills`
- Codex：`~/.codex/skills`、`~/.codex/plugins/cache`
- Claude Code：`~/.claude/commands`、`~/.claude/agents`、项目内 `.claude/commands`、项目内 `.claude/agents`
- OpenClaw：`~/.openclaw/skills`、`~/.openclaw/plugins`、`~/.openclaw/tools`、`~/.openclaw/workflows`
- Hermes：`~/.hermes`

第一次运行后，你可以在本地后台里调整这些来源目录。

## 怎么安装？

像安装普通本地 skill 一样直接安装：

```bash
git clone https://github.com/chemny/agent-skill-manager.git ~/.agents/skills/skill-manager
```

安装完成后，重新打开一个 Agent 会话，然后像使用其他 skills 一样直接使用。

## 快速开始

直接问 Agent：

```text
使用 skill-manager 扫描我的本地 skills，并打开管理后台。
```

如果你想在终端里直接运行：

```bash
python3 ~/.agents/skills/skill-manager/scripts/agent_skill_manager.py scan
python3 ~/.agents/skills/skill-manager/scripts/agent_skill_manager.py web --host 127.0.0.1 --port 8765 --open
```

第一条命令会扫描本地 skills，第二条命令会打开管理后台。

## 常用命令

```bash
python3 scripts/agent_skill_manager.py scan
python3 scripts/agent_skill_manager.py list
python3 scripts/agent_skill_manager.py search skill-manager
python3 scripts/agent_skill_manager.py show skill-manager
python3 scripts/agent_skill_manager.py usage --days 30
python3 scripts/agent_skill_manager.py health
python3 scripts/agent_skill_manager.py report
python3 scripts/agent_skill_manager.py web --open
```

类 Unix 系统可以用：

```bash
bin/asm scan
bin/asm web --open
```

Windows 可以用：

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\skill-manager.ps1" -Action Scan
powershell -ExecutionPolicy Bypass -File ".\scripts\skill-manager.ps1" -Action Web
```

## 管理后台

本地 HTML 后台是最主要的使用入口。它支持：

- 中英文切换；
- skills 扫描和刷新；
- 按等级、平台、状态、来源筛选；
- 只按名称搜索；
- 查看详情和使用记录；
- 管理自定义来源目录；
- 从 GitHub、GitLab、Gitee、skills.sh 或 zip 链接安装 skill；
- 带 24 小时缓存的智能扫描，可按全量、平台或等级范围运行；
- 本地版本统一；
- 通过 `.disabled` 目录实现真实启用和停用；
- 带确认和备份的删除；
- 导出报告。

## 更新来源顺序

点击更新或运行智能扫描时，它会按这个顺序找新版本：

1. 先读 24 小时内的本地缓存，避免反复请求远程服务。
2. 再查 skill 自己绑定的仓库来源：GitHub、GitLab、Gitee 或 skills.sh 指向的 GitHub 仓库。
3. 如果没有绑定来源，或者绑定来源暂时不可用，再用 SkillsMP 搜索。
4. SkillsMP 没有结果或请求失败时，继续用 `find-skills` / skills.sh 搜索。
5. 远程都找不到时，最后比较本地同名副本，提示是否统一到本地最高版本。

某个远程来源失败不会直接说明 skill 有问题。比如 GitHub 限流、SkillsMP 超时，都会继续尝试下一条来源。

启动后台：

```bash
python3 scripts/agent_skill_manager.py web --host 127.0.0.1 --port 8765 --open
```

## 运行时文件

运行时数据不会放进这个仓库。

- 数据库：`~/.agent-skill-manager/skills.db`
- 配置：`~/.agent-skill-manager/config.json`
- 报告：`~/.agent-skill-manager/reports/`
- 备份：`~/.agent-skill-manager/backups/`
- 日志：`~/.agent-skill-manager/logs/`

如果想临时使用另一套运行目录，可以设置 `ASM_HOME`：

```bash
ASM_HOME=/tmp/asm-test python3 scripts/agent_skill_manager.py list
```

## 兼容性

Agent Skill Manager 设计上兼容 Codex、Claude Code、OpenClaw、Hermes 和公共 skills 目录。

它主要依赖本地文件扫描、`SKILL.md` metadata、SQLite 和 Python 标准库。各平台路径只是默认值，不是硬性要求。

## 安全边界

这个工具默认比较保守：

- 内置 skills 和插件缓存 skills 默认只观察；
- 停用会把可编辑 skill 移出启用目录，放到对应的 `.disabled` 目录；
- 删除前会确认，并尽量先备份可编辑文件；
- 远程更新检查有 24 小时缓存；
- 找不到远程更新时，仍然可以比较本地副本并统一版本；
- 本地数据库、报告、备份、日志和使用记录不会提交到 Git。

## 更新这个 skill

如果你是用 Git 安装的：

```bash
cd ~/.agents/skills/skill-manager
git pull
```

更新后重新开一个 Agent 会话即可。

## 仓库结构

```text
skill-manager/
├── README.md
├── README.zh.md
├── CHANGELOG.md
├── LICENSE
├── SKILL.md
├── assets/
│   └── agent-skill-manager-dashboard.png
├── bin/
│   └── asm
└── scripts/
    ├── agent_skill_manager.py
    ├── release_check.sh
    └── skill-manager.ps1
```

## 发布检查

```bash
./scripts/release_check.sh
```

它会检查 Python 语法、内嵌 JavaScript、临时运行目录创建，以及仓库里有没有残留 Python 缓存文件。

## 许可证

MIT
