# WeChatDataAnalysis 本地化与去商业化自用修改清单

本文档面向将本仓库改造为**纯本地运行、无外连隐私上传、无付费推广/订阅限制、无闭源二进制 45 天硬过期约束**的个人自用版本，按优先级整理了完整的改造清单、涉及文件与行号、改造方案及验收项。

> **改造原则说明**
> 1. **彻底本地化 & 开源替代**：对于闭源组件（`native_core`、`wce_integrity.pyd`、`wda_xkey_helper`），不采用任何破解/绕过授权的方式，而是**解耦并替换为纯开源本地实现**（已解密 SQLite + 标准 Python 导出/加密 + 本地开源 ASR），从根本上消除 45 天过期与在线租约依赖。
> 2. **能力边界透明**：原项目“高级版 PRO”中的 123 项功能在本开源仓库中**没有后端实现代码**（仅保留前端占位按钮与演示弹窗），改造时可清理这些占位入口，或在后续基于本地解密数据自行扩展（如将 `/ai` 接入本地 Ollama）。

---

## Phase 0（P0）：紧急本地安全加固

> **目标**：修复本地后端运行期间可被浏览器中任意恶意网页利用的高危漏洞。

- [ ] **0.1 收紧 CORS 配置，禁止通配符 + 携带凭据**
  - 文件：`src/wechat_decrypt_tool/api.py`（L66-L72）
  - 现状：`CORSMiddleware(allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])`
  - 修改：
    - 将 `allow_origins` 收紧为本地前端白名单（例如 `["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:10392", "http://127.0.0.1:10392"]`，Electron `file://` 或自定义协议按需放行）。
    - 关闭 `allow_credentials=False`（本地无 Cookie 登录需求）。
    - （推荐）在启动时生成一次性随机 `X-Local-Token`，由 Electron 主进程注入前端并校验所有 `/api/*` 请求。

- [ ] **0.2 修复 `/api/biz/proxy_image` 的任意本地文件读取（`file://`）与 SSRF 漏洞**
  - 文件：`src/wechat_decrypt_tool/routers/biz.py`（L338-L352）
  - 现状：直接将外部入参 `url` 传入 `urllib.request.urlopen(req, timeout=10)`，未校验协议、未校验主机名、未限制响应体大小。
  - 修改：
    - 强制校验 `urlparse(url).scheme in {"http", "https"}`。
    - 增加域名白名单校验（仅允许 `mmbiz.qpic.cn`、`mmbiz.qlogo.cn`、`wx.qlogo.cn`、`res.wx.qq.com` 等微信图片 CDN，可复用 `src/wechat_decrypt_tool/routers/sns.py` L3353-L3362 的 `_is_allowed_sns_media_host` 或 `routers/chat_media.py` L2332 的 `_is_safe_http_url`）。
    - 禁止重定向到内网 IP（127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16）并限制最大读取字节数（如 20 MB）。

- [ ] **0.3 锁定本地回环绑定与高危管理接口**
  - 文件：`src/wechat_decrypt_tool/runtime_settings.py`（L26-L47、L176-L183）、`src/wechat_decrypt_tool/routers/admin.py`（L54-L56、L87-L100、L177-L188）
  - 修改：
    - 强制默认且仅允许绑定 `127.0.0.1`，禁用 `WECHAT_TOOL_ALLOW_REMOTE_CALLS` 放行非回环请求的能力（避免远程触发 `/api/admin/log-file/open` 或服务重启）。

---

## Phase 1（P1）：切断非必要外连、云端上传与遥测

> **目标**：确保 `wxid`、微信 `global_config`、图片解密密钥及使用行为不再外传给任何第三方服务器。

- [ ] **1.1 移除图片密钥自动上传至 `view.free.c3o.re`**
  - 文件：`src/wechat_decrypt_tool/key_service.py`（L1314-L1335、L1428-L1517）
  - 现状：本地图片密钥 V2 验真未命中时，自动调用 `fetch_and_save_remote_keys()`，向 `https://view.free.c3o.re/api/key` 上传 `wxid`、`global_config` 和 `global_config.crc`。
  - 修改：
    - 删除 `get_image_key_integrated_workflow()`（L1314 起）的远程回退分支，本地扫描/验真失败时直接返回明确提示，让用户手动重试本地内存扫描或手动输入密钥。
    - 移除 `fetch_and_save_remote_keys()` 函数及其路由暴露（检查 `src/wechat_decrypt_tool/routers/keys.py` 中所有调用点）。

- [ ] **1.2 移除 WxCDN 付费原图服务（`https://wxcdn.c3o.re`）**
  - 后端文件：
    - `src/wechat_decrypt_tool/cdn_image_service.py`（L39-L43、L600-L675、L761-L902、L1002-L1078）——曾上传 `wxid`、`global_config`、`global_config.crc` 以及在本地解密失败时上传单图 `key`（AES 密钥）
    - `src/wechat_decrypt_tool/routers/cdn.py`（整文件，注册于 `src/wechat_decrypt_tool/api.py`）
    - `src/wechat_decrypt_tool/routers/system.py`（L82-L115，CDN 开关与配置读写）
    - `src/wechat_decrypt_tool/routers/chat_media.py`（L2690-L2766，媒体缺失时触发 WxCDN 下载逻辑）
  - 前端文件：
    - `frontend/components/PlanWindow.vue`（整文件：Free/Plus/Pro/Ultra 套餐与兑换码弹窗）
    - `frontend/stores/cdnPlan.js`（整文件）
    - `frontend/lib/wxcdn-card/`（整目录）
    - `frontend/components/SettingsDialog.vue`（L579-L616，WxCDN 设置区块）
    - `frontend/app.vue`（L22 及 `PlanWindow` 挂载引用）
  - 修改：注销 `cdn` 路由、移除 `chat_media.py` 中的 CDN 回退调用、删除前端套餐窗口与设置项；媒体缺失时仅提示“本地未缓存原图，请先在微信客户端查看/下载”。

- [ ] **1.3 移除 `license.fqyw.love` 行为遥测上报**
  - 文件：
    - `src/wechat_decrypt_tool/api.py`（L83-L125，`_record_content_free_product_events` HTTP 中间件）
    - `src/wechat_decrypt_tool/native_core_telemetry.py`（整文件，向 `https://license.fqyw.love/v1/events` 上报事件）
  - 修改：移除 `api.py` 中的事件上报中间件，将 `record_product_event()` 改为 no-op 或随 Phase 3 整体移除 `native_core_telemetry.py`。

- [ ] **1.4 禁用自动更新检查与第三方私有下载站链接**
  - 文件：
    - `desktop/package.json`（L142-L147，`publish` / `electron-updater` 配置）
    - `desktop/src/main.cjs`（L1586-L1590，`https://dl.fqyw.love/desktop-updates/...` 硬编码下载链接，以及 `autoUpdater` 初始化逻辑）
  - 修改：关闭桌面端自动联网检查更新，移除 `dl.fqyw.love` 相关跳转。

- [ ] **1.5 （可选）切断定位卡片高德地图外连**
  - 文件：`frontend/components/ChatLocationCard.vue`（L107、L123-L125）、`src/wechat_decrypt_tool/chat_export_service.py`（L7154-L7162）
  - 修改：若追求严格断网，可将高德瓦片图（`webrd01.is.autonavi.com`）替换为纯文字坐标/地址卡片展示，避免外传聊天中的经纬度。

---

## Phase 2（P2）：清理付费/PRO 营销弹窗与未实现占位入口

> **目标**：去除界面中的商业推广、Telegram 买断引导以及点击即弹收费窗口的假入口。

- [ ] **2.1 移除 PRO 演示弹窗与外部推广链接**
  - 文件：
    - `frontend/components/AdvancedFeaturesDialog.vue`（整文件）
    - `frontend/lib/pro-demos/`（整目录，含 `catalog.js` 等 123 项动画演示）
    - `frontend/lib/developer-support.js`（L4-L14，Telegram 群与 `@knchudj` 联系链接）
    - `frontend/composables/useAdvancedFeaturesDialog.js`（如有，全局检索 `openAdvancedFeaturesDialog` / `openFeatureUnavailableDialog`）
    - `frontend/components/SidebarRail.vue`（L94-L105、L530-L531，侧边栏 “Pro” 按钮与 `https://wuuud.com/` 跳转）
  - 修改：删除上述组件与引用，移除 `frontend/app.vue` 中对 `AdvancedFeaturesDialog` 的挂载。

- [ ] **2.2 清理或隐藏无后端实现的占位 UI 入口**
  - **AI 助手页面**：`frontend/pages/ai.vue`（L1-L91）、`frontend/components/SidebarRail.vue`（L51-L61）、`frontend/components/SettingsDialog.vue`（L28、L255-L261、L702、L1048）
    - 方案 A（精简）：从侧边栏和设置页隐藏 AI 入口，删除占位页。
    - 方案 B（自建）：保留入口，按 Phase 4 改造为接入自定义本地 Ollama / OpenAI-Compatible API。
  - **聊天页写操作占位按钮**：
    - `frontend/components/chat/ConversationPane.vue`（L38-L48 “添加消息”按钮、L156-L165 底部“发送”输入框）→ 隐藏或改为只读浏览模式。
    - `frontend/components/chat/ChatOverlays.vue`（L993-L1002 右键“修改文字”）与 `frontend/composables/chat/useChatEditing.js`（L24、L217-L222）→ 移除右键菜单中的占位项。
  - **朋友圈发布占位按钮**：
    - `frontend/pages/sns.vue`（L44-L50、L1187 “发布朋友圈”按钮）→ 移除按钮。

- [ ] **2.3 （可选）清理官网推广目录**
  - 目录：`website/`（仅用于官网宣传与 Pro 对比展示，不影响桌面端/Web 应用运行，自用可删除或忽略）。

---

## Phase 3（P3）：去闭源二进制依赖与解除 45 天硬过期限制

> **目标**：彻底摆脱 `wechatdb_client`/`wechatdb_broker`、`wce_integrity.pyd`、`wda_xkey_helper` 及 `license.fqyw.love` 租约，使项目成为 100% 开源、跨平台（Windows/macOS/Linux）、永不过期的本地工具。

### 3.1 导出完整性组件纯 Python 化（解锁全部 10 类导出）
- [ ] **重构 `src/wechat_decrypt_tool/export_integrity.py`**
  - 现状：强依赖闭源 `src/wechat_decrypt_tool/native/wce_integrity.pyd`（仅 Windows 存在）与 `native_core_export.seal_export_manifest()`（依赖 `NativeCoreFeature.EXPORT` 授权）。
  - 修改：
    - 移除 `load_wce_integrity_native()` 和 `from .native_core_export import seal_export_manifest`。
    - 用纯 Python 实现 `export_css(kind)` 与基础 HTML 导出模板/页眉页脚（直接提供干净的无水印 CSS/HTML，无外部二进制依赖）。
    - 将 `IntegrityZipWriter` 改为标准 `zipfile.ZipFile` 轻量封装；`write_zip_integrity_sidecars()` / `write_active_html_zip_integrity()` / `write_file_integrity_sidecars()` 改为可选生成纯标准 `SHA256SUMS.txt` 或直接 no-op（不再生成 `.wce` / `.wes` 闭源封签）。
- [ ] **替换 `.wec` 加密导出为纯 Python 加密或纯 ZIP 导出**
  - 文件：`src/wechat_decrypt_tool/native_core_export.py`（L186-L241、L436-L622）及调用点：
    - `src/wechat_decrypt_tool/chat_export_service.py`（L3636、L3695）
    - `src/wechat_decrypt_tool/sns_export_service.py`（L3536、L3559）
    - `src/wechat_decrypt_tool/routers/record_export.py`（L773、L910）
    - `src/wechat_decrypt_tool/routers/chat_contacts.py`（L2889、L3011）
    - `src/wechat_decrypt_tool/routers/account_archive_export.py`（L451、L458）
  - 修改：自用导出默认直接输出标准明文 ZIP/HTML/JSON/TXT/Excel；如仍需密码保护，用 Python `cryptography`（AES-256-GCM）或 `pyzipper`（AES-ZIP）替代 `native_core_export` 的 WEC1 封装。
- [ ] **删除闭源导出二进制**
  - 文件：`src/wechat_decrypt_tool/native/wce_integrity.pyd`、`tools/build_wce_integrity.ps1`。

### 3.2 数据库读取解耦 `native_core`（全面转向开源已解密模式）
- [ ] **将默认数据源切换为 `decrypted`（本地已解密 SQLite）**
  - 文件：`src/wechat_decrypt_tool/source_fallback.py`（L6-L14）、`src/wechat_decrypt_tool/account_source_policy.py` 及各 router（`routers/chat.py`、`routers/sns.py`、`routers/biz.py`、`routers/chat_contacts.py`、`routers/favorites.py`、`routers/general.py` 等）。
  - 现状：默认 `source="auto"` 会优先尝试连接闭源 `wcdb_realtime` / `native_core_realtime`，失败后才降级到 `decrypted` 并弹出警告条。
  - 修改：
    - 将默认数据源改为 `decrypted`（项目内置的 `wechat_decrypt.py` 已经是纯开源的微信 4.x SQLCipher 离线解密实现，解密后产出标准 SQLite 文件，所有查询、搜索、年度总结、导出均可直接读取）。
    - 去除前端“实时更新不可用”的常驻降级报错提示（可在设置中保留“手动增量重新解密”按钮）。
- [ ] **（可选进阶）如需本地开源“准实时同步”**
  - 方案：用 `watchdog` 监听微信 `db_storage` 目录（含 `-wal` 文件）变更，触发 `wechat_decrypt.py` + `sqlite_wal.py` 对变更的单个 `.db` 文件执行增量解密覆盖，或接入开源 `sqlcipher3` Python 库以只读方式直接查询加密库，从而完全无需闭源 `wechatdb_broker`。
- [ ] **移除 `native_core` 租约与客户端强依赖**
  - 文件：
    - `src/wechat_decrypt_tool/native_core_client.py`
    - `src/wechat_decrypt_tool/native_core_lease.py`
    - `src/wechat_decrypt_tool/native_core_device_credential.py`
    - `src/wechat_decrypt_tool/native_core_broker.py`
    - `src/wechat_decrypt_tool/native_core_database.py`
    - `src/wechat_decrypt_tool/native_core_realtime.py`
    - `src/wechat_decrypt_tool/native_core_dev_lease.py`
    - `src/wechat_decrypt_tool/native_core_raw_key_cache.py`
    - `src/wechat_decrypt_tool/wcdb_realtime.py`
  - 修改：在完成 3.1 与 3.2 切换后，清理上述模块及其在 `api.py` / `chat_realtime_autosync.py` / `sns_realtime_autosync.py` 中的初始化与路由注册。

### 3.3 桌面端与构建脚本去闭源化
- [ ] **移除启动时自动下载闭源运行库 & 45 天过期校验**
  - 文件：
    - `desktop/src/source-native-core-bootstrap.cjs`
    - `desktop/src/windows-source-native-core-bootstrap.cjs`
    - `desktop/src/native-core-runtime.cjs`
    - `desktop/resources/native-core-source-macos.json`
    - `desktop/resources/native-core-source-windows.json`
    - `desktop/src/main.cjs`（移除对上述 bootstrap / runtime 模块的调用）
  - 修改：Electron 主进程启动时不再校验 `expiresAtUnix`，不再从 GitHub Release 下载 `wechatdb_broker` / `wechatdb_client`。
- [ ] **移除私有 PKI 根证书自动安装逻辑**
  - 文件：
    - `desktop/src/macos-private-pki-runtime.cjs`（曾执行 `security add-trusted-cert -r trustRoot -p codeSign`）
    - `desktop/src/windows-private-pki-runtime.cjs`
    - `desktop/src/main.cjs`（L3327 附近调用点）
  - 修改：个人本地打包或源码运行无需信任原开发者私有代码签名根证书，直接移除。

### 3.4 语音转文字去闭源化
- [ ] **移除依赖 `native_core` 的微信原生 ASR，统一使用开源本地模型**
  - 文件：`src/wechat_decrypt_tool/native_core_voice_asr.py`、`src/wechat_decrypt_tool/native_voice_transcription.py`
  - 修改：保留并默认使用仓库内已完整开源的 `src/wechat_decrypt_tool/voice_transcription.py`（支持 Whisper / Faster-Whisper / Qwen / SenseVoice 本地模型，支持 Hugging Face 与 HF-Mirror 镜像下载）。

### 3.5 密钥提取方案梳理（Windows / macOS）
- [ ] **Windows**：继续使用本地开源方案（`dll_key_scan.py`、`image_key_memory_scan.py`、手动密钥输入；`tools/key_wheels/wx_key-*.whl` 来源为开源项目 `H3CoF6/py_wx_key`，也可自行从源码编译替换）。
- [ ] **macOS**：
  - 文件：`src/wechat_decrypt_tool/macos_db_key_helper.py`、`src/wechat_decrypt_tool/routers/keys.py`（L592-L614）
  - 修改：移除对闭源 `wda_xkey_helper` 的依赖；保留“手动输入数据库密钥”以及仓库内开源的 `macos_db_key_capture.py` / `native/macos/source/wcdb_native_capture.c`（使用前请确认理解对 `WeChat.app` 临时重签名的风险并提前备份）。

---

## Phase 4（P4）：可选自建扩展方向（替代原 PRO 实用功能）

如果希望在纯本地免费版中补齐部分实用体验，可基于已解密 SQLite 自行扩展：

- [ ] **自建本地 AI 聊天助手（替代 `/ai` 占位页）**
  - 后端新增 `/api/ai/chat`，基于已有的 `chat_search_index.py`（FTS 全文检索）召回相关聊天记录作为上下文，直接调用本地 **Ollama**（`http://127.0.0.1:11434`）或用户自填的 OpenAI-Compatible API，完全不经过第三方中转服务器。
- [ ] **自建本地副本编辑（仅限离线副本）**
  - 如仅用于导出前整理脱敏，可在已解密的 SQLite 副本上增加隐藏/脱敏特定消息或导出过滤规则，无需修改微信真实数据库。
- [ ] **Linux 原生运行支持**
  - 完成 Phase 3 后，由于移除了 `wce_integrity.pyd`、DPAPI/Keychain 设备凭证和 Windows/macOS 专属 `native_core`，后端 + 前端可直接作为纯 Web 应用在 Linux / Docker 中运行（用于导入已解密数据库进行分析、搜索、年度总结和导出）。

---

## 附：改造后的测试清理与验收清单

### 需同步调整或移除的测试文件
以下测试专门针对被移除的闭源 `native_core` / 租约 / 遥测策略，完成 Phase 3 后需同步删除或改写为纯开源模式测试：
- `tests/test_native_core_*.py`（全部 12 个文件）
- `tests/test_macos_native_core_policy.py`、`tests/test_windows_native_core_policy.py`
- `tests/test_wcdb_realtime_native_core_required.py`、`tests/test_wcdb_realtime_dll_path_selection.py`、`tests/test_wcdb_sidecar_watchdog_desktop.py`
- `tests/test_export_integrity.py`（改写为验证纯 Python 导出与 SHA-256 校验）

### 最终验收标准
- [ ] **断网验收**：完全断开外网后，应用可正常启动、解密本地数据库、浏览聊天/朋友圈/收藏、全文搜索、生成年度总结、导出全部 10 类格式（HTML/JSON/TXT/Excel/ZIP）以及使用本地 Whisper/Qwen 语音转文字。
- [ ] **时间验收**：将系统时间调至 60 天甚至 1 年后，应用无任何过期报错。
- [ ] **抓包验收**：使用抓包工具监控全流程，确认无任何请求发往 `c3o.re`、`fqyw.love`、`wuuud.com` 等域名。
- [ ] **安全验收**：
  - 使用 `curl -H "Origin: https://evil.example" http://127.0.0.1:10392/api/health` 确认不返回 `Access-Control-Allow-Origin: *` 或 `https://evil.example`。
  - 访问 `http://127.0.0.1:10392/api/biz/proxy_image?url=file:///etc/hosts` 返回 `400` 拒绝。
- [ ] **界面验收**：全站无“高级版 PRO”、“购买兑换码”、“加入 TG 群买断”等弹窗或按钮。
