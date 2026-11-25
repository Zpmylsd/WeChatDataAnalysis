<div align="center">
    <h1>WeChatDataAnalysis - 微信数据库解密与分析工具（Windows 内网自用去限制版）</h1>
    <p>微信 4.x 数据库本地解密与分析，支持聊天记录、朋友圈、联系人、收藏浏览与全格式导出、年度总结。</p>
</div>

## 内网自用去限制版改造说明

本仓库已根据 [docs/LOCALIZATION_MODIFICATION_CHECKLIST.md](docs/LOCALIZATION_MODIFICATION_CHECKLIST.md) 完成去限制与纯本地化改造，**核心控制权完全归属本地**：

1. **解除 45 天硬过期与在线租约强杀进程**：解耦闭源 `native_core`（`wechatdb_broker.exe` / `wechatdb_client.dll`），数据源默认走本机已解密的 SQLite（`decrypted` 模式，永久有效、可自由读写扩展），彻底移除向 `license.fqyw.love` 发送设备指纹心跳及 4xx 强杀进程的机制。
2. **彻底切断外部上传与遥测**：
   - 移除 `key_service.py` 中本地图片密钥 V2 验真未命中时静默上传 `wxid` + `global_config` + `global_config.crc` 到 `view.free.c3o.re` 的回退逻辑。
   - 移除 `api.py` 中每次打开会话、搜索、导出时向 `license.fqyw.love/v1/events` 上报操作遥测的中间件。
   - 移除桌面端启动时自动从外部下载闭源二进制、安装私有 PKI 根证书及 `dl.fqyw.love` 更新引导的逻辑。
3. **导出纯 Python 化（移除 `wce_integrity.pyd` 与 `.wec` 加密壳）**：
   - 用纯 Python 模块（`export_html_assets.py` + `export_integrity.py`）完整提供 5 套导出 CSS 样式表与 HTML 交互运行时（会话搜索、语音播放、日期跳转、合并转发弹窗、分页按需加载），并移除 `#wceBrandBlocker` 页面篡改锁死限制。
   - 所有导出直接生成标准 HTML / JSON / TXT / Excel 与标准 `.zip` 压缩包，不再生成 `.wec` 闭源加密壳或 `.wes` 封签。
4. **移除 WxCDN 配额计费与 PRO 推广噪音**：
   - 整套删除 WxCDN 前后端代码、兑换码套餐窗口、`AdvancedFeaturesDialog.vue` 推广弹窗（123 项演示动画）以及无后端实现的占位按钮。
5. **语音转文字默认使用本地开源模型**：
   - 使用内置开源 `voice_transcription.py`（Whisper / Qwen / SenseVoice），不锁死特定微信客户端版本。

---

## 年度总结

<table>
  <tr>
    <td align="center" colspan="2"><img src="frontend/public/style1.png" alt="年度总结 Modern" width="800"/></td>
  </tr>
  <tr>
    <td><img src="frontend/public/AnnualSummary1.gif" alt="AnnualSummary 1" width="400"/></td>
    <td><img src="frontend/public/AnnualSummary2.gif" alt="AnnualSummary 2" width="400"/></td>
  </tr>
  <tr>
    <td><img src="frontend/public/AnnualSummary3.gif" alt="AnnualSummary 3" width="400"/></td>
    <td><img src="frontend/public/AnnualSummary4.gif" alt="AnnualSummary 4" width="400"/></td>
  </tr>
  <tr>
    <td><img src="frontend/public/AnnualSummary5.gif" alt="AnnualSummary 5" width="400"/></td>
    <td><img src="frontend/public/AnnualSummary6.png" alt="AnnualSummary 6" width="400"/></td>
  </tr>
  <tr>
    <td><img src="frontend/public/AnnualSummary7.png" alt="AnnualSummary 7" width="400"/></td>
    <td><img src="frontend/public/AnnualSummary8.png" alt="AnnualSummary 8" width="400"/></td>
  </tr>
</table>

## 界面预览

<table>
  <tr>
    <td align="center" colspan="2"><b>聊天记录页面</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/message.png" alt="聊天记录页面" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>设置面板</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/setting.png" alt="设置面板" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>朋友圈</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/sns.png" alt="朋友圈" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>聊天记录搜索</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/search.png" alt="聊天记录搜索" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>聊天记录导出</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/export.png" alt="聊天记录导出" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>联系人导出</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/Contact.png" alt="联系人导出" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>微信收藏</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/readme-favorites.png" alt="微信收藏" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>小程序</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/readme-mini-programs.png" alt="小程序" width="800"/></td>
  </tr>
  <tr>
    <td align="center" colspan="2"><b>视频号直播</b></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="frontend/public/readme-finder.png" alt="视频号直播" width="800"/></td>
  </tr>
</table>

## 可导出的内容

| 内容 | 支持格式 | 导出范围 / 说明 |
| --- | --- | --- |
| 聊天记录 | HTML / JSON / TXT / Excel（标准 ZIP 或增量目录） | 当前会话、自定义会话、全部会话、群聊或单聊；支持按消息类型和时间筛选 |
| 朋友圈 | HTML / JSON / TXT / Excel（标准 ZIP 或增量目录） | 指定联系人或全部联系人 |
| 联系人 | HTML / JSON / TXT / Excel / CSV | 支持按联系人分类和关键词筛选，可选择是否包含头像链接 |
| 收藏 | HTML / JSON / TXT / Excel（标准 ZIP） | 支持按收藏类型和关键词筛选 |
| 好友验证 | HTML / JSON / TXT / Excel | 支持按发起方向和关键词筛选 |
| 小程序 | HTML / JSON / TXT / Excel | 支持按关键词筛选 |
| 视频号直播 | HTML / JSON / TXT / Excel | 支持按直播类型和关键词筛选 |
| 转账与红包 | HTML / JSON / TXT / Excel | 支持按记录类型和关键词筛选 |
| 服务号记录 | HTML / JSON / TXT / Excel | 支持按服务号和记录类型导出 |
| 账号数据归档 | 标准 ZIP | 可选择导出数据库、资源文件或两者 |

> Excel 格式生成 `.xlsx` 文件；聊天记录、朋友圈和收藏会将对应格式文件与必要资源一起打包为标准 ZIP（无闭源 `.wec` 加密壳限制）。

## 快速开始

### 1. 下载 Windows 桌面安装包（推荐）

1. 打开本仓库 Release 页面：https://github.com/Zpmylsd/WeChatDataAnalysis/releases
2. 下载 `WeChatDataAnalysis-<version>-Setup.exe` 安装包（或独立后端 `wechat-backend.exe`）
3. 安装完成后启动 `WeChatDataAnalysis`，先在首页检测本机微信并提取密钥、完成数据库解密，即可离线浏览与导出全部数据。

### 2. 从源码运行（开发者）

#### 2.1 克隆项目

```bash
git clone https://github.com/Zpmylsd/WeChatDataAnalysis.git
cd WeChatDataAnalysis
```

#### 2.2 安装后端依赖

```bash
# 使用 uv (推荐)
uv sync --no-editable
```

#### 2.3 安装桌面端与前端依赖

```bash
cd desktop
npm ci
cd ../frontend
npm ci
cd ..
```

#### 2.4 启动桌面开发模式

```bash
cd desktop
npm run dev
```

#### 或分别启动后端 API 与前端开发服务器

```bash
# 终端 1：在项目根目录启动后端 API（默认 http://127.0.0.1:10392）
uv run --no-sync main.py

# 终端 2：在 frontend 目录启动前端（默认 http://localhost:3000）
cd frontend
npm run dev
```

## 打包为 EXE（Windows 桌面端）

本项目支持在本机 Windows x64 环境或通过 GitHub Actions（`.github/workflows/release.yml`）一键编译生成免私有证书签名的 `Setup.exe` 与 `wechat-backend.exe`：

```bash
cd desktop
npm ci
npm run dist
```

输出位置：
- 桌面端安装包：`desktop/dist/WeChatDataAnalysis-<version>-Setup.exe`
- 独立后端可执行文件：`desktop/resources/backend/wechat-backend.exe`

## 语音模型下载源

在「设置 → 语音转文字 → 模型下载源」可切换官方源（Hugging Face）与国内镜像（[HF-Mirror](https://hf-mirror.com)）。

## 安全说明与免责声明

1. **仅限个人/内网自用**：此工具仅用于解密和备份您自己合法持有的微信数据。
2. **密钥与数据隐私**：解密后的 SQLite 数据库与媒体资源均保存在本机，请妥善保管您的密钥与导出文件。
3. **非官方工具**：本项目与微信、腾讯及其关联主体不存在隶属、授权或合作关系。
