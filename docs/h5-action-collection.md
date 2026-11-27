# WPK H5 牌局动作采集生产规范

目标是持续记录当前 WPK 牌桌上已经公开显示的动作，回答哪一桌、哪一手、哪个位置的玩家在何时做了什么，以及能确认的筹码变化。记录应可回查来源，可发现漏采，并能在桌况和金额足够明确时交给 EV 分析。首批验收覆盖 8 座和 9 座标准无限注德州扑克的观战与入座场景。

## 当前证据和数据来源

已有 HAR 包含 4 条连接的 3,327 条二进制消息，另一次浏览器实时采集收到 87 条帧元数据。现有证据支持识别约 5 秒的周期性往返。公开牌出现前后也有短突发，类似突发在其他时间重复出现。公开资料尚缺 WPK 动作消息规格、字段口径和授权的实时数据接口。 [WePoker 官网](https://www.wpk.life/) · [Chrome CDP WebSocket 帧定义](https://chromedevtools.github.io/devtools-protocol/tot/Network/#type-WebSocketFrame)

| 来源 | 当前用途 |
| --- | --- |
| 当前 Chrome WPK 标签页的 WebSocket 摘要 | 记录匿名连接号、相对时间、方向、操作码、字节数、编码特征及本机批次到达时间。用于连接健康、周期识别和时序校准。 |
| 同桌画面 OCR 和手工核对 | 记录公开可见的座位、动作标签、街次、金额和公共牌。画面遮挡或证据冲突时保留缺项。 |
| 厂商授权接口、文档化的公开事件或官方牌谱 | 获得相应文档和样本后验证字段。牌谱可作结算核对或离线金标，实时来源须单独验证。 |

写入动作库前须有明确的数据来源和验证记录。负载、令牌、Cookie、带查询参数的地址、对手未公开的底牌以及从消息长度推断的玩家和动作均排除在采集结果之外。原始 HAR 仅供本机受控检查，清理版导出也可能包含完整消息数据。 [Chromium HAR 导出实现](https://github.com/ChromeDevTools/devtools-frontend/blob/main/front_end/models/har/Log.ts)

## 同步采集与时序校准

当前实现提供两个命令。使用本机的新 JSONL 文件作为 `TIMELINE`，另选文件保存报告。

```bash
deep-stack h5-calibrate TIMELINE
deep-stack h5-correlate TIMELINE -o REPORT
```

1. 启动 `h5-calibrate`，在 WPK 标签页打开现有 Chrome 扩展，选择终端显示的配对文件并开始采集。保持目标牌桌完整可见。观战可加 `--spectate`，锁定实体座位数可用 `--table-seats 8` 或 `--table-seats 9`。默认按 0.75 秒间隔采样，可用 `--interval` 调整，用 `--seconds` 限定时长，或按 `Ctrl+C` 停止。
2. 时间线写入 `h5_batch`、`screen_action` 和 `screen_board`。`arrival_ms` 是本机接收批次的时刻，`sample_started_ms` 和 `at_ms` 分别记录截屏开始与完成，单位均为相对于这一条时间线的毫秒。旧记录没有 `sample_started_ms` 时按 `at_ms` 处理。帧的 `offset_ms` 以每次浏览器采集的首个 CDP 帧为零，自己的时钟基准独立于本机时间线。画面记录保留公开街次、座位、动作、可见金额和公共牌，手牌标识在文件内匿名化。
3. `h5-correlate` 按批次到达时间找候选。默认取截屏开始前至结束后的各 1,500 毫秒，可用 `--window-ms` 调整。报告按动作和公共牌标记分别统计零个、单个和多个候选，记录候选批数、帧数和省略数量，也统计重复批与被忽略的残缺尾行。浏览器相对偏移供查看排序和条件上界，实际帧时刻的下界未知。单个候选只表示该窗口内有一帧，业务映射仍待验证。 [Chrome CDP 时间字段](https://chromedevtools.github.io/devtools-protocol/tot/Network/#event-webSocketWillSendHandshakeRequest)

当前时间线支持本机匿名校准及离线复核。跨会话时钟对齐、丢弃间隔标记、协议动作持久化和自动对账属于后续生产能力。停止原因和丢弃计数可在扩展查看。

## Hand2Note 兼容性和牌谱对账

Hand2Note 4 的当前官方 Android 支持表列出 WePoker，注明 Windows 上的 LDPlayer 9 模拟器路线，Windows 桌面应用 HUD 列未勾选。该表还分别列出 PokerBros、PPPoker 等应用。经核对当前官方桌面表及说明，Chrome 中的 `h5.wpk.com` 仍没有可验证的 HUD、牌谱读取或实时动作接口支持记录。WPN 指 Winning Poker Network，官方教程适用于 ACR Poker、Black Chip Poker 等该网络房间。 [Hand2Note 4 支持说明](https://hand2note.com/Help/supported-sites) · [官方 Android 支持表](https://docs.google.com/spreadsheets/d/e/2PACX-1vSfUG8DRkQmc7objIMXjfYqCbDAJWbJZsgJtWtLdL74g7WCHRF3C_eA_EeeHyvonQeOXLd5PUO5GCgi/pubhtml?gid=68794297&single=true) · [WPN 教程](https://hand2note.com/Help/PokerSites/wpn-hud)

Hand2Note 3 的官方亚洲页面也曾列出 WePoker、PokerMaster、PokerKing、RedDragon 和 PP/KK Poker，说明的是旧版 Android 模拟器和 ASIA 订阅。Hand2Note 4 提供从文件、文件夹及部分追踪器数据库导入，对房间原生格式有依赖。已导入的手牌可通过官方导出功能保存为文本。 [旧版亚洲应用列表](https://hand2note3.hand2note.com/PokerMaster) · [Hand2Note 4 导入指南](https://hand2note.com/Help/getting-started) · [文本导出指南](https://hand2note.com/Help/Features/marked-hands)

后续可从授权的 WPK 牌谱或受支持客户端中导出的、已获准使用的 Hand2Note 牌谱取得样本。保留房间、客户端版本和原生格式。在本机用少量已完成手牌核对桌、手、座位、动作顺序、下注口径与结算。确认同一手牌标识及金额一致后，用牌谱为屏幕记录补证，并区分画面读数与牌谱字段。导入适配以真实样本和格式确认作为开工条件。Hand2Note 的 Alias 按房间和用户名管理别名。只有核对属于同一人时才合并个人统计，CSV 可用于迁移别名。 [Hand2Note Alias 指南](https://hand2note.com/Help/Features/aliases)

## 正式动作事件合同

以下是后续生产记录的合同。当前校准文件采用独立的精简字段。截图先产生观察事件，经过授权的数据源或人工核对可提升对应字段的证据等级。

| 字段组 | 约定 |
| --- | --- |
| 合同与来源 | `schema_version`、`source.kind`、`source.version`、`source.ref`。来源类型区分画面、人工核对、厂商授权事件和官方牌谱。引用只带本机匿名标识与必要的行号或采样号。 |
| 范围与顺序 | `session_id`、`table_id`、`hand_id`、`event_id`、`sequence`、`street`。保存授权来源定义的作用域和顺序。画面生成的桌、手、事件标识作用域为本机观察会话，顺序为观察次序。 |
| 动作和身份 | `kind` 规范为 `fold`、`check`、`call`、`bet`、`raise`、`all_in`。另记录 `seat`、`binding_revision`，以及允许保存且证据充分时的 `player_ref`。物理座位与本手位置分别保存。座位绑定覆盖的手牌范围须明确，换座或冲突后重新核对。 |
| 时间与完整性 | `observed_at_ms` 或采集区间、`time_basis`、`integrity`。对每项关键字段记录 `observed`、`verified` 或 `unknown`，并保留证据引用。未确认字段用 `null`。 |
| 筹码 | 以来源约定的单位分别记录 `amount_paid`（本次追加）、`raise_to`（加注到）、`street_total`（本街累计）、`stack_after` 和 `pot_total`。金额确定为零时写入 0，并注明动作规则或来源。盲注、ante、退款和边池等分项独立记录并用于对账。 |

同一来源的动作使用 `source`、`table_id`、`hand_id`、`event_id` 组成幂等键。完全相同的事件重放后保留单条。相同键出现冲突时保留冲突状态和证据，暂停归因。官方消息按文档定义的序号排序，并按文档判断可见序号是否连续。画面流仅按采集时序排列，同帧的动作次序和重复标签保持待核对。

断线、停采、换桌、队列拥堵或切换证据来源时记录缺口。重连先确认桌局和座位版本，再按官方文档支持的快照或重放续接。身份冲突暂停绑定，缺口影响当前牌局时暂停动作累计和 EV 建议。过期的建议从当前视图撤销，并保留所用输入的时间基准供回查。

## 分阶段验收

| 阶段 | 验收结果 |
| --- | --- |
| 同步记录 | 实际 Chrome 扩展与牌桌观察连续运行，文件可回放。跨来源时间不确定的样本和停止原因可解释。 |
| 字段验证 | 用同桌的已标注公开动作或获准牌谱逐项核对座位、街次、动作和金额口径。只对已验证的字段建立版本化映射。 |
| 发布门槛 | 用未参与标定的真实样本复测准确率、漏报、时延、重连、乱序和匿名输出，确认过期或证据不足时进入等待。 |

建议的首批量化目标为至少 300 条人工核对的公开动作，来自不少于 10 手和两个独立采集会话，覆盖六类动作及 8 座、9 座。座位与动作同时正确的精确率目标为 99%，明确可见动作的召回率目标为 95%，已输出金额和真值完全相符的比例目标为 99%。每类动作分别汇报分子、分母、漏报和误报，证据不足的输出同时计入缺项。动作从首次可见到输出的延迟目标为第 95 百分位不超过 1.5 秒，另报采样、OCR 与排队延迟。有效行动时间、数据完整度或筹码规则未通过校验时应等待。上述数值为验收提案，待基准结果和产品要求确认。

| 测试场景 | 核对项 |
| --- | --- |
| 8 座和 9 座，宽窗、窄窗、四色与旧牌面 | 座位与公开牌定位，闲置布局和主题变化恢复。 |
| 空座、留座、弃牌、同名、换人和重新入座 | 区分座位状态，旧身份停用，新绑定从明确的手牌范围生效。 |
| 跟注、让牌、弃牌、下注、加注、全下，盲注、ante 与退款 | 动作归属、金额口径、底池对账，覆盖同街再次行动和快速连续动作。 |
| 断线、重连、重复批次、乱序和缺帧 | 幂等、单一顺序、缺口可追踪，有明确验证条件时恢复输出。 |
| 截图遮挡、客户端切桌、动作倒计时变化和高延迟 | 确认当前行动者，过期撤下建议，保留可回查的等待原因。 |
| 高速批次、超限、错误数据和敏感信息 | 接收仅绑定本机，按现有批次限制验证，队列有上限，报告通过字段白名单断言。合成秘密、原始内容和原始身份都不得出现在日志、错误或报告。 |
