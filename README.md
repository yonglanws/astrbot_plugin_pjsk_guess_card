# PJSK猜卡面插件使用说明

## 1. 插件功能简介

> 本插件fork自[链接](https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_card)。玩法更改为让玩家猜出卡面的角色名称，而不是卡面ID，并增加了多种图片效果

本插件是使用《初音未来 缤纷舞台》的卡面资源制作的猜卡面游戏。插件会随机展示一张经过特殊效果处理的角色卡面图片，并给出少量的提示(花前/花后，星级)，玩家需要在规定时间内根据图片和提示猜出正确的角色名称。

插件内置了猜卡面排行榜、每日游戏次数限制、游戏冷却等功能。

### 图片效果系统
- **轻度模糊**：轻微的高斯模糊效果（2分）
  <img width="800" height="492" alt="mohu1" src="https://github.com/user-attachments/assets/a2a17b3f-b49e-4e73-860b-bd4a1c3355f6" />

- **重度模糊**：高强度高斯模糊效果（3分）
  <img width="800" height="492" alt="mohu2" src="https://github.com/user-attachments/assets/542cbaf1-9257-46ab-a11f-01e691f83886" />

- **分块打乱(简易)**：较大方块的随机排列（1分）
  <img width="800" height="457" alt="03507283e5565746a5773e34f6005fc2" src="https://github.com/user-attachments/assets/16417c3f-93eb-43cd-be3c-de76e99f9b80" />


- **分块打乱(困难)**：较小方块的随机排列（4分）
  <img width="800" height="492" alt="f7d037b3898764bd56822552d65351fe" src="https://github.com/user-attachments/assets/fb4af477-1560-4675-9de6-39b8982a2ff7" />


- **损坏效果**：剧烈的撕裂和噪点效果（3分）
  <img width="800" height="492" alt="22e5b04100ef95bfeedb75fb0608ff76" src="https://github.com/user-attachments/assets/bce7ed3c-8897-4b8c-8d8c-611ea44715c2" />


  
**注意**：本插件所有必需的图片和数据资源均托管在服务器，并已经默认配置，不需要下载（日后可能会上传资源下载供本地使用），资源截止至日服v5.4.0版本

## 2. 指令列表

### 游戏指令
- `猜卡` / `猜卡面` / `pjsk猜卡面`: 开始一轮随机的猜卡游戏。

### 数据与帮助
- `猜卡面帮助`: 显示本帮助信息。
- `猜卡面排行榜` : 查看猜卡总分排行榜。
- `猜卡面分数` : 查看自己的猜卡数据统计。

### 管理员指令
- `重置猜卡面次数` `[用户ID]`: 重置指定用户（或自己）的每日游戏次数。
  - **示例**: `重置猜卡面次数 123456789` (重置指定QQ号的次数) 或 `重置猜卡面次数` (重置自己的次数)。

## 3. 插件配置说明

插件的配置由机器人管理员通过 AstrBot 框架提供的 **WebUI 界面**进行修改。以下是可配置的选项说明：

```json
{
  "group_whitelist": [],
  "super_users": [],
  "answer_timeout": 300,
  "daily_play_limit": 10,
  "game_cooldown_seconds": 60,
  "max_guess_attempts": 10,
  "remote_resource_url_base": "http://47.110.56.9",
  "use_local_resources": false,
  "ranking_display_count": 10,
  "effects": {
    "light_blur": {"enabled": true, "difficulty": 2, "blur_radius": 15},
    "heavy_blur": {"enabled": true, "difficulty": 3, "blur_radius": 40},
    "shuffle_blocks_easy": {"enabled": true, "difficulty": 1, "block_size": 65},
    "shuffle_blocks_hard": {"enabled": true, "difficulty": 5, "block_size": 20},
    "glitch": {"enabled": true, "difficulty": 4, "glitch_intensity": 1}
  }
}
```

### 基础配置

- `group_whitelist` (列表): **群聊白名单**。只有在此列表中的群号才能使用本插件。若列表为空 `[]`，则对所有群聊生效。
- `super_users` (列表): **管理员用户ID列表**。
- `answer_timeout` (整数): 每轮游戏的回答**超时时间**（秒）。
- `daily_play_limit` (整数): 每个用户每天可以**发起游戏**的最大次数（-1表示无限制）。
- `game_cooldown_seconds` (整数): 游戏结束后的**冷却时间**（秒）。
- `max_guess_attempts` (整数): 每轮游戏中，所有玩家总共可以**尝试回答**的次数上限（-1表示无限制）。
- `remote_resource_url_base` (字符串): 远程资源服务器的根 URL，当使用远程资源时从此 URL 下载。
- `use_local_resources` (布尔): 是否使用本地资源，true 为使用本地 resources 文件夹，false 为使用远程资源。
- `ranking_display_count` (整数): 排行榜显示的玩家数量，建议范围：5-20。

### 图片效果配置

所有效果都可以配置是否启用、分数以及效果参数：

#### 轻度模糊
- `enabled` (布尔): 是否启用轻度模糊效果
- `difficulty` (整数): 分数，控制猜对时获得的分数
- `blur_radius` (整数): 模糊半径，控制模糊效果的强度，值越大越模糊

#### 重度模糊
- `enabled` (布尔): 是否启用重度模糊效果
- `difficulty` (整数): 分数，控制猜对时获得的分数
- `blur_radius` (整数): 模糊半径，控制模糊效果的强度，值越大越模糊

#### 分块打乱(简易)
- `enabled` (布尔): 是否启用分块打乱(简易)效果
- `difficulty` (整数): 分数，控制猜对时获得的分数
- `block_size` (整数): 区块大小，控制分块的大小，值越大区块越少

#### 分块打乱(困难)
- `enabled` (布尔): 是否启用分块打乱(困难)效果
- `difficulty` (整数): 分数，控制猜对时获得的分数
- `block_size` (整数): 区块大小，控制分块的大小，值越大区块越少

#### 损坏效果
- `enabled` (布尔): 是否启用损坏效果
- `difficulty` (整数): 分数，控制猜对时获得的分数
- `glitch_intensity` (整数): 损坏强度，控制损坏效果的强度，值越大损坏越剧烈

## 4.更新日志

### TODO
- [x] 更多的负面效果
- [ ] 用户可以主动请求提示，例如提示该成员所处的团体为"25时"，获取提示的代价是减少分数

~~- [ ] 增加游戏难度设置，难度更高的游戏会增加卡面的模糊程度/增加更多负面效果~~

- [x] 角色名称增加别名，例如“糖”可以代替“晓山瑞希”

### v1.5.0
- 新增效果参数可配置功能，现在可以在WebUI中配置各效果的启用状态和分数，以及模糊半径、区块大小、损坏强度等详细参数
- 修复分块打乱逻辑，确保无空白块、无破碎效果，所有区块都被正确填充和打乱
- 增加部分角色别名

### v1.4.0
- 修复跨群组尝试次数泄露问题，确保每个群组的尝试计数器完全独立
- 重构游戏状态管理，使用GameSession类实现会话状态隔离
- 新增缓存机制，优化图片处理性能
- 改进图片清理流程，确保缓存与文件系统同步

### v1.3.0
- 新增5种不同难度的图片效果。效果随机选择，游戏体验更加多样
- 修复Python 3.9及以下版本的类型注解兼容性问题
- 优化图片处理性能，确保实时渲染

### v1.2.0
- 修复数据库连接句柄泄漏问题
- 优化字体资源加载，避免重复IO操作
- 修复锁字典内存泄漏问题
- 保护后台任务不被GC回收
- 使用多线程处理CPU密集型任务，提升响应速度
- 实现输入过滤机制，仅接受有效的角色名称或别名，防止普通聊天干扰游戏

### v1.1.0
- 增加角色名称别名功能，例如“糖”可以代替“晓山瑞希”

### v1.0.0
- 初始版本，实现基本的猜卡游戏功能

## 5.联系作者
- 反馈：欢迎在 GitHub Issues 提交问题或建议
- QQ交流群：1065547818
