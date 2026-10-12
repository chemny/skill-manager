## 1.3.0 发布准备（2026-10-12）

- 删除、卸载、永久删除、清空回收站及设置移除统一使用居中确认弹框；取消、关闭及 Esc 不发送删除请求，批量操作确认一次。
- 备份中的删除进入回收站，回收站中的永久删除清除内容；列表和详情中的按钮文案保持一致。
- 列表直接展示筛选条件，排序使用表头；补齐 Agent 图标及来源声明。
- 更新检查成功结果缓存 15 分钟，失败请求按来源限制重试；清单缓存与备份缓存分开管理。
- 中英文 README 改为当前产品说明，使用正确仓库地址及隔离示例数据截图，明确 Windows 未真机测试的边界。

## 本地开发：共享根目录卸载入口（2026-10-09）

- Agent 的 Skills 根目录为共享链接时，卸载入口可选择同一实际 Skill 的已登记真实目录，明确展示全部已知 Agent 影响，不再无说明置灰。
- 共享根别名不直接写入；真实目录必须显式选择，保护、引用范围和指纹校验保持不变。取消不生成操作，选定后进入回收站，不改根目录链接结构。
- 安装位置详情的卸载保留所选入口范围，单个链接仍只移除入口，不误选共享文件。
- 卸载或恢复后刷新按磁盘实际情况重新判断单项链接健康状态，避免残留链接把已卸载 Skill 继续显示在全部列表。

## 本地开发：修复侧栏添加 Agent 入口（2026-10-09）

- 加号立即打开添加面板，读取目录期间显示加载反馈，读取失败可原地重试；关闭面板后不会被迟到响应重新打开。
- 未添加预设可按名称选择，系统自动确定默认目录；已添加项不可重复添加。预设全部添加时展示现有 Agent 和管理入口，不再仅提示后返回。

## 本地开发：同 Skill 归组与目录卸载（2026-10-09）

- 保留原列表结构，同一可靠来源的 Skill 合并为一行，Agent 仅合并图标；独立目录及数据库入口不变。未知来源、来源冲突、不同仓库或分支不按名称强行合并。
- 更新、查询、备份和标签展开为实际安装位置；全量检查继续覆盖全部可检测目录。归组更新仅处理有更新的位置，逐位置备份，部分失败可明确定位及重试。
- 单 Agent 直接卸载；多个 Agent 只弹一次目录选择，说明共享影响或仅移除链接，选定后进入回收站。卸载前及暂存后复核引用范围，关联改变时停止，不产生待确认事项。
- 移除网页查找重复、同步副本及共享整理入口，保留兼容 CLI/API。

## 本地开发：移除待确认与重复审批（2026-10-09）

- 移除网页待确认入口、列表、继续确认及预览确认面板；卸载、更新、安装、备份操作、批量维护和设置命令直接执行。
- 仅保留必要的目录、恢复覆盖方式及安装内容选择。卸载进入回收站，覆盖前自动备份，后台继续校验内容、保护限制和并发资源冲突。
- 内部暂存随任务执行或失败自动清理，不重放旧待确认操作；CLI 显式预览与 apply 接口保留兼容。
- 安装或恢复后重新读取完整清单；安装识别期间切换页面或修改来源、位置会丢弃旧识别结果。

## 本地开发：备份列表直接展示清理结论（2026-10-09）

- 删除版本列的当前版本及未核实小字，仅保留备份版本。
- 去掉“核实建议”按钮，列表自动分析当前页并直接显示建议删除、建议保留或暂不建议删除及简短理由。
- 分析走只读本地接口，每批最多八项、前端最多两批并发，不锁定导航、搜索和翻页；缺少来源或基线时不做无用的全文件比较，不请求云端、不自动删除。

## 本地开发：保守版本判断与备份清理建议（2026-10-09）

- 本地领先、独有修改、双方分叉及版本或基线不明时，不再直接提示覆盖更新；预览接口同步要求人工核对。
- 更新检查使用磁盘实际版本，判断规则升级使旧的六小时缓存失效。
- 备份列表增加版本与清理建议，详情直接展示目录、用途、当前版本及读取记录。建议按需核实，不自动删除。
- 只有当前安装及来源已核实、无独有修改且另有校验通过的更近回退副本或完整重复副本，才建议清理；不明项及防误删项保留。

## 本地开发：全量更新检查与六小时缓存（2026-10-09）

- 顶部检查更新覆盖所有已纳入管理的 Skills，不受筛选或分页影响；勾选检查仍只针对所选项目。
- SQLite 保存逐项检查缓存及检查记录，成功结果滚动有效六小时；本地指纹、来源、分支、版本或基线变化立即失效，失败只退避一分钟。
- 强制重新检查可绕过缓存；相同运行中任务复用，共享目录去重，同一 GitHub 仓库复用远端获取。一个来源限流不阻断其他来源。
- 有可靠来源的插件项可只读对比；没有来源或来源冲突会记录跳过原因，不放开插件更新及卸载权限。

## 本地开发：Agent 管理与统一目录入口（2026-10-09）

- 去掉各 Agent 的启用开关和重复添加目录入口，添加 Agent 默认参与扫描。
- 设置页与列表页复用同一添加目录流程，可选择已添加 Agent 或不关联 Agent；只登记位置，不移动文件。
- 移除 Agent 需确认，仅撤销管理配置和列表关联；保留文件、历史记录及其他 Agent 的共享关联，之后可重新添加。

## 本地开发：分页按钮锁定修复（2026-10-09）

- 分页按钮不再进入通用异步按钮锁，防止点击事件冒泡后被永久置灰；禁用状态仅由当前页与总页数决定。
- 覆盖 Skills、更新、备份列表的连续正向和反向翻页，以及第一页、最后一页边界。

## 本地开发：备份操作与两行列表（2026-10-09）

- 备份页顶部操作精简为备份、扫描、导入，移除列表与详情中的备份导出入口。
- Skills 与更新列表统一名称字号，分类标签与名称同行，简介单行省略；Agent 标签最多显示两行。

## 本地开发：列表内进度与表头排序（2026-10-09）

- 刷新、查询按发起视图及筛选范围运行，分页不缩小任务范围；切换 Agent 不串显示任务，也不禁用其他视图。
- 移除顶部后台任务条；刷新在表格中间显示转圈与大字，查询显示大字、进度条和已查询项数。
- 名称、空间、安装时间、Agent、最近使用和更新状态支持表头双向排序；先排序后分页，缺失时间始终置后。

## 本地开发：精简更新状态（2026-10-09）

- 更新结果统一为待查询、已是最新、有更新、查询失败；来源问题和无法可靠判断的情况显示横线，具体原因放详情。
- 有更新时直接显示立即更新，复用变更确认、备份与校验流程；实际更新进度显示在更新按钮中。
- 更新成功后保存已校验的最新状态；失败保留更新提示。共享引用仅在来源一致时复用更新结果。

## 本地开发：精简详情、设置与自动处理

- Skill 详情平铺简介、Agent、分类标签、时间、更新状态和位置，删除重复来源表单、简介编辑及技术栏目。
- 原始简介只读，历史手动简介保留数据但不再覆盖展示；分类标签仍为管理器自己的数据。
- 设置收敛到扫描目录、自动处理和问题排查；来源处理归入更新，目录添加复用同一流程。
- 扫描自动增量获取使用日志，失败保留扫描结果；缺失来源优先自动查找，候选须确认。
- 副本同步归入重复整理，增加明确的来源与目标选择；打开文件夹限定已登记 Skill。

## 本地开发：统一信息层级

- 扫描位置按实际目录合并，以表格展示 Agent、扫描状态；完整路径及手动设置移至右侧详情。
- 来源设置和使用记录优先展示当前情况与主要操作，高级字段按需展开。
- 副本先展示整理结论，备份先展示时间、范围和恢复状态；整机备份内容支持搜索。
- 操作预览直接显示影响范围与恢复风险，操作历史的技术数据折叠；存储页提供对应管理入口。

## 本地开发：统一历史备份列表

- 历史内容直接并入备份/回收站，不再提供待核对区域或要求提前收录。
- 展示恢复状态，只有恢复位置不明确时再选择位置；浏览不复制文件，历史时间未知显示横杠。
- 历史恢复、保留与清理复用核心指纹校验和预览确认。

## 本地开发：备份与回收站

- 明确管理器自动保护范围，保存原因自动生成；备份/回收站分表，支持搜索、排序、保留与批量清理。
- 增加历史目录备份发现、确认原位置后复制校验收录；压缩包只展示，保留历史原文件。
- 恢复支持其他已配置位置，保留标记阻止清理，批量失败准确报告部分完成。

# Changelog

## 未发布：安装信息、使用记录与分类

- 恢复安装器锁文件来源、子目录和安装时间；目录创建时间可作标明依据的估算。
- 独立导入/回查日志，读取与明确调用证据分开计数，支持每日统计、覆盖信息和额外日志目录。
- 固定五个主分类、八个标签，单项/批量编辑及筛选，管理数据不写入 Skill 文件。
- 本地文件和 GitHub 搜索只提供候选，支持仓库内 SKILL.md 内容核对；批量检查复用请求和下载，遇限流停止本轮。


- 独立更新页面：保存结果、全范围检查、失败重试、后台进度、内容变化提示及历史恢复入口；有效缓存打开页面不自动核对磁盘。

All notable changes to Agent Skill Manager are documented here.

## 未发布：存放位置与共享关系

- 后台任务期间保持导航和浏览可用，只读页面使用独立只读数据库连接；写操作仍串行。检查完成不自动弹出结果对话框，提供主动查看入口。新增后台写事务期间读取与不产生状态修改的隔离测试。

- 按反馈简化列表：未知安装时间用横杠，使用频次改最近读取时间分档，无下拉；Agent 仅保留图标，更新状态四档，列宽重新平衡。恢复顶部刷新与扫描的独立入口。

- 主列表调整为名称、大小、安装时间、Agent、使用频次、更新状态和直接操作；取消更多按钮，窄窗口操作列固定。
- 新增可信安装日期与首次登记区分；保留扫描/更新后的日期，恢复成功安装记录。
- 新增本机今天、近 7/30/90 天读取频次，真实目录证据去重；无记录不判定闲置。新增 3 项隔离测试，并补安装与中断恢复日期断言。

- Agent 分类移除“公共目录”，将目录位置与真实 Agent 分开；跨 Agent 共享按实际目标和不同 Agent 判断，同名独立副本不算共享。
- 补充 Codex `.agents/skills` 约定、OpenClaw 默认状态约定、Hermes 外部目录配置及运行环境目录覆盖。保留原扫描身份、历史与备份，不迁移技能文件。
- 网页增加存放位置筛选；安装按 Agent 筛选实际位置，预览列出关联 Agent；设置按实际位置合并链接入口。

## 1.3.0 - Local app-style management build (2026-10-08)

- Replaced the sprawling maintenance page with a fixed sidebar, compact physical-target table, filters, selection, right inspector, status bar and dedicated management views. Long explanations and technical data are collapsed into help/details; previews and results use dialogs.
- Integrated installation and editable-root settings into `/manage`; `/legacy` remains available for compatibility. New installation identifies/stages candidates, produces a persisted preview and uses the common verified apply/history/recovery path without a second download or existing-target overwrite.
- Added selection expiry/discard, staged-content validation, observe-only/alias root restrictions and safe rollback of failed installs. Late targets, including empty directories and externally created content during staging, are preserved.
- Added seven isolated install tests, bringing the suite to 38; verified search, pending previews, recoverable deletion, backup verification, restoration and installation through the isolated Web UI. Real library verification is read-only; no desktop framework, dependency installation, automatic cleanup or publication.

## 1.2.2 - Local sources, organization and loading build (2026-10-08)

- Added source coverage by physical Skill target, source evidence and explicitly reviewed same-target/name-only local source candidates.
- Distinguished shared references from independent same-name/source copy hints; conflicting sources and protected/plugin directories are excluded from unsafe suggestions.
- Added top 20 unique-space review targets and read-only dependency/cache/output/version-record breakdown without following symlinks or deleting files.
- Added timestamped SQLite inventory snapshots, mutation invalidation, cached reads during background work and reload-first rendering; live plans still revalidate filesystem state.
- Added 31 isolated behavioral tests for persistence, corrupt snapshots, concurrent invalidation, copy grouping, source candidates, symlink boundaries and stale write rejection. No dependencies, remote checks, real Skill cleanup or client work included.

## 1.2.1 - Local web management build (2026-10-08)

- Made `/manage` the homepage with search, Agent/update/shared filters, allocation sorting, pagination and click-to-select targets and synchronization sources.
- Replaced raw-JSON-only flows with readable change previews, affected references, explicit confirmation, results, backup actions, history and persisted-plan selection.
- Added single-task HTTP jobs, progress for batch checks, same-target deduplication and page-reload reconnect. Service shutdown waits until tasks finish; restart never retries writes automatically.
- Added lightweight backup summaries and explicit current verification, persisted last-scan reporting and invalidation of cached checks after managed content changes.
- Preserved the common CLI/HTTP maintenance core and observe-only plugin boundaries; no model dependency, desktop framework or automatic cleanup was added.

## 1.2.0 - Local maintenance build (2026-10-07)

- Added a common standard-library maintenance core, JSON inventory and reviewed plans shared by Skill/CLI and the optional web page.
- Added Claude skills/project roots, broken/shared reference handling, name ambiguity rejection and identity retention for managed moves.
- Added pinned GitHub update staging, conflict protection, extra-file preservation, checksummed backups, restore, copy synchronization, protection, recoverable removal and separate permanent purge.
- Added operation history, interrupted-operation recovery, abandoned-plan cleanup, incremental usage import and conservative usage/space reporting.
- Added verified local service start/status/stop and loopback Host/Origin/token checks. Disabled legacy unchecked group overwrite and install overwrite.
- Added 18 isolated behavioral tests. No client, key vault, automatic cleanup, GitHub push or publication is included.

## Unreleased

- Changed activate/deactivate from registry-only status edits to real filesystem moves using matching `.disabled` folders.
- Added rescan verification after delete and update operations so the dashboard reflects actual filesystem state.
- Added Smart Scan background jobs so long scans continue after the modal is closed and interrupted scans can be detected.
- Added Smart Scan scope selection for full scan, Agent platform, or grade-based priority.
- Added GitCode support for direct installs and GitCode GitHub mirror fallback when GitHub checks/downloads fail.
- Added update channel management with per-channel search/check/download/update statistics and score-based channel ordering.

## 1.1.0 - 2026-06-02

- Added a dashboard install flow for installing skills from GitHub, GitLab, Gitee, skills.sh, or direct zip links.
- Added install preview so repositories with multiple `SKILL.md` files can be reviewed before installation.
- Added backup-and-replace support when installing over an existing local skill folder.
- Added SkillsMP as a remote discovery fallback before `find-skills`.
- Updated remote checks so one failed source falls through to the next source before local version unification.

## 1.0.0 - 2026-05-31

Initial public release.

- Cross-platform local skill inventory for Codex, Claude Code, OpenClaw, Hermes, and the public skills directory.
- Local HTML management console with Chinese/English UI.
- Skill grouping by name with per-platform tags.
- Status, health score, source, version, grade, and 30-day usage display.
- Real 30-day usage import from local session logs where available.
- Source management for configurable skill roots.
- Smart upgrade checks with 24-hour local cache.
- GitHub/Vercel-aware update checks and local version unification flow.
- Builtin/plugin skills are treated as observe-only by default.
- Delete flow includes confirmation and local backup.
- Snapshot report generation in Markdown and HTML.
- CLI wrapper for Unix-like shells and PowerShell compatibility wrapper.

## Release Notes

- Requires Python 3.10+.
- Uses only the Python standard library.
- Runtime data is stored under `~/.agent-skill-manager/` by default.


## 未发布：管理界面统一整改

- 删除工作区装饰、独立帮助和 Skill 字母头像；Agent 用本地图标，空间使用十进制 KB/MB/GB。
- 顶部合并为重新扫描，高级扫描在设置；底栏显示实际扫描时间。
- 列表用途短中文、原文保留与单独编辑；修正 YAML `>-` / `|-` 描述解析。
- 来源与更新操作按条件启用，备份/回收站按名称、用途和原位置展示；恢复冲突明确确认。
- HTTP 与任务失败带本地脱敏错误编号，服务重启、离线和繁忙状态提供恢复说明。

## 未发布：重复副本整理

- 移除“空间与副本”导航，在已安装工具栏增加“查找重复”。
- 后台按实际目录去重、逐组比对同名独立副本；展示 Agent、安装位置、大小与文件差异。
- 完全一致且来源相同的可编辑副本支持选择保留位置及整理预览，先校验备份再改为引用，支持恢复与失败回滚。
- 插件、受保护目录、内容差异和嵌套链接不提供自动整理；Windows 第一版支持比对，不提供自动链接整理。

## 未发布：导航、设置与待确认流程

- 侧栏精简为已安装、更新、安装 Skill、备份与回收站、设置；设置分扫描位置、更新来源、存储管理、问题排查。
- 扫描位置按实际目录合并，缺失目录折叠；移除扫描入口仅修改扫描配置。
- 顶部待确认只展示未确认预览和安装选择，支持稍后处理、继续选择与取消，过期预览明确标记；失败/中断进入问题排查。
- 备份和回收站分类展示，操作记录统一列表、扫描记录可查询；顶部按钮随页面调整。
- 修复任务重新连接时使用未定义变量的问题，保留后台任务期间的浏览能力。

- 备份页面从历史命名中提取保存时间并标注推测；详情改为右侧面板，按需只读比较当前文件并展示版本、差异和防止误删开关。

## Local library and portable backup workflow — 2026-10-08

- Split navigation into 全部 / 更新 / 安装 / 备份 / 回收站 / 设置; backups use one list.
- Add in-place directory adoption with discovery preview; keep full update checks in 更新.
- Support local folder / ZIP installation with metadata and deployment checks.
- Create, export, import and selectively restore checksummed portable snapshots; deduplicate real directories and retain Agent references, source/version and classification metadata.
- Restore to explicitly selected configured roots, with conflict preview, protection checks and automatic pre-overwrite backups. Imported source records remain in the package and do not authorize remote updates automatically.
- Add batch restore and an unfiltered clear-bin preview which skips retained entries and requires irreversible-action confirmation.
- Cloud upload and automatic recreation of shared Agent links remain outside this local first version.

## 批量操作与列表一致性 — 2026-10-08

- 全部列表补齐批量备份、更新、卸载；更新列表支持多选检查与更新。
- 批量操作先预览可执行项目与跳过原因，执行前统一复核文件、保护状态与 Agent 引用；失败停止并报告已完成项目。取消预览统一清理暂存。
- 备份混合选择时可恢复可恢复项、删除未保留项，明确提示跳过内容。
- 全部、更新、备份、回收站统一统计字号、搜索筛选、选择操作栏、表格边距与分页。

## 列表交互收尾 — 2026-10-09

- 多选切换原工具栏为批量操作栏，固定表格位置；各页仅保留相关操作，翻页显示本页选择数，筛选和切换菜单取消选择。
- 更新页移除大数字统计，顶部保留最近检查时间和详情入口，失败项使用一行提醒；详细范围、进度和结果分布收进右侧面板。
- 单项、批量预览、恢复设置和结果详情统一使用非模态右侧面板，位置和文件变化默认折叠。普通恢复在同一面板内完成两步，覆盖和永久删除保留明确确认。
- 关闭/取消预览释放暂存，只有明确点击“稍后处理”才保留单项预览。后台进度使用原底部状态条，不挤动列表。
- 回收站隐藏不适用的备份类型筛选、历史备份入口；备份概要保留项数与空间，防止误删状态留在筛选和条目中。

## 左下角批量操作 — 2026-10-09

- 全部、更新、备份和回收站的批量操作改为内容区左下角紧凑悬浮区，搜索筛选保持可见。
- 固定预留操作区高度，选中或取消不会挤动表格，也不会遮挡最后一行或分页。
- 移除取消选择按钮，支持取消勾选、表头勾选及 Esc 清除当前列表选择；面板打开时 Esc 优先关闭面板。

## 批量按钮并入分页行 — 2026-10-09

- 批量按钮与底部左侧数量同排，右侧继续显示分页。
- 移除独立悬浮菜单、边框阴影及底部预留层，恢复表格可用高度；四个列表页一致调整。

## 精简列表底栏 — 2026-10-09
- 删除底栏重复总数与选择数量，页码移至右侧翻页按钮旁；全部列表左侧统一为设置标签、批量备份、批量更新、批量卸载。
- 底部操作按钮常驻显示；未选择或不适用时置灰，选择后按可执行条件启用。

## 管理界面信息精简 — 2026-10-09
- 移除更新列表重复详情入口、安装步骤栏、底部重复计数与常驻教程。
- 技术数据移入问题排查；备份时间依据与提交编号折叠，精简 Skill 详情路径和关联说明。
- 来源与副本整理保留具体操作，冲突选项按需出现；保留失败处理与覆盖、删除风险提示。

## 清理常驻解释与重复功能 — 2026-10-09
- 删除更新失败提示条及其查看原因、重试入口；失败结果保留在筛选列表中。
- 删除备份、回收站刷新记录按钮，继续在进入页面和操作后刷新数据。
- 精简安装、设置、备份及空状态说明，移除存储管理重复导航和旧版工具入口。
- 使用记录从最近记录时间进入，空间构成和历史日志核对收进按需展开区域。

## 恢复安装指引 — 2026-10-09
- 恢复安装页右侧“如何安装”、来源格式和目标目录提示；窄屏在表单下展示指引。

## 安装及列表响应式布局 — 2026-10-09
- 安装表单和指引按可用空间分栏，卡片上下对齐；1440、1920 宽度保持并排。
- 列表统计与搜索筛选同排，搜索框限制为 240px；全部、更新、备份和回收站统一表格列宽及单元格间距。
- 通过 1440、1920 视口检查；较窄窗口按空间换行，底部批量按钮保持原位。

## 整合历史备份查找 — 2026-10-09
- 安装 Agent 选项简写为“全部”。
- 移除备份列表下方的其他备份位置区域；顶部查找历史备份统一提供常见位置和指定文件夹两种查找方式。

## 界面文案统一 — 2026-10-09
- 统一搜索、备份、筛选、排序、安装及设置名称，移除重复限定词。
- 外部备份按实际来源命名，简化日志与未完成操作术语；确认按钮标明具体操作。
- 保留批量、永久删除、清空回收站等范围和风险信息；无功能和数据结构变更。

## Skill 详情信息架构 — 2026-10-09
- 按用途、Agent、更新下一步、基本信息的顺序组织详情；仅展示当前可执行的更新入口。
- 路径、来源配置、其他操作及技术信息按需展开，备份和卸载固定在底部。
- 保留标签编辑、来源设置、使用记录和原有操作预览流程。
