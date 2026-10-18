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

发现目录不修改本机 Skills。搜索只返回名称命中的项目，按完全匹配、开头、中间、结尾排序；腾讯 SkillHub 最多检索 4 页、120 条候选，ClawHub 和 SkillsMP 每来源最多返回 30 条候选。`search_limited=true` 表示仍可能有未检索的标题匹配项，此时 `total` 只是已检索范围内的匹配数。趋势/新品缓存 6 小时，搜索缓存 24 小时，GitHub 原文件核对缓存 6 小时。读取列表不携带完整文件预览。成功任务可能含来源错误，必须查看 `providers[].error` 或 `discovery-verify.error`，不能仅凭任务 succeeded 宣称来源全部正常。安装 URL 固定到已核对提交，仍需显式调用现有安装流程；ClawHub 原生托管安装尚未接入。

接口文档：[ClawHub](https://docs.openclaw.ai/clawhub/http-api)、[SkillsMP](https://skillsmp.com/docs/api)、[GitHub Contents](https://docs.github.com/en/rest/repos/contents)。默认匿名，服务进程可选读取 `SKILLSMP_API_KEY` / `GITHUB_TOKEN`；不暴露到浏览器、不写入缓存。远端重定向只能同 HTTPS 主机。

| action | 主要参数 | 结果 / 用途 |
|---|---|---|
| inventory | ids（可选）、lightweight（可选） | items、shared_targets、space、last_scan、insights、cache；lightweight=true 仅同步基础文件状态和数据库元数据，复用目录体积及上次更新检查，不遍历文件树；无缓存时首次建立完整快照 |
| inventory-cached | 无 | 上次完整快照；cache.available=false 时没有可用快照，后台任务运行时仍允许直接读取 |
| inventory-detail | id（清单 ID） | 只读返回该项完整快照，包括来源候选和比较详情；不扫描文件或初始化写库 |
| discovery-cached | view=trending/new/search、query（search 必填） | 只读 items、providers；provider 状态含 fresh/error/needs_refresh/retry_at；不初始化写库、不请求远端 |
| discovery-refresh | 同上，通过 job-start 调用 | ClawHub 正式目录/搜索、SkillsMP 正式关键词搜索；有效缓存不重抓，失败保留旧结果并返回错误状态，限流遵守来源级冷却 |
| discovery-verify | id（已缓存候选）、github_url（可选来源前提），通过 job-start 调用 | 明确 GitHub 路径的固定提交及 SKILL.md blob/元数据核对；返回 verification 或 error；不执行安装或来源绑定 |
| inspect-space | target | 第一层 entries 的分类、占用和 checked_at；不跟随软链接，不删除 |
| cache-inspect | 无 | 只读检查管理器已登记、闲置超过 24 小时的暂存；返回 count、size、fingerprint；blocked 时不可清理 |
| cache-clean | fingerprint、confirmed=true | 锁内复核后仅删除上述暂存，返回 completed、failures、removed_size、staged_space；部分失败为 partial，不刷新全部 Skills |
| uninstall-references | ids（Skills ID，最多 1000 项） | 按需只读返回每个目标的完整已知引用（包括已移除的 Agent）、protected 与 read_only；不重建清单 |
| install-roots | 无 | 启用的可编辑实际 Skill 根；排除共享根别名、插件、commands/agents |
| install-identify | url（HTTPS）、target_root | 私有暂存中的候选、名称、版本、描述、路径、大小；返回 selection ID |
| preview | operation=install、id（selection ID）、remote_path（候选原路径） | 固定安装内容的持久预览；仅新增、不覆盖 |
| scan | project（可选）、import_logs | 逐目录扫描结果，默认不导入日志 |
| check | target（ID/完整路径） | source_unknown/latest/update_available/local_modified 等 |
| check-many | ids（明确选择的 ID 数组）、scope（可选：partial/all/failed） | checks、skipped、failed；同一目标且来源相同只检查一次；failed 仅检查 last_check 失败或 attempt_failed 的对象 |
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
- 网页在 `inventory-cached`、`job-start`、`job-status` 顶层传 `summary:true`。清单摘要保留全部引用身份、Agent、来源、保护、判断结果和可搜索简介；省略来源候选、逐文件差异等详情。点开详情或来源编辑时用 `inventory-detail` 补齐；摘要不能作为写入授权。未传该参数的调用方仍获得完整兼容响应，写操作结果不会删去恢复证据。客户端声明接受 gzip 时，大于 64 KB 的 JSON 响应压缩传输。
- 有数据时刷新仅在按钮内显示加载图标，不遮挡列表；同范围、同读取模式的并发刷新合并为一个任务。局部同步不延长其他条目的缓存时间。备份列表保留一小时缓存。
- 移除 Agent 修补历史快照及展示引用，不清空整个 payload；`cache.recheck_ids` 标记受影响的剩余引用，网页轻量核对后清除对应 ID。不延长无关行同步时间，不清除既有 dirty 标记。登记记录保留，写入仍重新读取全部消费者及保护。
- 更新页主命令为“检查更新”，范围始终为全部可检查的已安装 Skills；失败项目在行内可重试或核对来源，不另设批量“重试失败项”。成功比较结果缓存 15 分钟，命中不延长有效期；本地内容、来源或基线变化时失效。普通失败下次点击重试，服务端明确限流时遵守冷却时间。相同内容的重试失败保留此前已确认的比较结果，并单独记录失败尝试，不抹除“有更新”。
- 来源解析、安装锁和本地 Git 元数据只在单次检查内复用；不同引用的明确绑定和来源冲突仍独立核对。`run.prepared_at` 记录准备完成时间；检查前先报告未知总量，准备完成再给出进度。GitHub REST 限流或暂时断连时，可通过同一确认仓库的 HTTPS Git 查询准确提交，再从官方归档域名下载；不猜测其他分支或同名项目，不关闭 TLS，不放宽文件校验、大小或解压限制。`api_retry_at` 单独保留 REST 冷却，`rate_limit_scope=github-archive` 的下载限流不能靠 Git 元数据查询绕过。重复仓库连接失败仅在本轮复用，下轮可以重试。
- 重试失败但已有“有更新”的证据时继续显示“有更新”；其他旧比较结果不冒充本轮成功，列表显示“查询失败”，详情保留错误及此前比较记录。404、包校验不一致和过大归档分别报告来源缺失、`remote_integrity` 和 `remote_archive_limit`，不自动替换来源或写入本地 Skill。
- `baseline_unknown` 表示已查到来源但缺少可信比较基线，更新页显示“无法判断新旧”并单独提供筛选；它既不是“查询失败”，也不能算作“已是最新”。
- `attempt_failed` 保留 `source_issue`、错误和 `attempted_at`，不改写原成功结果的 `checked_at`。持久化只发布最终结果，不先写原始失败再恢复旧证据；比较缓存被清理时，仅在 `comparison_key` 与当前路径、来源、本地指纹、基线及版本规则完全匹配时复用 last_check。来源修复表单允许编辑已有失败绑定；候选不自动绑定。
- HTTP 读取在原请求预算内分配重试时间；永久 DNS、证书、404、校验错误及 429 不立即重试。没有可用冷却头的限流默认等待 60 秒。`rate_limit_scope=github-git` 不可用针对性 Git 检查绕过。固定提交的目录缺失证据按仓库、路径及候选名称在进程中复用最多 15 分钟，仍返回 `remote_source_path`；新的检查先解析 ref，提交变化重新读取目录，网络失败不缓存为目录缺失。
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
| `directory-preview` | `path` | 发现集合目录中的 Skills，返回数量、条目和指纹；单个 Skill 根提示选择上一级，不自动扩大范围 |
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

卸载先用 `uninstall-references` 按需读取全部引用，再计算受影响 Agent；只有一个进入通用确认，多个先选目录再确认一次。单个链接只移除入口，真实共享目录提示全部已知影响。网页 `execute(operation=delete)` 和 `execute-library-many(operation=delete)` 传 `expected_references`：键为目标 ID，值为精确 `{id,platform,path}` 数组（不含 protected/read_only 扩展字段）。执行前及暂存后与 `affected` 核对，变化拒绝写入；旧调用方可不传，但不能绕过保护和指纹校验。取消选择不生成暂存或待确认项目。

`cache-clean` 也属于删除命令，在网页提交前使用同一个通用确认框。仅清理 `ASM_HOME/plans` 中已登记的 preview/selection/applied/discarded 闲置暂存；失败、运行中、中断引用、未知目录、链接或特殊文件均保留。不得把此接口用于 Agent 缓存、备份、回收站、已安装 Skills 或 `.asm-hold-*`。检查指纹过时拒绝执行，不能换成直接删目录。检查不会初始化写库；清理只修补暂存空间计数并记录历史，不使整个 Skills 清单失效。

`reference_type=shared-root` 仍不允许直接写入。网页在当前 Agent 只有该别名入口时，可从同一 `real_path` 的已登记引用中找到可写真实目录，显示完整共享影响并要求选择该目录；即使只登记了一个 Agent，也不能静默从别名转为删除真实文件。没有已登记可写真实目录或相关引用受保护时不提供此操作。不可跳到同名的另一个实际目录，也不移动或重建 Agent 根目录链接。

删除、永久删除、卸载、清空回收站和设置中的移除操作，在提交请求前统一显示居中确认弹框：“是否确定删除？”，按钮为“取消、确认”，右上角可关闭。取消、关闭或 Esc 不发送删除请求；批量操作只确认一次。备份删除进入回收站，回收站永久删除。目录选择仍保留，选择后提交卸载时确认。其他明确命令点击后执行。网页不保存待确认队列。execute、execute-library-many、execute-backups、execute-restore-many、empty-trash 在一个后台任务中完成校验、暂存和写入；失败或结束后自动清理未执行暂存。所有写任务要求 confirmed=true，同一资源互斥，其他浏览和搜索仍可用。CLI 预览兼容接口不展示在网页。不可自动重放旧操作或重试失败写任务。

更新页顶部仅保留“检查更新”，各项目最近检查时间显示在表格内。检查详情分别说明最近一轮的范围/进度和各项目最后一次结果，不能把不同轮次混为同一批，也不能将零待更新解释为全库最新。
备份扫描执行中显示页面中央进度。成功后短暂提示；未完成或失败时改在备份列表上方保留结果提示和详情入口，不以部分结果声称扫描范围内只有零项备份。

## List Filters

列表不再提供折叠筛选面板或独立排序下拉框，排序统一由表头完成。
全部 Skills 直接显示更新状态、分类、Agent，单个 Agent 导航页只显示前两项。
全部页选择 Agent 仅筛选列表，不改变导航页身份；分类、更新状态和搜索条件同时生效。本机清单、更新、来源、备份和回收站的普通搜索只匹配列表显示的名称，不匹配简介或路径；搜索时默认按标题命中位置排序，主动点击表头后改用该列排序。
更新页显示更新状态、Agent；备份页显示备份类型、清理建议（建议删除、不建议删除、自行判断）。
回收站隐藏备份专属筛选。变更筛选清除该列表的批量选择，分页基于筛选后的完整集合计算。
清理建议复用缓存；选择建议筛选后，对搜索和类型范围内尚无建议的备份分批读取本地分析，
最多两个并行批次、每批八项，不将当前页误当成全量结果。无匹配时保留筛选入口。

`backup-advice` 的单项分析失败返回 `cleanup_advice:null` 和已脱敏 `error`，不是已完成的 `review`。
网页只缓存成功结论；失败显示横线及错误提示，30 秒后重试，连续失败指数退避至最多 5 分钟。
离开备份/回收站或页面不可见时不自动重试。完整响应缺项也进入退避，不循环立即请求。
批量恢复忽略已知不可恢复项并提示数量和原因、保留跳过选择；可恢复项仍经过 `restore-targets` 和写入前校验。
