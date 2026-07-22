# astrbot_plugin_miyoushe

米游社官方资讯推送插件 —— 为 [AstrBot](https://github.com/Soulter/AstrBot) 提供米游社多游戏资讯的自动推送与查询功能。

## 致谢

本插件的 HTML/CSS 渲染模板源自 [Yunzai-genshin](https://github.com/TimeRainStarSky/Yunzai-genshin) 插件（`apps/mysNews.js`、`model/mysNews.js`），在原版米游社风格的模板基础上进行了适配和优化。感谢 [TimeRainStarSky](https://github.com/TimeRainStarSky) 及 Yunzai 社区。

## 功能

- 定时轮询米游社官方公告/资讯并推送到群
- 支持图片渲染（复用米游社原版样式，含文章详情页和列表页）
- 支持多游戏：原神、崩坏：星穹铁道、绝区零、崩坏三、崩坏二、未定事件簿
- 支持两种推送类型：公告、资讯
- 支持序号查询、列表模式、帖子搜索、URL 自动解析
- 活动到期预警（每日轮询）
- 多平台支持：aiocqhttp、qq_official、telegram、weixin_oc
- 自动降级：高清渲染失败时自动降级为普通分辨率或文字兜底

## 命令

| 命令 | 说明 |
|------|------|
| `#原神公告` | 查看最新一条公告 |
| `#原神公告3` | 查看第 3 条公告 |
| `#原神公告列表` | 查看公告列表 |
| `#崩铁资讯` | 查看最新一条资讯 |
| `#米游社 关键词` | 搜索米游社帖子 |
| `#米游社 关键词 2` | 搜索并取第 2 条 |
| `#开启原神公告推送` | 开启公告推送（群管理员） |
| `#关闭崩铁资讯推送` | 关闭资讯推送（群管理员） |
| `#开启原神活动预警` | 开启活动到期预警（群管理员）|
| `#推送原神公告` | 手动触发推送（Bot 管理员）|
| `#米游社推送` | 查看帮助 |
| 发送文章链接 | 自动解析并展示文章详情 |

> 支持的游戏名：原神、崩铁/星铁、绝区零、崩坏三/崩3、崩坏二/崩2、未定/未定事件簿

## 安装

在 AstrBot 中通过插件市场安装，或手动将本仓库克隆到 `data/plugins/` 目录：

```bash
cd data/plugins
git clone https://github.com/makotowu/astrbot_plugin_miyoushe
/plugin reload
```

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `proxy` | string | "" | HTTP 代理地址 |
| `push_interval` | int | 300 | 轮询间隔（秒）|
| `push_window` | int | 7200 | 新资讯推送时间窗口（秒）|
| `max_push_per_round` | int | 2 | 每轮每个游戏+类型最大推送条数 |
| `rai` | bool | true | 是否启用图片渲染推送 |
| `render_cache_limit` | int | 32 | 渲染图片缓存上限 |

## 架构

```
main.py          → 主入口、命令注册、定时轮询、权限控制
renderer.py      → 图片渲染器（模板内联、分页、降级）
mhy_client.py    → 米游社 API 客户端（只读）
constants.py     → 游戏/类型常量映射
data_store.py    → 推送配置持久化
dedup_store.py   → SQLite 去重存储
```

## 协议

MIT License
