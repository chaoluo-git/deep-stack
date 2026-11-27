# WPK H5 牌局动作采集生产规范

目标是记录 WPK H5 牌桌已公开的动作、公牌和筹码，回答哪一桌、哪一手、哪个位置的玩家做了什么。当前 `h5-public` 从官网观战客户端采样，供观察与赛后核对。网络路线独立记录帧元数据，正式网络动作事件以验证过的包内字段为准。画面与官方牌局回顾可作独立对照。网络路线的首批验收覆盖 8 座和 9 座标准无限注德州扑克的观战与入座场景，并检查数据能否用于后续 EV 分析。

## 当前证据和数据来源

已有 HAR 包含 4 条连接的 3,327 条二进制消息。另一次纯网络实测在约 131 秒内写入 198 条二进制帧，172 条下行、26 条上行。扩展报告零丢弃，接收端正常清理配对文件。两轮都有约 5 秒的周期性往返。公开牌出现前后也有短突发，类似突发在其他时间重复出现。逐连接与方向检查时，头尾各 4 字节未出现重复，受检长度前缀候选为 0。独立探索把每条连接按时间划分为前半和留出集，起始位置的标准 Protobuf 线格式仅在前半 1,663 帧中偶合 2 次，在留出 1,664 帧中偶合 0 次。510 种受检 TLV 布局中，前半命中最多的一种在留出集中仅命中 9 次。是否存在加密或其他封装仍待证实，公开资料尚缺 WPK 动作消息规格和字段口径。 [WePoker 官网](https://www.wpk.com/) · [Chrome CDP WebSocket 帧定义](https://chromedevtools.github.io/devtools-protocol/tot/Network/#type-WebSocketFrame)

| 来源 | 当前用途 |
| --- | --- |
| 当前 Chrome WPK 观战页的客户端公开状态 | 采样可见公牌、动作及筹码变化。以已核对的桌、手、街次和玩家绑定保存归属。 |
| 当前 Chrome WPK 标签页的 WebSocket 摘要 | 记录匿名连接号、相对时间、方向、操作码、字节数、编码特征及本机批次到达时间。用于连接健康、周期识别和时序校准。 |
| 同桌画面人工标注 | 为已公开可见的座位、动作、街次、金额和公共牌提供独立对照。画面遮挡或证据冲突时保留缺项。 |
| 厂商授权接口、文档化的公开事件或官方牌谱 | 获得相应文档和样本后验证字段。牌谱可作结算核对或离线金标，实时来源须单独验证。 |

写入动作库前须有明确的数据来源和验证记录。负载、令牌、Cookie、带查询参数的地址、对手未公开的底牌以及从消息长度推断的玩家和动作均排除在采集结果之外。原始 HAR 仅供本机受控检查，清理版导出也可能包含完整消息数据。 [Chromium HAR 导出实现](https://github.com/ChromeDevTools/devtools-frontend/blob/main/front_end/models/har/Log.ts)

## 公开客户端代码证据

本次核对的公开 Cocos 浏览器主包版本为 `b7cb5`。Chrome HAR 中四条 WSS 连接的发起栈顶均为 `connectUrl`，在公开主包单行脚本的列号 11,682,298 处与 `HMFClient` 的浏览器 WebSocket 构造位置一致。这为当前样本的传输处理器提供了静态和抓包两份证据。 [WPK H5 入口](https://h5.wpk.com/) · [公开客户端主包](https://h5.wpk.com/assets/main/index.b7cb5.bin)

页面同时包含 MessagePack 和 Protobuf 运行时。启动器中的 MessagePack 资源解包示例处于注释中。3,327 条捕获消息从字节 0 尝试读取完整 MessagePack 对象时均无单对象匹配。完整多对象偶合量为 30 条，同长度随机对照为 26 条。`HMFClient` 的接收代码使用二进制缓冲区，并包含按分支进行 gzip 解包、应用层解密、业务 Protobuf 解析和派发的处理。`DataEncoder` 的部分保护分支使用 AES-GCM。具体某帧选用的处理分支与业务字段仍待验证。 [公开启动器](https://h5.wpk.com/main.a85d7.js) · [公开 Protobuf 库](https://h5.wpk.com/src/assets/scripts/base/protobuf.0554b.js) · [MessagePack 规范](https://github.com/msgpack/msgpack/blob/master/spec.md)

网关分支所用的 `proxy.BaseFrame` 在公开资源中有完整定义。字段为 `type`（#1，int32）、`data`（#2，bytes）、`target`（#3，string）和 `gzip`（#4，bool）。用该定义检查全部 3,327 条旧抓包，要求存在类型字段、字段号和线类型吻合且完整消耗帧字节。从偏移 0 到 8 的每个位置，完整匹配均为 0。当前证据能验证客户端分支和结构，但尚不足以判定哪一种处理分支用于某条抓包。 [公开 BaseFrame 定义](https://h5.wpk.com/assets/resources/import/99/995cd24e-2ac7-446b-b05f-b411f290c632.a7c5f.json)

| 客户端静态定义 | 可验证的字段名示例 |
| --- | --- |
| `NoticePlayerAction` | `last_action_seat_id`、`action_type`、`amount`、`ActionSeq`、`pots`。`GameNetWork` 注册 `MsgID_PlayerAction_Notice` 处理器，解码后交给牌桌更新流程。 |
| `NoticePlayerActionTurn` | `curr_action_seat_id`、`action_time`、`call_amount`、`minimum_bet`、`last_valid_raise_amount`。 |
| `NoticeGameBlind` 与 `NoticeGameAnte` | 庄位、大小盲座位和金额、逐座前注信息。 |
| `PlayerInfo` 与 `NoticeCommunityCards` | `playerid`、`seatid`、`stake`、`round_bet`，以及公共牌、下注轮次和 `gameUuid`。 |

这些名称和字段属于公开客户端的业务层定义。原始会话里的具体动作、金额单位和事件顺序仍需通过已获准使用的解码来源或同手牌牌谱核验。客户端还打包有 `GameActionRecordDataCtrl`、`ClubHandRecordsNew` 和 `HoldemReplayScene`。当前 Chrome H5 观战牌局回顾入口已实测，结构化导出能力仍待核实。 [公开客户端主包](https://h5.wpk.com/assets/main/index.b7cb5.bin)

## 观战回顾实测

在 H5 观战桌实测，右上角叠牌图标打开官方牌局回顾。面板可翻阅不同手，分街显示已公开的动作、金额和公共牌，并标记庄位及大小盲。同一场次翻手时，面板标题的「牌局ID」保持不变。验收标签使用手序号区分，服务端唯一手牌标识须从独立来源核对。客户端有本地缓存与内部缺页补取逻辑，观察到的回顾没有提供结构化导出入口。 [WPK H5](https://h5.wpk.com/) · [公开客户端主包](https://h5.wpk.com/assets/main/index.b7cb5.bin)

新桌再次取得原始 WebSocket 样本。2026 年 11 月 27 日 14:48:11 至 14:53:53 的最终导出记录 496 帧，其中下行 424、上行 72，全部为二进制。第 14 手从看到翻前开始到看到结算之间记录了 76 帧，其中下行 64、上行 12。人工先在实时牌桌确认翻牌 J♦、6♠、2♣，再确认转牌 7♦ 和河牌 2♥。官方回顾确认完整牌面，并显示 BTN 和 BB 各自的翻前跟注 22、翻牌过牌、转牌跟注 54、河牌过牌。公开 `proxy.BaseFrame` 从偏移 0 至 8 在全部帧和同手观察区间内均无完整匹配。报文长度和到达时间只能定位候选帧，实际动作的线级顺序和金额语义尚待授权机器数据核对。

公开客户端的业务派发还确认了 `actionNotify → BattleActionMsg` 和 `roundChangeNotify → BattleRoundChangeMsg`。前者的 `actionList` 提供座位、动作、金额候选及 `actionId`、`round`。后者的 `dealPublicCards` 是每轮新增的公共牌，客户端随后归入当前牌面。采集应按手牌建状态并累积公共牌。 [公开响应消息定义](https://h5.wpk.com/assets/resources/import/2f/2f6eb76a-cca7-498d-bab6-1260d71d9aa9.7c053.json)

## 客户端公开显示字段实测

使用 Chrome DevTools 在已打开的 H5 观战页做只读验证。实时读取活跃 Cocos 场景的座位动作文本、界面本街投入和已显示的公共牌 Sprite 牌值。官方回顾则读取当前已打开手牌的公开公牌槽和按街排列的渲染动作标签。

- 第 14 手的官方回顾结构化返回 J♦、6♠、2♣、7♦、2♥，庄位和大盲位均为翻前跟注 22、翻牌让牌、转牌跟注 54、河牌让牌，与此前独立的画面核验相符。
- 第 59 手连续观测约 30 秒，保留 13 个变化状态。客户端读到完整公共牌 K♣、3♥、J♦、10♠、K♥，并先后显示槽位 4 转牌下注 54、槽位 2 转牌跟注 54。已用同手官方回顾对照牌值、街次和两项金额。局部采样快照可能出现转牌已更新而第 4 张牌仍在绘制的情况。入库前按街次核验可见牌槽数量。

现场校验还确认，两个客户端模型的场次 ID 均与官方回顾标题的「牌局ID」一致，当前手号随换手递增，八位活跃玩家的座位模型与头像模型 ID 分别一致。另在 9 座桌核对了一位玩家，公开头像资料弹窗的 ID 与该座位一致，当时客户端观战座位状态为 `0`。完整动作时序、遗漏统计、跨客户端版本兼容和原始 WebSocket 到业务事件映射仍待验证。 [Chrome DevTools 调试参考](https://developer.chrome.com/docs/devtools/javascript/reference)

## 使用 H5 观战采集

启动观战接收器，再保持已打开标准德州观战桌的 Chrome 标签页可见。关闭同一标签页的 DevTools，在「WPK 采集」选择终端显示的配对文件并点击「开始」。扩展约每 500 毫秒读取一次已展示的状态，切到后台或退出牌桌期间等待。结束时先点击扩展的「停止」，再关闭接收器。 [扩展安装与配对](../README.md#h5-观战采集)

```bash
deep-stack h5-public --db players.sqlite3
deep-stack h5-public-show --db players.sqlite3 --limit 30
deep-stack h5-public-show --db players.sqlite3 --limit 30 --identities
deep-stack h5-public-show --db players.sqlite3 --player 001234 --limit 30
deep-stack players --db players.sqlite3 show 001234 --events 20
```

`h5-public --json` 输出 JSON 行。`h5-public-show` 读取最近的观察记录，默认隐去桌号、手号和玩家 ID。加 `--identities` 可在本机核对这些字段。`--player` 筛选该玩家对应的观察，`players show` 查询该玩家的汇总。两条命令均使用公开的数字玩家 ID，替换示例的 `001234` 即可。数据库保存在指定的本机路径。

当前观察事件包括 `public_table`、`public_board`、`public_identity`、`public_chips` 和 `public_action`。牌桌 ID 由两个客户端模型核对，手号取官方回顾引用的当前手号。玩家 ID 以同帧座位与头像资料来源一致且唯一为准，冲突或不完整时留空。只有具备已确认桌号、手号、街次和玩家的观察动作才进入玩家动作统计。`wager` 是当街可见累计投入，`stack` 是可见剩余筹码读数。数字标签按界面读数记录，带单位时要求客户端的金额格式化结果与显示一致。

`public_table.table_details` 记录公开盲注标签中的 `small_blind`、`big_blind`、`ante` 和可选 `third_blind`。现场样本 `2/4(2)` 对应小盲 2、大盲 4、前注 2。`dealer_slot` 取唯一可见的静态 `D` 标记，标记在移动或无法和当前座位核对时留空。大小盲的具体座位需要同手牌谱核对。

界面标签可能停留并晚于实际动作。采集会合并可识别的重复标签，标签相同而累计投入增加时可保留读数更新 `wager_update`。累计投入可在 `h5-public-show` 中查询。`players show` 中的动作统计不会把动画期间的累计投入算作单次动作金额。两次采样之间可以漏过动作，同帧也无法确认各座位的先后顺序。缺少整手观测时应以官方回顾核对。打开牌桌右上角叠牌图标后可逐手比对公牌、各街动作和金额。该接收器用于观战与赛后回查，实时 EV 建议有独立的决策条件和入口。`h5-live` 与 `h5-profile` 用于网络帧和封包分析。

## 实时包采集、封包分析和核对

`h5-live` 实时写入网络帧元数据，`h5-profile` 对本机导出的 HAR 或 CDP 捕获做二进制结构检查。

| 路径 | 转换 | 本机输出 |
| --- | --- | --- |
| `h5-live` | 从 Chrome CDP `Network.webSocketFrameSent` / `Network.webSocketFrameReceived` 读取帧。在扩展中按 opcode 检查文本或 Base64 表示，计算长度和相对时间，提取编码特征及匿名连接号。`/v1/frames` 接收通过鉴权和字段校验的批次。 | 终端或 `--journal` JSONL 中的 `h5_batch`，带接收时间、抓包批次和帧元数据。原始帧、解密内容和玩家动作均无 SQLite 记录。 |
| `h5-public` | 读取正在显示的观战客户端组件，筛选公牌、动作标签、座位、身份及桌况。`/v1/public` 校验快照并按手、街和座位提取变化。 | SQLite `h5_public_observations` 存观察，`source` 为 `h5_client_display`。同帧桌号、手号、街次和唯一玩家 ID 都核实后才写入 `hand_seats` 和 `visible_actions`，供玩家统计。 |

`h5-inspect` 和 `h5-profile` 的输入是本机已保存的 HAR 或 CDP 捕获，产物是 JSON 网络摘要或封包结构统计。本批原始二进制帧的业务字段仍待验证。`h5-public-show --player ID` 可在混用过不同采集模式的本机数据库里单独查看该玩家的 H5 客户端观察。

```bash
deep-stack h5-live --port 0 --journal network-session.jsonl --json
deep-stack h5-profile capture.har -o packet-profile.json
```

1. 在 WPK 标签页打开已安装的 Chrome 扩展，选择终端显示的配对文件并开始采集。时间轴保存 `h5_batch`。`arrival_ms` 是本机接收批次的时刻，`offset_ms` 以每次 Chrome 采集的首个可见 CDP 帧为零，两者使用独立时钟基准。浏览器停止后按 `Ctrl+C` 关闭本机接收端。停止原因和丢弃计数可在扩展查看。 [Chrome CDP 时间字段](https://chromedevtools.github.io/devtools-protocol/tot/Network/#event-webSocketFrameReceived)
2. 封包报告按连接和方向归纳包长与头尾分布，扫描有限范围内的 1、2、4 字节长度前缀。报告列出候选支持数和覆盖率，适用于筛选值得验证的结构。原始消息只在本机处理，派生报告保存统计量。
3. 独立记录实际观战画面中的座位、街次、动作和金额，或取得已获准使用的完整牌谱。按时序提出字段假设，用不同手牌和独立会话复测。对已有双源校准文件可用 `h5-correlate TIMELINE -o REPORT` 统计样本前后的候选帧数量。单个候选只表示时间范围内有一帧，业务映射仍待独立验证。

当前网络时间线支持本机匿名校准及离线复核。跨会话时钟对齐、丢弃间隔标记、协议动作持久化和自动对账属于后续生产能力。

## Hand2Note 兼容性和牌谱对账

Hand2Note 4 的当前官方 Android 支持表列出 WePoker，注明 Windows 上的 LDPlayer 9 模拟器路线，Windows 桌面应用 HUD 列未勾选。该表还分别列出 PokerBros、PPPoker 等应用。经核对当前官方桌面表及说明，Chrome 中的 `h5.wpk.com` 仍没有可验证的 HUD、牌谱读取或实时动作接口支持记录。WPN 指 Winning Poker Network，官方教程适用于 ACR Poker、Black Chip Poker 等该网络房间。 [Hand2Note 4 支持说明](https://hand2note.com/Help/supported-sites) · [官方 Android 支持表](https://docs.google.com/spreadsheets/d/e/2PACX-1vSfUG8DRkQmc7objIMXjfYqCbDAJWbJZsgJtWtLdL74g7WCHRF3C_eA_EeeHyvonQeOXLd5PUO5GCgi/pubhtml?gid=68794297&single=true) · [WPN 教程](https://hand2note.com/Help/PokerSites/wpn-hud)

Hand2Note 3 的官方亚洲页面也曾列出 WePoker、PokerMaster、PokerKing、RedDragon 和 PP/KK Poker，说明的是旧版 Android 模拟器和 ASIA 订阅。该页面也列有观战牌谱采集功能，未逐个房间确认。Hand2Note 4 提供从文件、文件夹及部分追踪器数据库导入，对房间原生格式有依赖。已导入的手牌可通过官方导出功能保存为文本。 [旧版亚洲应用列表](https://hand2note3.hand2note.com/PokerMaster) · [Hand2Note 4 导入指南](https://hand2note.com/Help/getting-started) · [文本导出指南](https://hand2note.com/Help/Features/marked-hands)

后续可从授权的 WPK 牌谱或受支持客户端中导出的、已获准使用的 Hand2Note 牌谱取得样本。保留房间、客户端版本和原生格式。在本机用少量已完成手牌核对桌、手、座位、动作顺序、下注口径与结算。确认同一手牌标识及金额一致后，用牌谱核对网络解析结果，并保存各字段的证据来源。导入适配以真实样本和格式确认作为开工条件。Hand2Note 的 Alias 按房间和用户名管理别名。只有核对属于同一人时才合并个人统计，CSV 可用于迁移别名。 [Hand2Note Alias 指南](https://hand2note.com/Help/Features/aliases)

## 正式动作事件合同

以下是后续生产网络动作记录的合同。当前校准文件和 `h5-public` 观察各自采用独立的精简字段。正式网络动作由已验证的包内字段产生，画面和赛后牌谱可为对应字段提供独立验证。

| 字段组 | 约定 |
| --- | --- |
| 合同与来源 | `schema_version`、`source.kind`、`source.version`、`source.ref`。生产动作记录可核验的网络来源和解析版本，人工标注与官方牌谱作为独立证据引用。引用只带本机匿名标识与必要的行号或采样号。 |
| 范围与顺序 | `session_id`、`table_id`、`hand_id`、`event_id`、`sequence`、`street`。保存来源定义的作用域与顺序，以及标注的本机观察次序。 |
| 动作和身份 | `kind` 规范为 `fold`、`check`、`call`、`bet`、`raise`、`all_in`。另记录 `seat`、`binding_revision`，以及允许保存且证据充分时的 `player_ref`。物理座位与本手位置分别保存。座位绑定覆盖的手牌范围须明确，换座或冲突后重新核对。 |
| 时间与完整性 | `observed_at_ms` 或采集区间、`time_basis`、`integrity`。对每项关键字段记录 `observed`、`verified` 或 `unknown`，并保留证据引用。未确认字段用 `null`。 |
| 筹码 | 以来源约定的单位分别记录 `amount_paid`（本次追加）、`raise_to`（加注到）、`street_total`（本街累计）、`stack_after` 和 `pot_total`。金额确定为零时写入 0，并注明动作规则或来源。盲注、ante、退款和边池等分项独立记录并用于对账。 |

同一来源的动作使用 `source`、`table_id`、`hand_id`、`event_id` 组成幂等键。完全相同的事件重放后保留单条。相同键出现冲突时保留冲突状态和证据，暂停归因。官方消息按文档定义的序号排序，并按文档判断可见序号是否连续。画面流仅按采集时序排列，同帧的动作次序和重复标签保持待核对。

断线、停采、换桌、队列拥堵或切换证据来源时记录缺口。重连先确认桌局和座位版本，再按官方文档支持的快照或重放续接。身份冲突暂停绑定，缺口影响当前牌局时暂停动作累计和 EV 建议。过期的建议从当前视图撤销，并保留所用输入的时间基准供回查。

## 分阶段验收

| 阶段 | 验收结果 |
| --- | --- |
| 同步记录 | 实际 Chrome 扩展与本机网络接收端连续运行，文件可回放。跨来源核对说明时间误差范围，停止原因可查。 |
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
| 高速批次、超限、错误数据和敏感信息 | 接收仅绑定本机，按现有批次限制验证，队列有上限，报告通过字段白名单断言。秘密和原始消息不进入日志、错误及派生报告，公开数字身份在本机存储，默认输出中隐藏。 |
