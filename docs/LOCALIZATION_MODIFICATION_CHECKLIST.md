# WeChatDataAnalysis（Windows 内网自用版）去限制改造清单 & 付费功能自研备忘录

> **适用前提**
> - **目标平台**：仅限 **Windows（x64）**，不考虑 macOS / Linux 兼容。
> - **部署场景**：仅在个人/内网环境使用，不做过度复杂的鉴权改造。
> - **核心目标**：
>   1. 摸清并剥离原开发者用于**限制用户**的机制与**必须连接其外部 API** 的代码，让项目控制权完全归自己所有；
>   2. 彻底删掉不会付费的推广弹窗、兑换码套餐与无后端实现的占位 UI；
>   3. 完整记录原项目所有付费功能（Windows 端 122 项 + WxCDN），按自研路线归类，方便后续自己按需开发（如定时本地归档朋友圈、自动监控解密保存新收到的图片等）。

---

## 一、原开发者用于「限制用户」的机制清单（Windows 端）

| 序号 | 限制机制 | 所在位置（文件与行号） | 限制效果 | 控制层归属 & 剥离方向 |
| --- | --- | --- | --- | --- |
| 1 | **45 天硬过期窗口** | `src/wechat_decrypt_tool/native_core_client.py` (L60, L1214-L1233)<br>`desktop/src/native-core-runtime.cjs` (L13, L67-L82)<br>`desktop/resources/native-core-source-windows.json` (L15 `expiresAtUnix`) | 每个闭源 `native_core` 构建硬编码 45 天生命周期，到期后即使离线也拒绝加载（`BUILD_MISMATCH`），迫使用户不断升级原开发者的新版二进制。 | **Python/JS 层 + 闭源 DLL/EXE 内部均有校验**。彻底脱离限制的唯一方式是解耦闭源 `native_core`，改用本机已解密 SQLite（`decrypted` 模式）。 |
| 2 | **在线租约校验 + 4xx 强杀进程** | `src/wechat_decrypt_tool/native_core_lease.py` (L47, L55, L250-L296, L682-L765)<br>`src/wechat_decrypt_tool/native_core_device_credential.py` (L35-L36, L234-L237) | 每 1 小时向 `license.fqyw.love` 发送设备指纹（32 字节 `deviceId`）与签名证明；若服务端返回 4xx 拒绝，会删除 Windows DPAPI 凭证并**强制终止** `wechatdb_broker.exe`。 | **服务端下发签名租约 + 闭源 broker 验签**。不依赖 `native_core` 后可整体移除。 |
| 3 | **功能位门控（Feature Bits）** | `src/wechat_decrypt_tool/native_core_client.py` (L80-L82, L110, L145-L154, L750-L756)<br>`src/wechat_decrypt_tool/wcdb_realtime.py` (L79-L80)<br>`src/wechat_decrypt_tool/native_core_voice_asr.py` (L236-L367) | 强制 `NativeCoreMode.REQUIRED` 无回退；用权限位控制 `DATABASE_READ`（实时读库）、`EXPORT`（导出封签）、`NATIVE_ASR`（微信原生语音转文字，且锁死微信 `4.1.12.26`）。 | 实时读库改为默认 `decrypted`（或自行基于 `wechat_decrypt.py` 做定时增量解密）；语音转文字直接用仓库内置的开源 Whisper/Qwen/SenseVoice（`voice_transcription.py`）。 |
| 4 | **导出封签绑定闭源 `wce_integrity.pyd`** | `src/wechat_decrypt_tool/export_integrity.py` (L17, L23-L95, L231-L265, L289-L357)<br>`src/wechat_decrypt_tool/native/wce_integrity.pyd`<br>`src/wechat_decrypt_tool/native_core_export.py` (L104-L150) | 所有 10 类导出（聊天/朋友圈/联系人/收藏/归档 ZIP 等）强制调用闭源 `wce_integrity.pyd` 生成 HTML 样式与签名，并调用 `native_core` 生成 `.wes` 封签与 `.wec` 加密壳。 | **完全在 Python 层可替换**：重写 `export_integrity.py`，用纯 Python 提供 HTML/CSS 模板和标准 `zipfile` 导出，彻底删除 `wce_integrity.pyd` 与 `native_core_export` 调用。 |
| 5 | **WxCDN 流量配额限制** | `src/wechat_decrypt_tool/cdn_image_service.py` (L1-L1156)<br>`frontend/lib/wxcdn-card/format.js` (L10-L16) | 免费版仅 50 MiB/天，超额返回 `429 quota_exceeded`，需向 `@knchudj` 购买 20 位兑换码升级 Plus/Pro/Ultra。 | **服务端计费，直接整套删除**（见第三部分）。 |
| 6 | **PRO 功能前端锁死（空壳入口）** | `frontend/pages/ai.vue`、`ConversationPane.vue`、`ChatOverlays.vue`、`sns.vue`、`SettingsDialog.vue` | 点击 AI 助手、添加/发送/编辑消息、发布朋友圈等按钮全部拦截并弹出 `AdvancedFeaturesDialog.vue` 引导去 Telegram 买断。 | 这些功能在本仓库**无后端实现**，直接删掉弹窗与占位入口，需要的功能按第四部分自己开发。 |

---

## 二、必须调用原开发者外部 API / 服务器的内容清单（Windows 端）

以下所有外部服务均属于原开发者（或其关联域名），**建议全部切断或移除**：

| 序号 | 外部域名 / API 地址 | 调用文件与行号 | 发送了什么数据 | 触发条件 & 处理方式 |
| --- | --- | --- | --- | --- |
| 1 | `https://license.fqyw.love/v1/challenges`<br>`https://license.fqyw.love/v1/leases` | `src/wechat_decrypt_tool/native_core_lease.py` (L47, L380-L390, L682-L765) | `deviceId`、`buildId`、`appId`、代码签名证书 SHA-256、签名挑战证明 | 使用闭源 `native_core` 时每小时心跳一次。**处理**：解耦 `native_core` 后整体删除该模块。 |
| 2 | `https://license.fqyw.love/v1/events` | `src/wechat_decrypt_tool/native_core_telemetry.py` (L17-L39, L79-L179)<br>`src/wechat_decrypt_tool/api.py` (L83-L125) | `deviceCredential` + 操作事件（`app_open`、`database_open`、`conversation_list`、`message_page`、`search`、`export_*`）与时间戳 | 每次打开会话/搜索/导出自动上报，无 UI 开关。**处理**：直接删除 `api.py` L83-L125 的中间件及 `native_core_telemetry.py`。 |
| 3 | `https://view.free.c3o.re/api/key` | `src/wechat_decrypt_tool/key_service.py` (L1314-L1320, L1428-L1517) | **`wxid` 目录名 + 本地 `global_config` + `global_config.crc` 原始文件** | **注意：会自动静默触发！** 本地图片密钥 V2 验真未命中时自动回退上传。**处理**：立即删除 L1314-L1335 回退调用与 `fetch_and_save_remote_keys()`。 |
| 4 | `https://wxcdn.c3o.re`<br>(`/token`, `/quota`, `/redeem`, `/download`) | `src/wechat_decrypt_tool/cdn_image_service.py` (L39-L43, L611-L627, L1029-L1066)<br>`src/wechat_decrypt_tool/routers/cdn.py`<br>`src/wechat_decrypt_tool/routers/chat_media.py` (L2690-L2766) | `wxid`、`global_config`、`global_config.crc`、`fileid`；**本地 AES 解密失败时还会把单图 `key`（AES 密钥）发给服务端代解** | 默认关闭，手动开启后触发。**处理**：整套删除前后端 WxCDN 代码。 |
| 5 | `https://dl.fqyw.love/desktop-updates/...` | `desktop/src/main.cjs` (L1586-L1590) | 手动点击更新时浏览器打开外部下载链接 | 桌面端更新引导。**处理**：删除相关 URL 与更新逻辑。 |
| 6 | `https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/...` | `desktop/package.json` (L142-L147)<br>`desktop/src/windows-source-native-core-bootstrap.cjs` (L22-L23, L433-L466) | 检查原仓库 Release 更新 + 启动时下载闭源 `wechatdb_broker.exe`/`wechatdb_client.dll` | 桌面端启动/更新时触发。**处理**：禁用 `electron-updater`，删除 `windows-source-native-core-bootstrap.cjs` 调用。 |
| 7 | `https://wuuud.com/`<br>`https://t.me/knchudj`<br>`https://t.me/+IQKBDeXtaKozZWYx` | `frontend/components/SidebarRail.vue` (L530)<br>`frontend/lib/developer-support.js` (L4-L5) | 仅外部跳转链接 | 付费推广入口。**处理**：删除链接与按钮。 |

> **附注（非原开发者域名，按需保留）**：
> - 腾讯图片 CDN（`qpic.cn`、`qq.com`）：加载头像/朋友圈/公众号外链图，可保留。
> - Hugging Face / HF-Mirror（`huggingface.co`、`hf-mirror.com`）与 PyTorch 源：仅在手动下载本地 Whisper/Qwen 语音模型时使用，可保留。
> - 高德地图瓦片（`webrd01.is.autonavi.com`，`ChatLocationCard.vue` L107）：渲染位置卡片用，内网自用可保留或按需移除。
> - **顺手安全小修（2 行代码，不影响内网访问）**：`src/wechat_decrypt_tool/routers/biz.py` (L338-L352) 的 `proxy_biz_image` 直接 `urllib.request.urlopen(url)`，建议顺手加一句 `if urlparse(url).scheme not in {"http", "https"}: return Response(status_code=400)`，防止误传入 `file://` 读取本机文件。

---

## 三、待删除的无用付费内容与 Windows 端去限制修改清单

### 3.1 删除付费推广 UI 与无后端占位入口（前端）
- [x] 删除 PRO 演示与推广组件：
  - `frontend/components/AdvancedFeaturesDialog.vue`
  - `frontend/lib/pro-demos/`（整目录，123 项演示动画）
  - `frontend/lib/developer-support.js`（TG 买断链接）
  - 全局清理 `openAdvancedFeaturesDialog` / `openFeatureUnavailableDialog` 引用及 `frontend/app.vue` 中的挂载
- [x] 删除侧边栏 Pro 按钮与跳转：`frontend/components/SidebarRail.vue` (L94-L105, L530-L531)
- [x] 删除 WxCDN 套餐与兑换码 UI：
  - `frontend/components/PlanWindow.vue`
  - `frontend/stores/cdnPlan.js`
  - `frontend/lib/wxcdn-card/`（整目录）
  - `frontend/components/SettingsDialog.vue` (L579-L616，WxCDN 设置区块)
  - `frontend/app.vue` (L22 `PlanWindow` 引用)
- [x] 清理无后端实现的占位入口（后续自己开发哪个再重新加回）：
  - `frontend/pages/ai.vue` + `SidebarRail.vue` (L51-L61) + `SettingsDialog.vue` (L28, L255-L261, L702, L1048)
  - `frontend/components/chat/ConversationPane.vue` (L38-L48 “添加消息”按钮、L156-L165 “发送”输入框)
  - `frontend/components/chat/ChatOverlays.vue` (L993-L1002 “修改文字”) + `frontend/composables/chat/useChatEditing.js` (L24, L217-L222)
  - `frontend/pages/sns.vue` (L44-L50, L1187 “发布朋友圈”按钮)
- [ ] （可选）删除官网宣传目录 `website/`

### 3.2 切断外部上传、遥测与 WxCDN（后端）
- [x] `src/wechat_decrypt_tool/key_service.py`：删掉 L1314-L1335 远程回退调用和 L1428-L1517 `fetch_and_save_remote_keys()`，确保不再上传 `global_config` 到 `view.free.c3o.re`。
- [x] `src/wechat_decrypt_tool/api.py`：删掉 L83-L125 的 `_record_content_free_product_events` 遥测中间件，注销 `cdn` 路由。
- [x] 删除 WxCDN 后端代码：
  - `src/wechat_decrypt_tool/cdn_image_service.py`
  - `src/wechat_decrypt_tool/routers/cdn.py`
  - `src/wechat_decrypt_tool/routers/system.py` (L82-L115 CDN 配置读写)
  - `src/wechat_decrypt_tool/routers/chat_media.py` (L2690-L2766 CDN 远程拉图回退)

### 3.3 解除 Windows 端 45 天硬过期与闭源组件强绑定（核心控制权回收）
- [x] **导出纯 Python 化（移除 `wce_integrity.pyd` 与 `native_core_export`）**：
  - 重写 `src/wechat_decrypt_tool/export_integrity.py`，用纯 Python CSS/HTML 模板 + 标准 `zipfile` 替代 `wce_integrity.pyd` 和 `seal_export_manifest()`。
  - 移除 `chat_export_service.py` (L3636, L3695)、`sns_export_service.py` (L3536, L3559)、`routers/record_export.py` (L773, L910)、`routers/chat_contacts.py` (L2889, L3011)、`routers/account_archive_export.py` (L451, L458) 中的 `.wec` 闭源加密壳调用（直接导出标准 ZIP 即可）。
  - 删除 `src/wechat_decrypt_tool/native/wce_integrity.pyd` 与 `tools/build_wce_integrity.ps1`。
- [x] **数据源默认走已解密 SQLite（摆脱 `wechatdb_broker.exe` 与 45 天过期）**：
  - 修改 `src/wechat_decrypt_tool/source_fallback.py` 与 `account_source_policy.py`，默认使用 `decrypted`（由仓库内开源的 `wechat_decrypt.py` 解密出的本地 SQLite，永久有效、可任意读写扩展）。
  - 移除 `native_core_client.py`、`native_core_lease.py`、`native_core_telemetry.py`、`native_core_device_credential.py`、`native_core_broker.py`、`native_core_database.py`、`native_core_realtime.py`、`native_core_export.py`、`native_core_voice_asr.py`、`wcdb_realtime.py` 的强依赖。
- [x] **Windows 桌面端去闭源引导**：
  - 在 `desktop/src/main.cjs` 中移除 `windows-source-native-core-bootstrap.cjs`、`native-core-runtime.cjs`、`windows-private-pki-runtime.cjs` 调用，并关闭 `electron-updater` 自动更新与 `dl.fqyw.love` 链接。
  - 语音转文字保留并默认使用开源的 `voice_transcription.py`（Whisper / Qwen / SenseVoice），移除仅限微信 `4.1.12.26` 的 `native_voice_transcription.py` 入口。
  - Windows 密钥提取继续使用现有的 `dll_key_scan.py`、`image_key_memory_scan.py` 和 `tools/key_wheels/wx_key-*.whl`（开源项目 `H3CoF6/py_wx_key`）。

---

## 四、原项目付费功能完整存档（Windows 端 122 项 + WxCDN，供后续自行开发）

以下完整记录了原项目在 `website/assets/js/data.js` 与 `frontend/lib/pro-demos/catalog.js` 中列出的全部 Windows 相关付费功能（共 13 个分组 122 项，已剔除第 123 项 macOS 专属取密钥），并按**个人后续自研路线**标注了实现思路与难度，便于你挑高价值功能逐个自己实现。

### 路线 A：纯本地数据层即可自行实现（推荐优先自研，无微信风控风险）

> 基于你本机已解密的 SQLite（`wechat_decrypt.py`）+ 微信本地磁盘文件监控（`FileStorage` / `msg/attach`）+ 本地脚本/定时任务即可落地。

#### 1. 自动下载 / 本地归档媒体（对应原 PRO #1-#15 + WxCDN）
> **自研思路**：
> - **方案 A1（纯本地磁盘实时监听归档）**：Windows 后台跑一个定时/文件监听脚本（如 Python `watchdog` 监控微信数据目录的 `msg/attach`、图片/视频/语音/文件目录），一旦微信收到新消息并在本地落盘 `.dat`/语音/视频，立刻调用本项目已有的图片密钥（XOR+AES）解密并拷贝到你的永久归档目录，防止微信清理缓存或对方撤回。
> - **说明**：如果某张原图微信 PC 端根本没点开下载、本地连缓存都没有，纯读磁盘拿不到原图；要实现“无人值守自动拉取未点击的原图”，需要配合 Windows UI 自动化（`uiautomation` 模拟点击）或接入微信协议层。

| # | 原付费功能名 | 原功能说明 | 自研可行性说明 |
| --- | --- | --- | --- |
| 1 | 聊天原图 | 接收聊天图片时自动保存可用原图 | 监控本地新 `.dat` 实时解密归档；未下载原图需客户端触发或协议拉取 |
| 2 | 聊天语音 | 接收语音消息时自动保存语音资源 | 可直接定时扫描解密库语音表 + SILK 转 MP3/WAV 归档 |
| 3 | 聊天视频 | 接收视频时自动保存视频和封面（>64MB 按需） | 监控本地视频目录自动拷贝归档 |
| 4 | 实况照片播放 | 聊天/朋友圈悬停播放实况视频 | 解析消息中的实况视频字段并在前端 `<video>` 悬停播放 |
| 5 | 聊天文件 | 接收文件消息时自动保存附件（>64MB 按需） | 监控微信本地文件接收目录自动归档（微信设置开启自动下载） |
| 6 | 聊天表情与 GIF | 自动保存可下载的表情/GIF | 从消息 XML 提取 `cdnurl` 定时下载到本地（腾讯公开 URL，无需 WxCDN） |
| 7 | 名片头像 | 自动保存联系人名片头像 | 从名片 XML 提取头像 URL 定时下载到本地 |
| 8 | 消息卡片封面 | 自动保存链接/公众号/小程序/视频号卡片封面 | 从消息 XML 提取封面 URL/本地缩略图定时归档 |
| 9 | 聊天记录与笔记附件 | 自动保存合并转发/笔记中的嵌套图片/语音/视频/文件 | 解析合并转发 XML 递归提取并归档本地已有附件 |
| 10 | 朋友圈图片与封面 | 浏览朋友圈时保存图片、评论表情、视频缩略图和封面 | **高推荐自研**：定时增量解密 `sns.db`，解析新动态 XML 中的图片/视频 URL（朋友圈图片大多可通过现有 `sns_media.py` ISAAC64 解密或直接下载），自动落盘永久保存（解决“三天可见”失效问题） |
| 11 | 收藏图片、GIF 与封面 | 浏览收藏时加载并保存图片、GIF、卡片封面 | 定时解析本地 `favorite.db` 并归档对应媒体文件 |
| 12 | 整条收藏附件 | 主动下载整条收藏的全部附件 | 同上，按收藏条目打包导出 |
| 13 | 朋友圈导出媒体 | 导出朋友圈 HTML 时自动补全原图、视频、封面 | 结合 #10 的本地归档库，导出 HTML 时直接引用已落盘媒体 |
| 14 | 公众号文章配图 | 下载公众号文章时自动抓取正文配图写入 HTML | 独立爬虫即可实现：请求公众号文章 URL，抓取 `mmbiz.qpic.cn` 配图替换本地路径 |
| 15 | AI 分析附件 | AI 分析时按需补全图片与文件 | 结合本地归档目录直接喂给多模态模型 |

#### 2. AI 与 MCP（对应原 PRO #41-#42）
| # | 原付费功能名 | 原功能说明 | 自研可行性说明 |
| --- | --- | --- | --- |
| 41 | AI 助手 | 围绕聊天记录提问、总结重点，并带消息出处引用 | **高推荐自研**：复用本项目已有的 `chat_search_index.py`（FTS 全文检索）做 RAG 召回，后端直接接本地 Ollama 或自己的 OpenAI/DeepSeek/Gemini API |
| 42 | MCP 接入 | 让支持 MCP 的 AI 客户端按需查询聊天数据 | **高推荐自研**：用 Python `fastmcp` 写一个 100 行以内的 MCP Server，直接只读查询本地已解密 SQLite 即可 |

#### 3. 本地数据监控与归档类（对应原 PRO #69, #101, #119）
| # | 原付费功能名 | 原功能说明 | 自研可行性说明 |
| --- | --- | --- | --- |
| 69 | 朋友圈连续同步 | 刷新最新动态并沿时间线读取历史页 | **高推荐自研**：定时任务（如每 15 分钟）增量解密 `sns.db` 并将新动态 + 评论 + 媒体写入你自己的永久存档库（即使对方后来设为“三天可见”或删除，本地归档依然保留） |
| 101 | 联系人变化记录 | 按周期对比本地联系人快照：新增、资料变化、不在列表 | **高推荐自研**：定时导出 `contact.db` 快照做 diff，记录昵称/头像/签名变化与列表变动 |
| 119 | 二维码与小程序码识别 | 识别图片中的二维码或小程序码内容 | 纯本地 Python 库（`pyzbar` / `opencv-python`）即可实现 |

#### 4. 离线副本消息修改与补录（对应原 PRO #16-#40）
> 说明：原 PRO 是直接改微信真实库；如果是为了**导出前脱敏/整理/补全历史记录**，直接操作你本机已解密的 SQLite 副本非常简单且零风险（SQL `UPDATE` / `INSERT` / `DELETE` 即可）。

- **消息修改（#16-#23）**：
  - `#16` 修改文字消息、`#17` 编辑消息 XML 源码、`#18` 修改时间、`#19` 底层字段编辑（`local_id`/`type`/`status`）、`#20` 恢复原消息、`#21` 修复为“我发送”、`#22` 反转气泡位置、`#23` 删除系统/撤回提示消息。
- **消息补录（#24-#40）**：
  - `#24` 文字、`#25` 图片、`#26` 文件、`#27` 语音、`#28` 视频、`#29` 表情、`#30` 转账记录、`#31` 红包记录、`#32` 位置、`#33` 链接卡片、`#34` 小程序卡片、`#35` 视频号卡片、`#36` 引用消息、`#37` 合并聊天记录、`#38` 通话记录、`#39` 系统消息、`#40` 拍一拍记录。

---

### 路线 B：涉及微信主动操作 / 私有协议 / 客户端 Hook（开发门槛高，且存在账号风控风险）

> 以下功能无法仅靠读取本地数据库完成，需要向微信服务器发包或注入/操控 Windows 微信客户端。列在此处做完整存档，供评估是否值得自研（若仅做个人数据备份与分析，通常不需要这些功能）：

- **微信主动收发与会话操控（#43-#64）**：
  - `#43` 发送文字、`#44` 群聊 `@`、`#45` 发送图片、`#46` 发送视频、`#47` 发送表情、`#48` 发送文件、`#49` 发送链接卡片、`#50` 发送 MP3 语音、`#51` 发送拍一拍、`#52` 标记会话已读/未读、`#53` 会话免打扰开关、`#54` 发送位置、`#55` 发送名片、`#56` 引用回复、`#57` 收藏表情浏览与发送、`#58` 登录后分页补拉历史消息与实时接收、`#59` 读取个人二维码、`#60` 撤回消息、`#61` 图片视频分组发送、`#62` 发送公众号文章卡片、`#63` 发送视频号卡片、`#64` 折叠/取消折叠聊天。
- **红包与转账（#65-#68）**：
  - `#65` 领取红包、`#66` 转账收款、`#67` 退还转账、`#68` 读取群红包领取明细。
- **朋友圈主动互动（#70-#76）**：
  - `#70` 朋友圈点赞、`#71` 图片评论、`#72` 文字评论、`#73` 发布图文/视频朋友圈、`#74` 删除自己动态、`#75` 删除评论、`#76` 回复评论。
- **群聊管理（#77-#90）**：
  - `#77` 修改本人群昵称、`#78` 显示群成员昵称开关、`#79` 保存到通讯录开关、`#80` 发布群公告、`#81` 新建群聊、`#82` 修改群名称、`#83` 拉好友进群、`#84` 邀请进群、`#85` 移除群成员、`#86` 退出群聊、`#87` 群成员批量加好友、`#88` 修改群备注、`#89` 读取群二维码、`#90` 发起群接龙。
- **联系人操作（#91-#100）**：
  - `#91` 修改好友备注、`#92` 同意好友请求、`#93` 删除好友、`#94` 添加好友、`#95-#99` 联系人标签增删改查、`#100` 手机号/微信号找人。
- **微信生态在线搜索与内容抓取（#102-#110）**：
  - `#102` 搜索公众号文章、`#103` 搜索公众号、`#104` 搜索视频号账号、`#105` 搜索视频号内容、`#106` 搜索视频号直播、`#107` 搜索小程序、`#108` 搜索服务号、`#109` 公众号主页历史文章批量下载、`#110` 微信链接解析。
- **视频号与直播（#111-#114）**：
  - `#111` 作品与评论浏览、`#112` 我关注的视频号、`#113` 关注/点赞/评论、`#114` 直播间互动。
- **小程序交互（#115-#118）**：
  - `#115` 发送小程序卡片、`#116` 小程序登录授权、`#117` 小程序数据请求、`#118` 小程序链接解析。
- **自动化营销任务（#120-#122）**：
  - `#120` 定时群发任务、`#121` 新好友自动备注/标签/欢迎语、`#122` 朋友圈关键词自动点赞/评论/跟圈。

---

## 五、推荐实操顺序（Windows 内网自用）

1. **第一步（马上切断外传）**：执行 **3.2**（删 `view.free.c3o.re` 自动上传、`license.fqyw.love` 遥测、WxCDN 前后端）+ 顺手修 `biz.py` 协议校验。
2. **第二步（清理界面噪音）**：执行 **3.1**（删所有 PRO 弹窗、TG 链接、无后端占位按钮）。
3. **第三步（拿回永久控制权）**：执行 **3.3**（重写 `export_integrity.py` 摆脱 `wce_integrity.pyd`、默认走 `decrypted` 模式摆脱 45 天硬过期的 `native_core`、清理 Windows 桌面端自动下载与私有证书逻辑）。
4. **第四步（按需自研高价值功能）**：优先从 **路线 A** 入手——① 定时增量解密 + 朋友圈永久归档（#10/#69），② 微信媒体目录监听 + 自动解密保存新收图片/视频/文件（#1-#9），③ 本地 AI/MCP 接入（#41-#42）。
