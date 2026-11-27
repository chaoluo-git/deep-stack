# Deep Stack 德州扑克牌桌识别与 EV 分析

在本地记录 WPK H5 观战牌桌的公开动作、公牌和筹码，也可从截图提取可见信息。用已核对的牌局状态复盘时，可以比较候选动作从当前决策点出发的筹码期望值（EV）。结果取决于输入的对手范围、响应概率、下注金额和结算规则。

## 适用范围

- 首版使用标准 52 张牌的无限注德州扑克和普通筹码 EV。短牌、特种玩法、锦标赛奖金与 ICM 需要单独的规则及模型。
- WPK H5 的实地观察样本是横向九座牌桌，左右各三座、顶部两座、底部中央为自己。昵称会被操作提示盖住，弃牌提示可压在头像上，手牌弃牌后会变暗。公共牌在中间，当前投入与剩余筹码靠近座位。浏览器的无障碍树没有提供这些文字字段，屏幕识别入口从截图读取。
- 画面中心有上下两行金额。观察到的单帧中，两行金额的差值恰好等于一个座位的一笔投入。底池合计时应以牌谱或人工核对为准。观察到的 `2/4(2)`、带入上限和保险等字段未用于推断本桌具体计价规则。
- 座位编号用于关联同一手牌的画面。余额是显示的剩余筹码，本轮投入和整手累计投入单独记录。动作日志只能记录采样到的变化。重复出现的同标签以及两帧之间的动作，需要用完整牌谱补齐。截图缩放、遮挡、动画和主题会影响识别，自动回归使用固定合成截图序列。
- `wpk.com` 官网公开说明会打击伙牌和作弊。截至 2026 年 11 月 26 日，在官网及其新闻、下载、品牌等公开入口中尚未核实到对第三方实时辅助的官方条款。截图识别的自动回归验证使用本地合成数据。[WPK 官网](https://www.wpk.com/) · [WPK H5](https://h5.wpk.com/)

## H5 观战采集

打开 Chrome 里的 WPK H5 德州观战牌桌，保持目标标签页可见。该模式直接读取官网客户端已显示的牌桌状态，将变化保存到本机 SQLite。查询中的 `source: "h5_client_display"` 标明这批观察来自客户端画面组件。WebSocket 抓包的元数据由 `h5-live` 单独记录。

```bash
deep-stack h5-public --db players.sqlite3
```

1. 在 Chrome 118 或以上打开 `chrome://extensions`，启用「开发者模式」后加载未打包的扩展。源码目录为 `extensions/wpk-h5-capture`，独立包内为 `wpk-h5-capture`。已有扩展时重新加载即可。
2. 关闭目标标签页的 DevTools。在 WPK 标签页打开「WPK 采集」扩展，选择终端显示的本机配对文件。看到「准备采集公开牌桌快照」后点击「开始」。文件选择器关闭弹窗时，可选「在独立窗口配对」。
3. 终端会显示已观察到的动作、公牌、盲注金额、前注和庄位。盲注金额来自牌桌的公开标签，庄位来自可见的 `D` 标记。结束时点击扩展的「停止」，再按 `Ctrl+C` 关闭本机接收程序。重启接收程序后使用新配对文件。

```bash
deep-stack h5-public-show --db players.sqlite3 --limit 30
deep-stack h5-public-show --db players.sqlite3 --limit 30 --identities
deep-stack h5-public-show --db players.sqlite3 --player 001234 --limit 30
deep-stack players --db players.sqlite3 show 001234 --events 20
```

本机数据库可按官网头像资料页的公开数字 ID 关联玩家。只有牌桌、手号、街次与玩家均已确认的动作才归入该玩家的统计。采集终端和 `h5-public-show` 默认隐藏桌号、手号与玩家 ID。`--identities` 在本机查询中显示它们。用 `--player` 筛选某位玩家的观察记录，或用 `players show` 查询其汇总。将示例 `001234` 换成已核对的玩家 ID。

动作标签会停留，本街投入是当街累计读数。两次采样之间的动作、同一时刻多个座位的先后顺序及缺失的整手记录需用官方牌局回顾核对。该模式用于观战记录和赛后核对，现场建议仅在独立的 `live` 截图入口及其决策条件下提供。观战采集与下面的网络帧统计各有独立配对文件和输出。 [动作采集规范](docs/h5-action-collection.md)

## H5 网络观察

Chrome 开发者工具的 Network 面板可观察当前登录会话。勾选 Keep log，在过滤框填 `resource-type:websocket is:running`，选择状态 `101` 的连接后打开 Messages，可核对收发方向、时间和消息长度。已有连接可直接观察。[Chrome Network 文档](https://developer.chrome.com/docs/devtools/network/reference/#websocket-messages)

先在 Network 面板选择 **Export HAR (sanitized)**，将文件保存到下载目录，例如命名为 `h5-wpk.har`。然后在项目目录运行以下命令。若保存到了其他位置或使用了不同文件名，请把第一个参数换成实际导出的文件路径。同样可以传入 Protocol Monitor 导出的 JSON 路径。[Protocol Monitor](https://developer.chrome.com/docs/devtools/protocol-monitor)

```bash
python3 -m deep_stack h5-inspect "$HOME/Downloads/h5-wpk.har" -o "$HOME/Downloads/h5-summary.json"
```

`h5-inspect` 读取已有文件。缺少输入文件时，它会报告查找的路径。

报告含匿名连接编号、相对时间、方向、操作码、字节长度和编码特征计数。Chrome 的清理版 HAR 仍可能带有原始消息与请求地址，适合在本机处理和分享派生的 `h5-summary.json`。样本较大时，可缩短采样窗口或筛选当前连接后导出。当前样本确认能持续采到二进制消息，从原始帧到座位、筹码、公牌和动作的字段映射仍待验证。[Chromium HAR 导出实现](https://github.com/ChromeDevTools/devtools-frontend/blob/main/front_end/models/har/Log.ts#L139-L176)

### 实时采集

启动本机接收程序。终端会显示配对文件的位置。`--json` 输出机器可读的 JSON 行，供本机程序接入。

```bash
deep-stack h5-live
deep-stack h5-live --port 0 --journal network-session.jsonl --json
```

使用上文所述的 Chrome 扩展，在 `https://h5.wpk.com` 标签页选择 `h5-live` 终端显示的配对文件。看到「准备采集帧元数据」后点击「开始」。采集时 Chrome 会显示调试提示。点击「停止」、关闭目标标签页或离开 WPK 会结束采集。接收程序重启后使用新配对文件。[Chrome Debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)

接收程序只监听 `127.0.0.1`。扩展在本地生成帧的时间、方向、操作码、字节数与编码标签，HTTP 批次只带这些元数据和匿名标识。配对令牌由本地文件提供，监听停止后配对失效。终端会把满足周期和延迟条件的往返标为「可能是保活或轮询」。可用 `h5-inspect --patterns` 对已导出的文件做同样的检测。牌局事件仍需要可验证的字段映射后才能进入 EV 分析。[CDP WebSocket 帧定义](https://chromedevtools.github.io/devtools-protocol/tot/Network/#type-WebSocketFrame)


### 本机二进制封包分析

`--journal` 为本次监听创建新的网络帧时间轴，记录浏览器的帧相对时间和本机批次到达时间。对已有 HAR 或 CDP 捕获，可在本机检查字节结构。

```bash
deep-stack h5-profile "$HOME/Downloads/h5-wpk.har" -o packet-profile.json
```

报告按匿名连接和方向统计包长、头尾重复程度、字节熵和候选长度字段。该命令读取 HAR 或 CDP 捕获中的二进制消息，网络时间轴只有帧元数据，适合实时监测和按时间核对。生产动作以已验证的消息字段为准，观战画面或已获准使用的牌谱可供人工标注和核对。实况证据、Hand2Note 的可用范围及验收条件见[动作采集规范](docs/h5-action-collection.md)。

## 安装与启动

GitHub Actions 的 `Test and build` 工作流为 Windows x64 和 macOS Apple Silicon 构建独立运行包。构建测试成功后，打开对应运行记录，在 Artifacts 中下载所用平台的文件。包中含 Python 运行时、离线 OCR 模型和依赖，解压后请保留可执行文件与随包目录的相对位置。[GitHub Actions](https://github.com/chaoluo-git/deep-stack/actions/workflows/build.yml)

- Windows x64 下载 `deep-stack-windows-x64`，完整解压 ZIP，在解压目录的 PowerShell 中运行 `.\deep-stack.exe demo`。
- macOS Apple Silicon 下载 `deep-stack-macos-arm64`，解压外层 ZIP 后运行 `tar -xzf deep-stack-macos-arm64.tar.gz`，再运行 `./deep-stack/deep-stack demo`。

Windows 完整解压后若遇到 ONNX Runtime 的 DLL 加载错误，可核对系统是否有最新的 x64 Visual C++ v14 运行库。[ONNX Runtime 运行要求](https://onnxruntime.ai/docs/install/#requirements) · [Microsoft x64 运行库](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)

从源码运行时，推荐 Python 3.12。在项目目录运行 `python -m pip install ".[ocr]"`，随后可使用 `deep-stack` 或 `python3 -m deep_stack`。两个平台的独立包都使用离线 RapidOCR。macOS 源码版可改用 `.[live]` 和系统 Swift/Vision。Windows OCR 和屏幕采集依赖的版本要求 Python 3.11 或更高，纯 EV 分析可用 Python 3.10。

以下命令用安装后的 `deep-stack` 举例。直接解压运行时使用对应平台的可执行文件路径。

```bash
deep-stack --help
deep-stack demo
deep-stack demo --state -o state.json
deep-stack analyze state.json --samples 20000 --seed 42 -o advice.json
```

## 截图和窗口尺寸

1. `deep-stack capture -o observations.json` 做一次屏幕采集。Windows 和独立包尝试自动定位可见的横向绿色牌桌。macOS 源码版弹出原生截图选区，请框选网页内牌桌。系统可能要求授予录屏权限。
2. `deep-stack observe first.png second.png -o observations.json` 读取同一手牌按时间顺序排列的一张或多张截图。已经裁到牌桌的 PNG 或 JPEG 可直接读，带浏览器边框或桌面背景时用 `--auto-viewport` 自动找牌桌，或者用 `--viewport X,Y,W,H` 指定牌桌裁切。单位为截图像素，原点在左上角。
3. `deep-stack draft observations.json -o draft.json` 用最近的观察值和可承接的历史观测生成状态草稿。核对公共牌、自己手牌、座位、底池、筹码和动作顺序，并补齐当前参与者、本轮投入和合法动作。识别不清或漏采的字段可从截图或牌谱补齐。
4. `analyze` 接收确认后的状态文件。比较弃牌、过牌、跟注和指定尺度的进攻动作中输入已经明确的合法候选项。单挑河牌使用精确枚举，其余场景使用蒙特卡洛抽样。`--samples` 可填 `100` 至 `200000`，默认为 `4000`。`--seed` 固定抽样，便于复现。

自动定位每次按可见牌桌重新计算边界。macOS 单窗捕获的内置布局已标定宽窗和分屏窄窗，窗口变窄时按座位锚点移动识别框，牌和数字的框宽仍按高度缩放。浏览器缩放、超出标定区间的窗口或不同主题可能让控件重新排列。收到宽高比提示或发现读框偏离时，可为该布局单独标定。

```bash
deep-stack layout-template -o layout.json
deep-stack observe screenshot.png --auto-viewport --layout layout.json -o observations.json
deep-stack layout-template --live -o live-layout.json
```

`layout.json` 中 `reference_aspect_ratio` 是标定牌桌的像素宽度除以高度。`regions` 中每个框的 `x`、`y`、`width`、`height` 分别按牌桌宽高归一化到 `0..1`。例如牌桌宽 1280 像素，一个识别框左边距为 1120 像素，则它的 `x` 为 `0.875`。保留模板中的 `kind`，将特定框置为 `null` 可停用识别。先用保存的截图核对新标定。画面存在多个相似窗口、自动定位失败或需要固定边界时，可用 `--viewport` 明确裁切。

观察文件使用 `deep-stack-observation-v1` 格式，`frames` 保留输入顺序。每帧的 `seats` 记录座号 `seat`、昵称 `name`、剩余筹码 `stack`、动作标签 `action` 和可见投入 `wager`。`hero_cards` 和 `board` 记录完整的识别结果，`observed_hero_cards` 和 `observed_board_cards` 保留可识别的单张牌。牌角点数由 OCR 读取，并结合花色轮廓识别。置信不足或证据冲突时保留 `null`。连续牌槽和空槽可独立判定街次。`pot` 和 `center_pot` 分别是上、下两行读数。默认布局已按可见画面标定 `hero` 和 `top_left` 的投入位置，`top_right` 的投入位置仍待补齐。

`events` 索引观察到的动作标签，`frame` 从 `1` 开始，`wager_frame` 表示相应金额的来源帧，同一帧内各动作的发生顺序未知。跨街持续显示的弃牌、全下标签会去重。

## 连续观察和实时提示

```bash
deep-stack live --session table-01
deep-stack live --session table-01 --spectate
deep-stack live --layout live-layout.json --model model.json --session table-01 --json
deep-stack live --layout live-layout.json --frames first.png second.png --json
```

`live` 在 macOS 上优先抓取唯一可见的 `WePoker-H5` 窗口，再在窗内定位牌桌，终端放在前面也不会成为截图的一部分。请保持 WPK 标签选中。需要固定窗体时可传 `--window-id ID`，`--viewport` 可直接指定捕获图像中的像素区域。屏幕捕获模式需要牌桌在可见屏幕中完整显示。用 `Ctrl+C` 停止。`--interval` 是采样间隔，`--max-age` 是可输出建议的最大帧龄，两者单位为秒。指定 `--frames` 时顺序回放本地图像。`--json` 按行输出事件，适合留档或接入本机界面。

内置布局识别 8 座和 9 座。切换布局时保留可见空座的位置。需要指定布局时可加 `--table-seats 8` 或 `--table-seats 9`。默认区分观战、已入座等待下一手、留座和已弃牌。专门观战时可以加 `--spectate`，底部玩家显示为 `bottom_center`。准备入座时，用普通 `live` 命令重新启动即可进入建议模式。

花色识别兼容原有牌面和新版四色大图标牌面。新版牌面用绿色梅花、蓝色方片，同时核对图标的颜色与轮廓。可以直接在 WPK 切换样式，命令参数相同。

终端逐街输出公共牌，例如 `公共牌｜翻牌 A♠ 7♦ K♣`。暂未读到的牌槽显示 `?`，补读成功后更新。`--json` 的 `board` 事件保留原始牌槽顺序、街次及识别完整度。

桌况分行显示盲注、ante、牌局位置、空座、等待/留座及筹码。物理位置始终用「上中」「右下」等标注，动作同时显示牌局位置，例如 `翻前 HJ · 右下 跟注`。位置以庄标 `D` 和本手参与座次为基准，跳过明确空座和暂不参与的玩家。弃牌玩家仍保留本手位置。能确定完整座次时按实际人数映射 `BTN`、`SB`、`BB`、`UTG`、`LJ`、`HJ`、`CO` 等，单挑用 `BTN/SB`。只读到部分位置时会明确标出。`--json` 的 `table` 事件保留 `position_by_seat` 和 `seat_statuses`，`action` 事件带物理座位与 `position`。`--quiet-actions` 可隐藏桌况与动作，仍显示公共牌。

引擎需从倒计时确认轮到自己，并核对牌面、底池、活跃座位及相应动作的筹码信息，读数不足或过期时输出等待原因。会话中的已全下玩家和待确认边池需要转到状态文件复盘。默认可比较读数充分的过牌、跟注和弃牌。进攻动作还要求金额和对手应答模型。观战或等待发牌期间，会继续记录同一手中已绑定玩家的可见动作。

`--model` 接收可选的 JSON 对象。使用内置 WPK 布局且省略 `--model` 时，默认采用已观察到的两行底池口径，并在每次识别时验算金额。指定自定义布局时，可在模型中明确写入需要的口径。

| 字段 | 含义 |
| --- | --- |
| `empty_seats` | 已核对的空座位 ID 列表，自身座位 `hero` 应省略。 |
| `pot_includes_wagers` | 仅在已核对顶部底池包含本街可见投入时填 `true`。引擎还需验算两行底池的差额。 |
| `ranges` | 座位到对手范围的对象，例如 `{"right_upper":"random"}`。可使用下面分析状态的加权范围格式。 |
| `aggressive_amount` | 下注或加注按钮的金额口径。显示改动后的本街总投入时填 `"street_total"`，显示此次追加金额时填 `"additional"`。 |
| `responses` | 从动作到各对手座位应答概率的对象，例如 `{"bet":{"right_upper":0.40}}`。有多个待应答座位时，应逐席核对。 |

对手范围、反应率和结算规则直接影响建议。缺少完整牌谱时，采样可能漏掉两帧之间的动作。当前识别回归采用合成画面，真实 WPK 牌桌的持续识别准确率仍待验证。

## 按稳定玩家 ID 记录行为

启动 `live` 后，在网页打开玩家头像并保持弹窗，直到终端报告绑定。程序连续两帧核对 ID，结合昵称、头像和打开弹窗前的画面确认座位。座位证据冲突时，可以用 `bind` 手动确认。也可以截取弹窗内含 ID 标识的整行，用 `read-id` 辅助读取。玩家 ID 按字符串保存，保留前导零。

观战可选 `deep-stack live --session table-01 --spectate --scan-avatars`。前台需保持可见的 WPK 窗口。macOS 需授予终端或启动程序的应用「辅助功能」权限。Windows 使用 Chrome 或 Edge，点击时会移动系统鼠标指针。程序逐个打开需要确认身份的头像，核对成功后关掉它自己打开的资料弹窗。

```bash
deep-stack players read-id profile-id.png
deep-stack players --db players.sqlite3 bind table-01 right_upper 001234 --name 示例昵称
deep-stack players --db players.sqlite3 bindings table-01
deep-stack live --db players.sqlite3 --session table-01 --layout live-layout.json --json
deep-stack players --db players.sqlite3 show 001234 --events 20 --profiles 5
deep-stack players --db players.sqlite3 unbind table-01 right_upper
```

实际屏幕连续观察默认使用当前目录的 `players.sqlite3`，可用 `--db FILE` 指定本地数据库，或用 `--no-player-log` 关闭记录。文件回放默认只输出观察结果，显式给 `--db` 才会写入。不同牌桌请使用不同的 `--session`，绑定时用同一个场次值。

动作按场次、手牌、街次、座位和稳定 ID 存储。同一动作标签持续出现时去重，同街观察到不同金额的加注可分别记录。检测到座位空出、头像明显变化或昵称冲突时，暂停向原 ID 追加。再次核对弹窗可以继续原玩家记录或登记新玩家。刚入座等待发牌或暂时留座的玩家仍保留已有 ID。同名玩家保持独立。

`show` 输出含可见动作的手牌数、按街动作数及本街可见投入的最小值、最大值和均值，金额是采样到的累计投入读数。`--events N` 附带最近 N 条可见动作。俱乐部弹窗的手数、胜率、入池率和摊牌胜率完整读到后，单独保存为画像，`--profiles N` 可查询最近的变化。VPIP 和 PFR 需要已确认的完整翻前分母，缺少该分母时返回 `null`。SQLite 文件保存在本机，实时模式处理完画面后丢弃图像。

## 分析状态 JSON

下例是一个本地构造的单挑河牌场景。`pot` 是行动前桌上的全部底池，包含此前投入的筹码。`stack` 是各座位在决策前的剩余筹码，`wager` 是本轮已投入。`range: "random"` 表示对手在未被已知牌阻断的手牌组合中等权。有牌谱依据时也可传入加权的具体组合，例如 `[{"cards":["9h","9d"],"weight":3}]`。`amount` 表示从当前筹码中额外投入的数量。

```json
{
  "hero_cards": ["As", "Ks"],
  "board": ["Ts", "Js", "Qs", "2c", "7d"],
  "pot": 150,
  "seats": [
    {"seat": "hero", "stack": 400, "in_hand": true, "wager": 0},
    {"seat": "villain", "stack": 350, "in_hand": true, "wager": 50, "range": "random"}
  ],
  "actions": [
    {"kind": "fold", "amount": 0},
    {"kind": "call", "amount": 50}
  ]
}
```

- 自己的座号写作 `hero`，其他座号应唯一。牌用牌面与小写花色表示，例如 `As` 是黑桃 A，`Td` 是方片 10。公共牌长度分别为 `0`、`3`、`4`、`5`。同一张牌的重复输入和互相冲突的状态须在分析前更正。
- 合法动作及尺度由用户核对，特别是最低加注额、行动顺序以及短码全下后是否重新开放加注。动作类型为 `fold`、`check`、`call`、`bet`、`raise`、`all_in`。
- 主动下注、加注或提高本街下注的全下需要给出仍有筹码且需应答的座位的跟注概率，例如 `responses: {"villain": 0.6}`。每个座位也可使用 `{"default":0.2,"by_hand":[{"cards":["Qs","Qd"],"probability":0.9}]}` 按对手手牌组合覆盖默认概率。给定各座位底牌后，继续决策按座位独立。继续方跟注后直接发牌摊牌。模型尚未覆盖对手再加注、后续街下注、抽水与保险。
- 进攻动作、全下及有多个对手时的跟注需要提供每个座位的 `wager`。已有对手全下且要比较继续动作时，需要所有座位的 `wager` 和 `committed`。有短码跟注或边池时，应记录每个座位的整手投入 `committed`，包括已经弃牌的玩家正常投入。可另设 `dead_money` 记录由所有活跃玩家争夺的主池死注，例如大盲前注。填写 `committed` 时，`pot` 等于所有 `committed` 与 `dead_money` 之和。任一座位填写 `wager` 或 `committed`，其余座位也应填写对应字段。
- 比较的是从决策点开始的净筹码增量。以前投入的筹码已在底池中，弃牌的增量 EV 为 `0`。建议只在输入列出的备选动作中寻找最高 EV，前提是玩家确实能按输入尺度行动。未知对手范围或抽样误差可能使接近的动作互换顺序。

分析输出在 `actions` 中列出各动作的 `ev`、标准误差 `std_error` 和约 95% 抽样区间 `ci95`，并返回推荐动作 `recommendation`、前两名 EV 差 `ev_gap` 和区间是否重叠 `intervals_overlap`。区间反映给定模型内的抽样误差。上例会精确枚举 990 种对手组合，弃牌 EV 为 `0`，跟注 50 的 EV 为 `+150`。`demo` 用四种等权对手组合，跟注 20 的 EV 为 `+70`。

## 验收标准

| 环节 | 可复现的检查 |
| --- | --- |
| 牌桌观察 | 对固定截图识别座位、剩余筹码、可读的牌和下注动作。对遮挡或无法确认的字段保留缺失。 |
| 窗口尺寸 | 不同像素尺寸和标题栏偏移的合成图能定位同一类牌桌，另存布局可复用。 |
| 时序 | 同一动作标签在相邻帧持续时不重复计数。筹码变动或新动作应由观测差异支撑。 |
| 实时提示 | 回放中牌局条件满足后输出候选 EV。行动轮次或关键筹码不确定时给出等待原因。 |
| 玩家记录 | 前导零保留，同名玩家按不同 ID 隔离。昵称变化暂停归属，数据库重开后仍能去重。 |
| 状态草稿 | 复制有依据的观察值。需要人工核对的字段保持待确认，分析器能拒绝关键信息不足或矛盾的状态。 |
| EV | 单挑河牌可以手算对照。相同蒙特卡洛输入和随机种子可以复现。平局、短码和边池按各池参与资格分配。 |
| 输出 | 能看到备选动作的 EV、推荐动作、计算方法，以及适用的模型假设与误差。 |

```bash
python3 -m unittest discover -s tests
```

七张牌的胜负和平局结果用 PH Evaluator 0.5.3.1 对照了 25,000 组固定种子样本，又用 PokerKit 0.7.8 对照了 20,000 个对局，两组均全部一致。对照库只用于验证。[PH Evaluator 0.5.3.1](https://pypi.org/project/phevaluator/0.5.3.1/) · [PokerKit 0.7.8](https://pypi.org/project/pokerkit/0.7.8/)

## 理论与开源参考

- CFR 是不完全信息扩展式博弈的求解方法。DeepStack 将持续重求解和学习得到的叶节点评估用于双人无限注。Pluribus 的六人结果是实证表现。首版的候选动作 EV 使用范围与响应假设，完整策略求解还需要行动树、策略迭代及独立基准测试。[CFR 论文](https://proceedings.neurips.cc/paper_files/paper/2007/hash/08d98638c6fcd194a4b1e6992063e944-Abstract.html) · [DeepStack 论文](https://doi.org/10.1126/science.aam6960) · [Pluribus 论文](https://noambrown.com/papers/19-Science-Superhuman.pdf)
- OpenSpiel（Apache-2.0）适用于用 Kuhn、Leduc 等小型牌局验证 CFR，并用 ACPC 德州扑克环境验证状态转移。PokerKit（MIT）适用于规则、牌谱和结算的交叉验证。PH Evaluator（Apache-2.0）可以独立对照七张牌牌力。TexasSolver（AGPL-3.0）可用于双人翻后抽象树的结果对照。[OpenSpiel](https://github.com/google-deepmind/open_spiel) · [PokerKit](https://github.com/uoftcprg/pokerkit) · [PH Evaluator](https://github.com/HenryRLee/PokerHandEvaluator) · [TexasSolver](https://github.com/bupticybee/TexasSolver)
- DeepStack-Leduc 是作者团队发布的教学实现，可在简化的双人 Leduc 博弈中观察持续重求解与价值网络。由于牌组和决策树规模不同，它适合验证算法结构。[DeepStack-Leduc](https://github.com/lifrordi/DeepStack-Leduc)
