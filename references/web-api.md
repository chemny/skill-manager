# 本地管理接口

实现：`maintenance.Manager` 负责文件、预览、备份和恢复；`web_jobs.Jobs` 只负责 Web 任务调度。CLI 直接调用同一核心。没有模型调用或新增第三方依赖。

服务只监听回环地址。`GET /api/service` 提供实例/PID；`POST /api/maintenance` 接收 JSON，需同源 Host/Origin 和 `X-ASM-Token`。令牌存在本机私有 `service.json`，不要写入日志、公开 URL 或提交到 Git。客户端启动服务后读令牌即可调用，不需要解析网页或聊天。

## 任务

开始：

```json
{"action":"job-start","request":{"action":"scan","import_logs":false}}
```

返回任务 `id`、`action`、`state`、`message`、`completed`、`total`。之后轮询：

```json
{"action":"job-status","id":"job-..."}
```

不传 id 返回 `active`，便于重连同服务中的活动任务。终态 `succeeded` 返回 `result`；`failed` 返回 `error`。任务失败的状态响应本身可以是 HTTP 200，调用方必须读取 `state`；不能把“请求成功”当“维护成功”。更新检查的单项结果也须读取 `status` 和批量 `failed` 数量。

任务只保留在本服务内存中，最多最近30个。服务重启或记录过期会报告找不到，不自动执行原请求。应重新读取 `history` 和清单核实事实，不重放旧暂存操作。按资源互斥调度任务，无关浏览可并行；执行中的任务会阻止主动停止服务。任务没有自动取消或自动重试。

## 请求动作

| action | 主要参数 | 结果 / 用途 |
|---|---|---|
| inventory | ids（可选）、lightweight（可选） | items、shared_targets、space、last_scan、insights、cache；lightweight=true 仅同步基础文件状态和数据库元数据，复用目录体积及上次更新检查，不遍历文件树；无缓存时首次建立完整快照 |
| inventory-cached | 无 | 上次完整快照；cache.available=false 时没有可用快照，后台任务运行时仍允许直接读取 |
| inspect-space | target | 第一层 entries 的分类、占用和 checked_at；不跟随软链接，不删除 |
| install-roots | 无 | 启用的可编辑实际 Skill 根；排除共享根别名、插件、commands/agents |
| install-identify | url（HTTPS）、target_root | 私有暂存中的候选、名称、版本、描述、路径、大小；返回 selection ID |
| preview | operation=install、id（selection ID）、remote_path（候选原路径） | 固定安装内容的持久预览；仅新增、不覆盖 |
| scan | project（可选）、import_logs | 逐目录扫描结果，默认不导入日志 |
| check | target（ID/完整路径） | source_unknown/latest/update_available/local_modified 等 |
| check-many | ids（明确选择的 ID 数组） | checks、skipped、failed；同一目标且来源相同只检查一次 |
| preview | operation、target；sync 另传 source | 固定内容和指纹的预览，不执行 |
| preview | operation=restore/purge、id（备份 ID） | 恢复或永久清理预览 |
| apply | id（预览 ID）、confirmed=true | 操作状态、记录 ID、对应备份；实际执行 |
| backup | target | 创建并校验备份 |
| backups | verify=true（可选） | 默认轻量索引，verified=null 表示本次未校验 |
| verify-backup | id | valid 表示本次校验结果 |
| history / plans | 无 | 持久历史 / 待处理预览 |
| get-plan | id | 完整预览和当前 state；仅 preview 可执行 |
| discard-plan | id | 放弃暂存，不清理技能 |
| recover | id（操作 ID）、confirmed=true | 检查或恢复中断的维护操作 |
| protect / unprotect | target | 开关保护状态 |
| source-bind | target、url、ref、path | 绑定确认来源，清空本条目基线和旧检查 |
| usage | target、days | 部分读取证据，无记录表示未知 |

`allow_local_changes=true` 只用于已审阅的 update/sync/restore 冲突，仍须生成预览再执行。保护项、内置项和共享根别名写入限制不会被这个参数绕过。常规动作也能直接 POST，但 Web UI 用任务方式处理，以免长操作阻塞页面。

## 安装约定

`install-identify` 会下载到私有 plans 暂存区，不写入技能根、不执行脚本或安装依赖。候选路径、内容指纹和根目录都重新校验；下载含链接需要人工审阅。网页选定候选原始 `remote_path` 后交给 `execute`，内部校验并执行；兼容调用方仍可显式预览及 `apply`。执行复用已暂存内容，不重新下载；目标出现、内容改变、根被改成别名等情况拒绝执行。首次成功安装登记确认来源和基线，记录到统一历史；失败或中断使用同一恢复入口。

`plans` 可能返回 `state=selection/action=install-selection`（已识别尚未预览），该项不能直接 apply；可 discard 后重新识别。识别和预览各自有效期 24 小时，兼容调用方过期后需要显式放弃；当前网页在执行结束、失败或取消选择时释放暂存。一次候选选择生成一个预览；要安装同一仓库另一个 Skill，重新识别。插件/共享根别名不作为安装位置。

## 清单字段

- `id` 是登记对象身份，`name` 不是唯一键。
- `path` 是所选入口，`real_path` 是实际目标，`reference_type` 说明目录/链接/共享根入口。
- `references` 保留登记入口 ID（含旧 `shared` 存储命名空间，兼容已有历史）。`known_agents` 只含真实 Agent；`shared=true` 要求至少两个不同 Agent 关联同一实际目录。单个 Agent 的多个入口不算跨 Agent 共享，`multiple_references` 单独说明多路径入口。关联不证明当前加载或全部消费者。
- `storage_roots`、`locations` 表示存放位置与目录依据。`locations` 中的 `shared` 是兼容的存储身份，不是 Agent；目录不存在时不会自动创建。默认约定目录结合 Agent 目录或可执行文件痕迹发现，配置可用 `discover_defaults=false` 关闭自动补充。环境变量只读取当前服务进程的运行环境，Windows 与 WSL 的清单不混用。
- `install-roots.roots[].agents` 说明该实际安装位置关联的 Agent；安装预览固定 `install_agents`，执行前再次核对。没有 Agent 关联的自定义存放位置不保证 Agent 会加载。
- `space` 是当前入口估算，`target_space` 是实际目标估算；汇总 `space.installed_unique` 去重。逐行相加可能重复。
- `update_check` 是上次检查和时间，不等于实时状态；管理器变更相关内容后清除缓存，外部编辑仍需要重新检查。
- `backup_summary` 是本路径登记的可用备份数量和最新条目，不保证当前完整性；恢复必须重新校验。
- `usage_status=unknown` 不能被解释为长期不用。

- `source_readiness` 汇总同一真实目录的来源情况；`check_supported` 不保证来源正确或网络可用。同目录的 ref/URL/子路径不一致标为 `source_conflict`。
- `source_candidates` 只提出本机来源线索，`same-target` 与 `name-only` 说明依据；不会写入来源。绑定必须显式调用 `source-bind`。
- `insights` 包含去重的来源覆盖、共享目标、独立同来源副本及空间核对目录。副本按名称及 provider/URL/子路径归组，ref 可不同，集合仓库的安装元数据可能缺子目录，未证明同一个远端 Skill 或内容相等；空间估算不等于可回收空间。
- `cache.generated_at` 是读取时间，`from_cache=true` 表示历史快照，`dirty=true` 表示生成后存在管理器内变更。外部编辑不被监视，即使 dirty=false 也不代表实时。缓存跨服务重启保存，任务队列不保存。
- 页面先 `inventory-cached` 显示列表。每项 `synced_at` 记录基础同步时间，15 分钟内切换页面不重复同步；过期时后台轻量同步当前范围。手动刷新绕过时间限制，但不重算全部文件指纹或目录体积。只有首次无缓存才建立完整清单，扫描及受影响项目的操作后刷新保留完整读取路径。
- 有数据时刷新仅在按钮内显示加载图标，不遮挡列表；同范围、同读取模式的并发刷新合并为一个任务。局部同步不延长其他条目的缓存时间。备份列表保留一小时缓存。
- 更新页仅保留“检查更新”，范围始终为全部已安装 Skills。成功比较结果缓存 15 分钟，命中不延长有效期；本地内容、来源或基线变化时失效。普通失败下次点击重试，服务端明确限流时遵守冷却时间。相同内容的重试失败保留此前已确认的比较结果，并单独记录失败尝试，不抹除“有更新”。
- `update_check`、体积和来源线索是上次完整检查记录；轻量同步不核验嵌套内容，不据此保证文件未修改。快照不能作为执行授权或绕过写操作前的重新校验。外部新增 Skills 通过扫描发现，添加目录统一在设置中进行。


### 界面简介和本地诊断

`set-description`（可用作后台任务）接收 `target` 与最长 80 字 `description`。空串恢复默认。仅写管理器展示配置，不修改技能源文件；清单新增 `display_description`、`description_basis`。缓存 schema 为 5。

`history` 同时返回最多 30 条脱敏 `errors`。失败 HTTP/任务包含 `error`、`message`、`code`、`error_id`；日志不可写时 HTTP 诊断还有 `log_unavailable`。不要自动重试失败的写操作。`backups` 新增 `name`、`reason`、`target_exists`，恢复预览仍在核心重新校验目标。

### Recovery catalog

Maintenance jobs share the CLI recovery core:

- `discover-backups`: optional absolute directory `path`; read files and persist candidates only.
- `retain-backup`: `id`, boolean `retained`; retained entries cannot be purged, including already prepared plans.
- `ignore-backup-candidate`: candidate `id`; preserves files.
- `import-backup`: candidate `id`, confirmed `original_path`, `type` (`backup` or `quarantine`), `confirmed: true`. Copies and verifies a directory candidate; never moves its historical source. Archives and nested symlinks are unsupported.
- `preview-purge-many`: 1–10000 backup `ids`; returns individual immutable cleanup plans and estimated allocated bytes. No deletion.
- `apply-purge-many`: cleanup plan `ids`, `confirmed: true`; rechecks retention and each plan, stops on failure and reports `partial` plus completed/remaining plans.

`backups` remains a read-only browse call, returning `candidates`, `retained`, historical provenance and current allocated size. `preview` with `operation: restore` accepts optional `restore_path` below an enabled Skills root; existing targets still require explicit `allow_local_changes` and are backed up before replacement. Recovery catalog discovery does not mean external Agent writes are automatically backed up. Cloud upload is not implemented; portable local export is documented below.

Historical discoveries now appear directly in `backups` with `external: true`, nullable `original_path`/`created_at`, and `restore_status` (`needs_target`, `unsupported`; native backups use `ready`). Discovery does not copy files. `preview` restore accepts historical IDs with a required `restore_path`; the source is verified and staged without importing a permanent duplicate. Cleanup of a historical ID removes its discovered source path after the usual preview/confirmation, then marks the catalog entry purged. Retention applies equally to native and historical entries.

`backup-detail`（`id`）为只读浏览动作，后台任务期间可用。返回保存时间及推测依据、明确的 Skill 版本、文件清单（最多 500 项）、校验结果和当前安装候选的逐文件差异。差异方向是用备份替换当前内容；同名候选不作为恢复路径。`backups` 仅返回轻量时间/版本信息，不在列表加载时遍历并哈希全部当前目录。

## 本地目录与可迁移备份（2026-10-08）

新增动作通过同一 `job-start` 队列执行；不调用模型或云端。导航为全部、更新、安装、备份、回收站、设置，备份使用一个列表。

| action | 参数 | 用途 |
|---|---|---|
| `directory-preview` | `path` | 发现指定目录中的 Skills，返回数量、条目和指纹 |
| `directory-add` | `path`, `fingerprint`, `confirmed:true` | 复核预览后就地登记并扫描，不复制或关联 Agent |
| `install-local-identify` | `path`, `target_root` | 暂存本地目录或 ZIP，网页后续自动执行；兼容调用方可显式预览及 apply |
| `create-snapshot` | `scope:all/agent/selected`, `agent` 或 `ids` | 创建去重的完整内容备份包 |
| `import-snapshot` | `path` | 校验并收录本工具导出的 ZIP，不执行恢复 |
| `export-backup` | `id` | 返回 `download_id`；使用令牌请求 `GET /api/backup-file?id=...` 下载 ZIP |
| `snapshot-detail` | `id` | 读取备份包的 Skills、版本、来源、标签和引用信息 |
| `preview-restore-many` | `entries:[{id,key?}]`, `target_root?`, `allow_overwrite` | 为每项生成恢复预览；备份包必须选择新位置，key 来自备份清单 |
| `apply-restore-many` | 预览 `ids`, `confirmed:true` | 复核全部目标后逐项恢复，失败停止并报告部分完成 |
| `preview-empty-trash` | 无 | 预览全部可用回收站项目，不受页面筛选影响，跳过保留标记 |

批量永久清理支持最多 10000 项，仍需预览及 `confirmed:true`。备份包采用 `skill-manager-snapshot-v1`：每个实际目录只存一份，文件 SHA-256 与权限纳入清单；拒绝越界路径、特殊文件、内部链接、遗漏文件、损坏内容，以及超过 2 GB 或 100000 个 ZIP 条目的包。导入后恢复到用户选择的已配置目录，覆盖前自动备份；不照搬旧设备路径，不自动重建 Agent 链接。分类标签恢复；来源和引用信息保存在包内，不自动授予远端更新权限。Skill 自身目录中的文件原样备份，因此其中如有密钥也会随包保存；不会额外读取 Agent 的全局配置或密钥存储。没有加密或云端上传。

`backups` 增加 `bundle`、`scope`、`entry_count`、`snapshot_at`；整体备份与单项/历史备份同列。`backup-detail` 对备份包返回 `snapshot`，普通备份的 `version_state` 以本机对应内容为参照，不代表远端最新：`same/older/newer/different/not_installed/unknown`。只有明确原安装位置和纯数字版本可判断新旧，同名历史候选不据此判定过时。

## Skills 批量维护

- `preview-library-many`：传入 `operation=delete|update` 和 `ids`（清单 ID 数组）；返回固定的 `plans` 与 `skipped` 原因。更新只接受已经检查且具备更新依据的项目。重复实际目录的更新会合并，卸载保留不同 Agent 入口的独立含义。
- `apply-library-many`：传入 `ids`（预览计划 ID 数组）、相同 `operation`、`confirmed=true`。全部项目执行前复核指纹、保护状态和引用；更新使用已暂存内容，并自动备份。卸载进入回收站。单项执行失败后停止，返回 `completed`、`remaining`、`results` 和错误，不能将部分完成显示为全部成功。
- `discard-previews`：传入 `ids`（预览计划 ID 数组）；只取消仍处于 preview 的计划，清理其暂存，已执行计划不受影响。

以上动作通过 `job-start` 调度；批量预览与执行会提供逐项进度。这些显式预览接口仅供兼容调用方使用；当前网页多选后点击命令，使用下述 execute 系列任务直接完成操作。

## Web 交互约定

### 归组列表与安装位置

`inventory.items` 仍是一条登记入口一项，ID 和历史身份不变。网页先按 `real_path` 去重，再以可靠的 provider、仓库 URL、ref、子目录和 Skill 名称归组；缺失或冲突来源仅按实际位置归并。不同仓库、分支及集合仓库不同名称不能仅因同名或同仓库合并。显示合并的 Agent 图标，不移动或创建链接。Agent 筛选先限定安装位置，再计算分组状态；任一位置有更新即显示有更新。全量检查仍覆盖全部可检测实际目录，不使用归组代表 ID 代替全部安装位置。

归组更新展开为各个可更新的实际目录并逐位置备份，已是最新或受保护位置不写入；部分失败返回具体安装路径，不显示全组成功。多选备份、标签和查询也展开为实际位置，同一真实目录仅处理一次。

卸载按所选入口计算受影响 Agent；只有一个时直接执行，多个时仅选择一次目录。单个链接只移除入口，真实共享目录必须提示全部已知影响。网页 `execute(operation=delete)` 和 `execute-library-many(operation=delete)` 传 `expected_references`：键为目标清单 ID，值为精确的 `{id,platform,path}` 引用数组。执行前及内部暂存后均与当前 `affected` 结果核对，任何变化拒绝写入并要求重新选择；旧 CLI/API 调用方可不传，不绕过已有保护和指纹校验。取消选择不生成暂存或待确认项目。

`reference_type=shared-root` 仍不允许直接写入。网页在当前 Agent 只有该别名入口时，可从同一 `real_path` 的已登记引用中找到可写真实目录，显示完整共享影响并要求选择该目录；即使只登记了一个 Agent，也不能静默从别名转为删除真实文件。没有已登记可写真实目录或相关引用受保护时不提供此操作。不可跳到同名的另一个实际目录，也不移动或重建 Agent 根目录链接。

删除、永久删除、卸载、清空回收站和设置中的移除操作，在提交请求前统一显示居中确认弹框：“是否确定删除？”，按钮为“取消、确认”，右上角可关闭。取消、关闭或 Esc 不发送删除请求；批量操作只确认一次。备份删除进入回收站，回收站永久删除。目录选择仍保留，选择后提交卸载时确认。其他明确命令点击后执行。网页不保存待确认队列。execute、execute-library-many、execute-backups、execute-restore-many、empty-trash 在一个后台任务中完成校验、暂存和写入；失败或结束后自动清理未执行暂存。所有写任务要求 confirmed=true，同一资源互斥，其他浏览和搜索仍可用。CLI 预览兼容接口不展示在网页。不可自动重放旧操作或重试失败写任务。

更新页顶部仅保留“检查更新”，各项目最近检查时间显示在表格内。检查详情分别说明最近一轮的范围/进度和各项目最后一次结果，不能把不同轮次混为同一批，也不能将零待更新解释为全库最新。

## List Filters

列表不再提供折叠筛选面板或独立排序下拉框，排序统一由表头完成。
全部 Skills 直接显示更新状态、分类、Agent，单个 Agent 导航页只显示前两项。
全部页选择 Agent 仅筛选列表，不改变导航页身份；分类、更新状态和搜索条件同时生效。
更新页显示更新状态、Agent；备份页显示备份类型、清理建议（建议删除、不建议删除、自行判断）。
回收站隐藏备份专属筛选。变更筛选清除该列表的批量选择，分页基于筛选后的完整集合计算。
清理建议复用缓存；选择建议筛选后，对搜索和类型范围内尚无建议的备份分批读取本地分析，
最多两个并行批次、每批八项，不将当前页误当成全量结果。无匹配时保留筛选入口。
