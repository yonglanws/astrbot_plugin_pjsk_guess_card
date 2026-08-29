# astrbot_plugin_pjsk_guess_card

《初音未来 缤纷舞台》（Project SEKAI）**猜卡面**娱乐插件。插件随机展示一张经过特殊效果处理的角色卡面图片并给出少量提示（花前/花后、星级），玩家需在限时内猜出正确的角色名称。

> 本插件 fork 自 [astrbot_plugin_pjsk_guess_card](https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_card)。玩法更改为让玩家猜出卡面的角色名称，而不是卡面 ID，并增加了多种图片效果。

## 特性

- 🎨 **多样图片效果**：轻度/重度模糊、分块打乱（简易/困难）、横向/纵向切割、截取区域、两长条/三长条截取等（均可独立配置启用、难度分数与参数）
- 🔄 **题库自动同步**：卡池数据自 Haruki master（`cards.json`，三星/四星卡）每 24 小时自动同步，新卡随游戏版本更新自动入库，版本未变跳过大文件下载
- 🌐 **多服务器题库**：支持日服 / 国服题库自由切换，按群独立记忆
- 🏆 **精美数据面板**：内置积分排行榜（Pillow 本地渲染，支持自定义名称、未绑定 QQ 徽章）、个人战绩查询、每日次数限制与冷却
- 🤖 **QQ 官方机器人支持**：官机 markdown 渲染，开局附操作连接，结算附切换题库/绑定/查分/排行榜连接；快捷入口由 `quick_entries` 配置控制（默认关闭）；支持绑定普通 QQ 迁移分数
- ⚡ **双模式退出**：`仅退出本局` 与 `退出自动模式` 严格分离，自动模式精简无扰

## 指令

### 游戏指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `猜卡` / `猜卡面` | `pjsk猜卡面` | 开始一轮随机猜卡游戏 |
| `自动猜卡` / `自动猜卡面` | `pjsk自动猜卡面` | 自动模式：每局结束后自动开下一局 |

### 题库与账号

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `猜卡面切换日服题库` / `猜卡面切换国服题库` | `猜卡切换日服题库` / `猜卡切换国服题库` | 切换本群卡池服务器（下一局生效） |
| `猜卡面绑定 QQ号` | `猜卡面绑定`、`猜卡绑定`、`猜卡面绑定QQ` | 官方机器人账号绑定至普通 QQ（需发送"确认"） |

### 数据与帮助

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `猜卡面排行榜` | `猜卡排行榜`、`本地猜卡排行榜` | 查看猜卡总分排行榜 |
| `猜卡面分数` | `pjsk猜卡面分数`、`猜卡分数`、`猜卡面个人分数` | 查看自己的猜卡数据统计 |
| `猜卡面自定义名称 [名称]` | `自定义名称`、`猜卡自定义名称` | 设置玩家个性化 ID（不带参数可清除） |
| `猜卡面帮助` | - | 显示帮助信息 |

### 管理员指令

| 指令 | 说明 |
| --- | --- |
| `测试猜卡 [效果名称]` | 测试模式，指定效果开始一轮游戏（不计分、不扣次数） |
| `重置猜卡面次数 [用户ID]` | 重置指定用户（或自己）的每日游戏次数 |

### 退出机制说明

- **`仅退出本局`**：仅在游玩中生效，立即结束当前对局并公布答案（自动模式继续开下一局）。
- **`退出自动模式`**（别名：`退出`）：任何时候可触发，停止自动续局（当前对局继续打完）。
- **自动模式精简**：自动模式期间全程不出现 markdown 连接按钮，结算只显示结果、卡名与下一局提示，退出自动模式后恢复完整结算面板。

## 配置说明

通过 AstrBot WebUI 界面进行配置：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `default_server` | string | `jp` | 默认题库服务器（`jp`=日服 / `sc`=国服） |
| `update_interval_hours` | int | `24` | 卡池 master 数据自动更新间隔（小时） |
| `connect_link_template` | string | （官方标签） | QQ 官方机器人结算连接的 markdown 模板 |
| `quick_entries` | list | `[]` | 结算快捷入口指令列表；为空时不显示快捷入口 |
| `jp_resource_url_base` | string | `https://storage.exmeaning.com/sekai-jp-assets` | 日服卡面资源根地址 |
| `sc_resource_url_base` | string | `https://storage.exmeaning.com/sekai-sc-assets` | 国服卡面资源根地址 |
| `quick_entries` | list | `[]` | 结算快捷入口列表（若为空则不显示快捷入口） |
| `answer_timeout` | int | `30` | 答题超时时间（秒） |
| `daily_play_limit` | int | `10` | 每日游戏次数上限（-1 为无限制） |
| `game_cooldown_seconds` | int | `60` | 游戏冷却时间（秒） |
| `max_guess_attempts` | int | `10` | 每轮最大尝试回答次数上限（-1 为无限制） |
| `ranking_display_count` | int | `10` | 排行榜显示人数（建议 5-20） |
| `reward_valid_time` | int | `5` | 首位答对后的奖励有效时间（秒，0 为禁用） |
| `group_whitelist` | list | `[]` | 群聊白名单（为空则所有群可用） |
| `whitelist_reject_message`| string | （提示语） | 非白名单群聊提示语（留空不提示） |
| `super_users` | list | `[]` | 管理员用户 ID 列表 |
| `blacklist` | list | `[]` | 黑名单用户 ID 列表 |
| `effects` | object | `{...}` | 各图片效果的启用、分数与详细参数配置 |

## 资源

- 日服master：[Team-Haruki/haruki-sekai-master](https://github.com/Team-Haruki/haruki-sekai-master)
- 国服master：[Team-Haruki/haruki-sekai-sc-master](https://github.com/Team-Haruki/haruki-sekai-sc-master)
- 角色数据：`characters.json`（内置 26 位角色中文名与别名映射）
- 日服卡面资源：`https://storage.exmeaning.com/sekai-jp-assets/character`
- 国服卡面资源：`https://storage.exmeaning.com/sekai-sc-assets/character`

优先走 GitHub Contents API（大文件走 raw 媒体类型通道），失败时回退 jsDelivr CDN。数据持久化于 `data/plugin_data/pjsk_guess_card/`。

## 依赖

`Pillow`、`pilmoji`、`aiohttp`。图片全部使用 Pillow 本地渲染。

## 致谢

部分代码及灵感参考自 [astrbot_plugin_pjsk_guess_song](https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_song)。在此致谢。

完整更新历史见 [CHANGELOG.md](CHANGELOG.md)。
