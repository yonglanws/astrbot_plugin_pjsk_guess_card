import asyncio
import json
import random
import re
import time
import os
import sqlite3
import io
from typing import List, Dict, Optional, Tuple, Union
from pathlib import Path
from jinja2 import Template
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
from pilmoji import Pilmoji
from urllib.error import URLError
from urllib.parse import urlparse
import aiohttp


try:
    # 兼容Pillow >= 9.1.0, 使用 Resampling 枚举
    from PIL.Image import Resampling
    LANCZOS = Resampling.LANCZOS
except ImportError:
    # 兼容Pillow < 9.1.0, ANTIALIAS 的值为 1，直接使用该值以绕过linter
    LANCZOS = 1

# AstrBot's recommended logger. If this fails, the environment is likely misconfigured.

from astrbot.api import logger


try:
    # Attempt to import from the standard API path first.
    from astrbot.api.event import filter, AstrMessageEvent
    from astrbot.api.star import Context, Star, register, StarTools
    import astrbot.api.message_components as Comp
    from astrbot.core.utils.session_waiter import session_waiter, SessionController
    from astrbot.api import AstrBotConfig
except ImportError:
    # Fallback for older versions or different project structures.
    logger.error("Failed to import from astrbot.api, attempting fallback. This may indicate an old version of AstrBot.")
    from astrbot.core.plugin import Plugin as Star, Context, register, filter, AstrMessageEvent  # type: ignore
    import astrbot.core.message_components as Comp  # type: ignore
    from astrbot.core.utils.session_waiter import session_waiter, SessionController  # type: ignore
    # Fallback for StarTools if it's missing in older versions
    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str) -> Path:
            # Provide a fallback implementation that mimics the original get_db_path logic
            # This path is relative to the directory containing the 'plugins' folder
            return Path(__file__).parent.parent.parent.parent / 'data' / 'plugins_data' / plugin_name


# --- 插件元数据 ---
PLUGIN_NAME = "pjsk_guess_card"
PLUGIN_AUTHOR = "nichinichisou"
PLUGIN_DESCRIPTION = "PJSK猜卡插件"
PLUGIN_VERSION = "1.1.1" # 版本升级
PLUGIN_REPO_URL = "https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_card"


# --- 数据库管理 ---
def get_db_path(context: Context, plugin_dir: Path) -> str:
    """获取数据库文件的路径，确保它在插件的数据目录中"""
    plugin_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
    os.makedirs(plugin_data_dir, exist_ok=True)
    return str(plugin_data_dir / "guess_card_data.db")


def init_db(db_path: str):
    """初始化数据库和表"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id TEXT PRIMARY KEY,
                user_name TEXT,
                score INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                correct_attempts INTEGER DEFAULT 0,
                last_play_date TEXT,
                daily_plays INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()


# --- 图像处理函数 ---
# def optimize_image(image_path: str, output_path: Optional[str] = None, quality: int = 70, max_size: tuple = (800, 800)) -> str:
#     """
#     优化图像，降低质量和大小以加快发送速度
    
#     Args:
#         image_path: 原图路径
#         output_path: 输出路径，如果为None则生成临时路径
#         quality: JPEG质量 (1-100)
#         max_size: 最大尺寸 (宽, 高)
        
#     Returns:
#         优化后图像的路径
#     """
#     if output_path is None:
#         # 生成临时文件路径
#         dirname = os.path.dirname(image_path)
#         basename = os.path.basename(image_path)
#         filename, ext = os.path.splitext(basename)
#         output_path = os.path.join(dirname, f"{filename}_optimized{ext}")
    
#     try:
#         with Image.open(image_path) as img:
#             # 检查是否需要缩放
#             width, height = img.size
#             if width > max_size[0] or height > max_size[1]:
#                 # 计算缩放比例
#                 ratio = min(max_size[0] / width, max_size[1] / height)
#                 new_size = (int(width * ratio), int(height * ratio))
#                 # 使用resize而非thumbnail，避免LANCZOS类型问题
#                 img = img.resize(new_size)
            
#             # 转换为RGB模式(JPEG不支持透明通道)
#             if img.mode == 'RGBA':
#                 img = img.convert('RGB')
            
#             # 保存为优化的JPEG
#             img.save(output_path, "JPEG", quality=quality, optimize=True)
#             logger.info(f"图像已优化: {output_path}")
#             return output_path
#     except Exception as e:
#         logger.error(f"图像优化失败: {e}")
#         return image_path  # 失败时返回原路径


# --- 卡牌数据加载 ---
def load_card_data(resources_dir: Path) -> Tuple[Optional[List[Dict]], Optional[Dict]]:
    """从插件的 resources 目录加载 guess_cards.json 和 characters.json 的数据"""
    try:
        cards_file = resources_dir / "guess_cards.json"
        characters_file = resources_dir / "characters.json"
        
        with open(cards_file, "r", encoding="utf-8") as f:
            guess_cards = json.load(f)
        with open(characters_file, "r", encoding="utf-8") as f:
            characters_data = json.load(f)
        
        characters_map = {char["characterId"]: char for char in characters_data}
        return guess_cards, characters_map
    except FileNotFoundError as e:
        logger.error(f"加载卡牌数据失败: {e}. 请确保 'guess_cards.json' 和 'characters.json' 在插件的 'resources' 目录中。")
        return None, None


# --- 核心插件类 ---
@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESCRIPTION, PLUGIN_VERSION, PLUGIN_REPO_URL)
class GuessCardPlugin(Star):  # type: ignore
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_dir = Path(os.path.dirname(__file__))
        self.resources_dir = self.plugin_dir / "resources"
        self.db_path = get_db_path(context, self.plugin_dir)
        init_db(self.db_path)
        self.guess_cards, self.characters_map = load_card_data(self.resources_dir)
        self.last_game_end_time = {} # 存储每个会话的最后游戏结束时间
        self.http_session = None

        # 为每个会话的游戏启动过程添加锁，防止竞态条件
        self.session_locks = {}

        # 新增：创建角色名到ID的映射
        self.character_name_to_id_map = {
            char['name'].lower(): char_id for char_id, char in self.characters_map.items()
        } if self.characters_map else {}

        # 使用 context 初始化共享的游戏会话状态
        if not hasattr(self.context, "active_game_sessions"):
            self.context.active_game_sessions = set()

        if not self.guess_cards or not self.characters_map:
            logger.error("插件初始化失败，缺少必要的卡牌数据文件。插件功能将受限。")
        
        if not aiohttp:
            logger.warning("`aiohttp` 模块未安装，远程图片功能将受限或性能较差。建议安装: pip install aiohttp")

        # --- 新增：初始化后台任务句柄 ---
        self._cleanup_task = None

        # 启动时清理一次旧图片
        self._cleanup_output_dir()
        # --- 新增：启动周期性清理任务 ---
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task())

    async def _get_session(self) -> Optional['aiohttp.ClientSession']:
        """延迟初始化并获取 aiohttp session"""
        if not aiohttp:
            return None
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
        return self.http_session

    async def _send_stats_ping(self, game_type: str):
        """(已重构) 向专用统计服务器的5000端口发送GET请求。"""
        if self.config.get("use_local_resources", True):
            return

        resource_url_base = self.config.get("remote_resource_url_base", "")
        if not resource_url_base:
            return

        try:
            session = await self._get_session()
            if not session:
                logger.warning("aiohttp not installed, cannot send stats ping.")
                return

            # 从资源URL中提取协议和主机名，然后强制使用5000端口
            parsed_url = urlparse(resource_url_base)
            stats_server_root = f"{parsed_url.scheme}://{parsed_url.hostname}:5000"
            
            # 构建最终的统计请求URL
            ping_url = f"{stats_server_root}/stats_ping/{game_type}.ping"

            # 异步发送请求
            async with session.get(ping_url, timeout=2):
                pass  # We just need the request to be made.
        except Exception as e:
            logger.warning(f"Stats ping to {ping_url} failed: {e}")

    async def _periodic_cleanup_task(self):
        """每隔一小时自动清理一次 output 目录。"""
        cleanup_interval_seconds = 3600 # 1 hour
        while True:
            await asyncio.sleep(cleanup_interval_seconds)
            logger.info("开始周期性清理 guess_card output 目录...")
            try:
                # 猜卡插件的清理任务IO不多，可以直接运行
                self._cleanup_output_dir()
            except Exception as e:
                logger.error(f"猜卡插件周期性清理任务失败: {e}", exc_info=True)

    def _get_resource_path_or_url(self, relative_path: str) -> Optional[Union[Path, str]]:
        """根据配置返回资源的本地Path对象或远程URL字符串。"""
        use_local = self.config.get("use_local_resources", True)
        if use_local:
            path = self.resources_dir / relative_path
            return path if path.exists() else None
        else:
            base_url = self.config.get("remote_resource_url_base", "").strip('/')
            if not base_url:
                logger.error("配置为使用远程资源，但 remote_resource_url_base 未设置。")
                return None
            return f"{base_url}/{'/'.join(Path(relative_path).parts)}"

    async def _open_image(self, relative_path: str) -> Optional[Image.Image]:
        """打开一个资源图片，无论是本地路径还是远程URL。"""
        source = self._get_resource_path_or_url(relative_path)
        if not source:
            return None
        
        try:
            if isinstance(source, str) and source.startswith(('http://', 'https://')):
                session = await self._get_session()
                if not session:
                    logger.error("无法获取远程图片: `aiohttp` 模块未安装。")
                    return None
                
                async with session.get(source) as response:
                    response.raise_for_status() # Will raise an error for non-200 status
                    image_data = await response.read()
                    return Image.open(io.BytesIO(image_data))
            else:
                return Image.open(source)
        except (URLError, Exception) as e:
            logger.error(f"无法打开图片资源 {source}: {e}", exc_info=True)
            return None

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """
        检查当前消息是否被允许.
        - 如果白名单为空, 则允许所有群聊和私聊.
        - 如果白名单不为空, 则只允许在白名单内的群聊中触发, 并禁用所有私聊.
        """
        whitelist = self.config.get("group_whitelist", [])
        
        if not whitelist:
            return True # 白名单为空, 允许所有
        
        # 白名单不为空, 开始严格检查
        group_id = event.get_group_id()
        if group_id and str(group_id) in whitelist:
            return True # 是白名单中的群聊, 允许
            
        return False # 是私聊, 或非白名单群聊, 均不允许

    def get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        return sqlite3.connect(self.db_path)

    async def _create_options_image(self, options: List[Dict], cols: int = 3) -> Optional[str]:
        """根据提供的选项（缩略图）列表生成一个网格状的选项图片"""
        if not options:
            return None

        thumb_w, thumb_h = 128, 128 # 固定尺寸
        
        padding = 15
        text_h = 35
        
        # 根据列数计算行数
        rows = (len(options) + cols - 1) // cols # 向上取整
        
        img_w = cols * thumb_w + (cols + 1) * padding
        img_h = rows * (thumb_h + text_h) + (rows + 1) * padding

        img = Image.new('RGBA', (img_w, img_h), (245, 245, 245, 255)) # 浅灰色背景
        
        try:
            font = ImageFont.truetype(str(self.resources_dir / "font.ttf"), 20)
        except IOError:
            font = ImageFont.load_default()

        draw = ImageDraw.Draw(img)

        for i, option in enumerate(options):
            row_idx = i // cols
            col_idx = i % cols
            
            x = padding + col_idx * (thumb_w + padding)
            y = padding + row_idx * (thumb_h + text_h + padding)

            try:
                thumb_img = await self._open_image(option['relative_thumb_path'])
                if not thumb_img: continue
                
                thumb = thumb_img.convert("RGBA").resize((thumb_w, thumb_h), LANCZOS)
                
                img.paste(thumb, (x, y), thumb)
                
                # 绘制ID文本
                text = f"ID: {option['id']}"
                text_bbox = draw.textbbox((0, 0), text, font=font)
                if text_bbox:
                    text_width = text_bbox[2] - text_bbox[0]
                    text_x = x + (thumb_w - text_width) / 2
                    text_y = y + thumb_h + 5
                    draw.text((text_x, text_y), text, font=font, fill=(30, 30, 50))
            except Exception as e:
                logger.error(f"处理缩略图失败: {option['relative_thumb_path']}, 错误: {e}")
                continue

        # Save image
        output_dir = self.plugin_dir / "output"
        os.makedirs(output_dir, exist_ok=True)
        img_path = output_dir / f"options_{int(time.time())}.png"
        img.save(img_path)
        return str(img_path)

    def _cleanup_output_dir(self, max_age_seconds: int = 3600):
        """清理旧的排行榜图片和选项图片"""
        output_dir = self.plugin_dir / "output"
        if not output_dir.exists():
            return
            
        now = time.time()
        try:
            for filename in os.listdir(output_dir):
                file_path = output_dir / filename
                # 确保只删除本插件生成的 png 和 jpg 图片 (包括排行榜、选项图和优化后的答案图)
                if file_path.is_file() and (
                    filename.startswith("ranking_") or 
                    filename.startswith("options_") or
                    filename.startswith("answer_")
                ) and (filename.endswith(".png") or filename.endswith(".jpg")):
                    file_mtime = file_path.stat().st_mtime
                    if (now - file_mtime) > max_age_seconds:
                        os.remove(file_path)
                        logger.info(f"已清理旧图片: {filename}")
        except Exception as e:
            logger.error(f"清理图片时出错: {e}")

    # --- 游戏逻辑 ---
    def start_new_game(self, character_id: Optional[int] = None) -> Optional[Dict]:
        """准备一轮新游戏，加入花前/花后逻辑"""
        if not self.guess_cards or not self.characters_map:
            logger.error("无法开始游戏，因为卡牌数据未成功加载。")
            return None

        card_pool = self.guess_cards
        if character_id:
            card_pool = [c for c in self.guess_cards if c['characterId'] == character_id]
            if not card_pool:
                logger.warning(f"没有找到角色ID为 {character_id} 的卡牌。")
                return None

        card = random.choice(card_pool)
        difficulty = random.choice(["easy", "normal", "hard"])
        card_type = random.choice(["normal", "after_training"])
        
        # 修正: 使用 card['id'] 和 card_type 来构建正确的问题图片文件名
        question_img_name = f"{card['id']}_card_{card_type}_{difficulty}.png"
        answer_image_filename = f"card_{card_type}.png"

        # 当使用本地资源时，检查图片是否存在
        if self.config.get("use_local_resources", True):
            question_img_path = self.resources_dir / "questions" / question_img_name
            if not question_img_path.exists():
                logger.error(f"问题图片未找到: {question_img_path}")
                return None

            answer_image_path = self.resources_dir / "member" / card["assetbundleName"] / answer_image_filename
            if not answer_image_path.exists():
                logger.error(f"预处理的答案图片未找到: {answer_image_path}")
                return None

        character = self.characters_map.get(card["characterId"])
        if not character:
            logger.error(f"未找到ID为 {card['characterId']} 的角色")
            return None

        score_map = {"easy": 1, "normal": 2, "hard": 3}
        base_score = score_map.get(difficulty, 1)
        
        show_rarity_hint = random.choice([True, False])
        show_training_hint = random.choice([True, False])

        if not show_rarity_hint:
            base_score += 1
        if not show_training_hint:
            base_score += 1
        
        # 获取答案卡牌图片路径 (已预先压缩)
        
        return {
            "card": card,
            "difficulty": difficulty,
            "card_state": card_type,
            "question_image_source": self._get_resource_path_or_url(f"questions/{question_img_name}"),
            "character": character,
            "score": base_score,
            "show_rarity_hint": show_rarity_hint,
            "show_training_hint": show_training_hint,
            "answer_image_source": self._get_resource_path_or_url(f'member/{card["assetbundleName"]}/{answer_image_filename}'),
        }

    # --- 指令处理 ---
    @filter.command("猜卡", alias={"guess", "gc","猜卡面"})
    async def start_guess_card(self, event: AstrMessageEvent):
        """开始一轮猜卡游戏"""
        if not self._is_group_allowed(event):
            return
            
        session_id = event.unified_msg_origin

        # --- 锁定会话以防止竞态条件 ---
        if session_id not in self.session_locks:
            self.session_locks[session_id] = asyncio.Lock()
        lock = self.session_locks[session_id]

        async with lock:
            if session_id in self.context.active_game_sessions:
                yield event.plain_result("......有一个正在进行的游戏了呢。")
                return

            cooldown = self.config.get("game_cooldown_seconds", 60)
            last_end_time = self.last_game_end_time.get(session_id, 0)
            time_since_last_game = time.time() - last_end_time

            if time_since_last_game < cooldown:
                remaining_time = cooldown - time_since_last_game
                time_display = f"{remaining_time:.3f}" if remaining_time < 1 else str(int(remaining_time))
                yield event.plain_result(f"嗯......休息 {time_display} 秒再玩吧......")
                return

            if not self._can_play(event.get_sender_id()):
                yield event.plain_result(f"......你今天的游戏次数已达上限（{self.config.get('daily_play_limit', 10)}次），请明天再来吧......")
                return
            
            # 标记游戏会话为活动状态，然后释放锁
            self.context.active_game_sessions.add(session_id)

        try:
            # --- 新增：解析指定角色 ---
            args = event.message_str.strip().split(maxsplit=1)
            target_char_id = None
            target_char_name = ""
            if len(args) > 1:
                char_name_arg = args[1].lower()
                # 优先完全匹配
                if char_name_arg in self.character_name_to_id_map:
                    target_char_id = self.character_name_to_id_map[char_name_arg]
                else:
                    # 模糊匹配
                    for name, char_id in self.character_name_to_id_map.items():
                        if name.startswith(char_name_arg):
                            target_char_id = char_id
                            break
                
                if not target_char_id:
                    yield event.plain_result(f"......没有找到名为 '{args[1]}' 的角色。")
                    return
            # --- 结束 ---

            # 记录游戏开始，并增加该用户的每日游戏次数
            self._record_game_start(event.get_sender_id(), event.get_sender_name())

            # --- 新增：发送统计信标 ---
            asyncio.create_task(self._send_stats_ping("guess_card"))

            game_data = self.start_new_game(character_id=target_char_id)
            if not game_data:
                yield event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误，请联系管理员。")
                return

            # --- V1.1.0 新功能：生成动态答案池图片 ---
            options_img_path = None
            correct_card = game_data['card']
            difficulty = game_data['difficulty']
            show_training_hint = game_data['show_training_hint']
            show_rarity_hint = game_data['show_rarity_hint']

            candidate_pool = []
            if self.guess_cards:
                character_id = correct_card['characterId']
                rarity = correct_card['cardRarityType']

                # 修正后的逻辑：
                # 1. 基础范围是该角色的所有卡牌
                candidate_pool = [c for c in self.guess_cards if c['characterId'] == character_id]
                
                # 2. 如果有星级提示，则将其作为过滤器应用
                if show_rarity_hint:
                    candidate_pool = [c for c in candidate_pool if c['cardRarityType'] == rarity]
            
            options = []
            # 提示决定选项的展示方式
            if show_training_hint:
                # 有状态提示：只显示提示对应的那个状态的缩略图
                state_to_show = game_data['card_state']
                for card in candidate_pool:
                    relative_thumb_path = f"member_thumb/{card['assetbundleName']}_{state_to_show}.png"
                    options.append({'id': card['id'], 'relative_thumb_path': relative_thumb_path})
                random.shuffle(options) # 单独排序
            else:
                # 没有状态提示：显示两种状态的缩略图，并让同一张卡的花前花后相邻
                card_thumb_groups = []
                for card in candidate_pool:
                    group = []
                    relative_normal_path = f"member_thumb/{card['assetbundleName']}_normal.png"
                    group.append({'id': card['id'], 'relative_thumb_path': relative_normal_path})
                    
                    relative_after_path = f"member_thumb/{card['assetbundleName']}_after_training.png"
                    group.append({'id': card['id'], 'relative_thumb_path': relative_after_path})
                    
                    if group:
                        card_thumb_groups.append(group)
                
                # 随机打乱卡牌（组）的顺序，但保持花前花后配对
                random.shuffle(card_thumb_groups)
                # 将分组展开成最终的选项列表
                options = [thumb for group in card_thumb_groups for thumb in group]
            
            if options:
                # 横向最多显示5个，让图片比例协调
                cols = min(len(options), 5)
                options_img_path = await self._create_options_image(options, cols=cols)
            # --- V1.1.0 功能结束 ---

            # 在后台日志中输出答案，方便测试
            logger.info(f"[猜卡插件] 新游戏开始. 答案ID: {game_data['card']['id']}")
                
            hints = []
            if game_data["show_rarity_hint"]:
                rarity_map = {
                    "rarity_3": "⭐⭐⭐", 
                    "rarity_4": "⭐⭐⭐⭐",
                }
                hints.append(f"星级提示: {rarity_map.get(game_data['card']['cardRarityType'], '未知')}")
            
            if game_data["show_training_hint"]:
                state_text = "花后" if game_data["card_state"] == "after_training" else "花前"
                hints.append(f"状态提示: {state_text}")

            timeout_seconds = self.config.get("answer_timeout", 30)
            character_name = game_data["character"]["name"]
            
            # 如果指定了角色，在消息中提示
            if target_char_id:
                intro_text = f".......嗯\n难度: {game_data['difficulty']}，基础分: {game_data['score']}\n这是 {character_name} 的一张卡牌，请在{timeout_seconds}秒内发送卡牌ID进行回答。\n"
            else:
                intro_text = f".......嗯\n难度: {game_data['difficulty']}，基础分: {game_data['score']}\n这是 {character_name} 的一张卡牌，请在{timeout_seconds}秒内发送卡牌ID进行回答。\n"
            
            hint_text = "\n".join(hints) + "\n" if hints else ""
            
            msg_chain: list = [Comp.Plain(intro_text + hint_text)]

            try:
                question_source = game_data.get("question_image_source")
                if question_source:
                    msg_chain.append(Comp.Image(file=str(question_source)))
                
                if options_img_path:
                    msg_chain.append(Comp.Image(file=options_img_path))
                yield event.chain_result(msg_chain)
            except Exception as e:
                logger.error(f"......发送图片失败: {e}. Check if the file path is correct and accessible.")
                yield event.plain_result("......发送问题图片时出错，游戏中断。")
                return

            timeout_seconds = self.config.get("answer_timeout", 30)
            
            # 为当前轮次添加猜测次数计数器
            guess_attempts_count = 0
            max_guess_attempts = self.config.get("max_guess_attempts", 10)
            
            # --- 新增: 游戏状态变量 ---
            game_ended_by_timeout = False
            winner_info = None
            game_ended_by_attempts = False


            @session_waiter(timeout=timeout_seconds)  # type: ignore
            async def guess_waiter(controller: SessionController, answer_event: AstrMessageEvent):
                nonlocal guess_attempts_count, winner_info, game_ended_by_attempts

                answer_text = answer_event.message_str.strip()
                
                # 移除对!前缀的强制要求
                answer_id_str = re.sub(r"^[!！]", "", answer_text)

                if answer_id_str.isdigit():
                    guess_attempts_count += 1
                    try:
                        answer_id = int(answer_id_str)
                        correct_id = game_data["card"]["id"]

                        if answer_id == correct_id:
                            winner_id = answer_event.get_sender_id()
                            winner_name = answer_event.get_sender_name()
                            score = game_data["score"]
                            
                            self._update_stats(winner_id, winner_name, score, correct=True)

                            # 记录胜利者信息，但不立即发送消息
                            winner_info = {"name": winner_name, "id": winner_id, "score": score}

                            controller.stop()
                            return # 回答正确，直接退出
                        else:
                            self._update_stats(answer_event.get_sender_id(), answer_event.get_sender_name(), 0, correct=False)
                    except (ValueError, IndexError):
                        pass

                    # 如果达到猜测次数上限，则结束游戏
                    if guess_attempts_count >= max_guess_attempts:
                        game_ended_by_attempts = True
                        controller.stop()

            try:
                await guess_waiter(event)
            except TimeoutError:
                game_ended_by_timeout = True
            
            # 记录游戏结束时间，无论游戏如何结束
            self.last_game_end_time[session_id] = time.time()

            # --- 统一在游戏结束后公布结果 ---
            correct_id = game_data['card']['id']

            text_msg = []
            if winner_info:
                text_msg.append(Comp.Plain(f"{winner_info['name']} ......回答正确了呢......\n"))
                text_msg.append(Comp.Plain(f"获得 {winner_info['score']} 分......\n答案是: ID {correct_id}\n"))
            elif game_ended_by_attempts:
                text_msg.append(Comp.Plain(f"本轮猜测次数已达上限（{max_guess_attempts}次）......无人答对......\n"))
                text_msg.append(Comp.Plain(f"正确答案是: ID {correct_id}\n"))
            elif game_ended_by_timeout:
                text_msg.append(Comp.Plain("时间到.............好像......没有人答对......\n"))
                text_msg.append(Comp.Plain(f"正确答案是: ID {correct_id}\n"))
            
            if text_msg:
                yield event.chain_result(text_msg)

            # 使用预先处理好的答案图片
            question_source = game_data.get("question_image_source")
            answer_source = game_data.get("answer_image_source")
            image_msg = []
            if question_source: image_msg.append(Comp.Image(file=str(question_source)))
            if answer_source: image_msg.append(Comp.Image(file=str(answer_source)))
            
            if image_msg:
                yield event.chain_result(image_msg)

        finally:
            # 无论游戏如何结束（正常、超时、错误），都确保移除会话标记
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)


    @filter.command("猜卡帮助")
    async def show_guess_card_help(self, event: AstrMessageEvent):
        """显示猜卡插件帮助"""
        if not self._is_group_allowed(event):
            return
        help_text = (
            "--- 猜卡插件帮助 ---\n\n"
            "**基础指令**\n"
            "  `猜卡` - 完全随机猜一张卡\n"
            "  `猜卡 [角色名]` - 猜指定角色的卡 (例如: 猜卡 mfy)\n\n"
            "**数据统计**\n"
            "  `猜卡排行榜` - 查看猜卡总分排行榜\n"
            "  `猜卡分数` - 查看自己的猜卡数据统计\n\n"
            "**管理员指令**\n"
            "  `重置猜卡次数 [用户ID]` - 重置指定用户的每日游戏次数"
        )
        yield event.plain_result(help_text)


    @filter.command("猜卡分数", alias={"gcscore", "我的猜卡分数"})
    async def show_user_score(self, event: AstrMessageEvent):
        """显示玩家自己的猜卡积分和统计数据"""
        if not self._is_group_allowed(event):
            return
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT score, attempts, correct_attempts, last_play_date, daily_plays FROM user_stats WHERE user_id = ?", (user_id,))
            user_data = cursor.fetchone()
            
        if not user_data:
            yield event.plain_result(f"......{user_name}，你还没有参与过猜卡游戏哦。")
            return
            
        score, attempts, correct_attempts, last_play_date, daily_plays = user_data
        accuracy = (correct_attempts * 100 / attempts) if attempts > 0 else 0
        
        # 计算排名
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM user_stats WHERE score > ?", (score,))
            rank = cursor.fetchone()[0] + 1
        
        daily_limit = self.config.get("daily_play_limit", 10)
        remaining_plays = daily_limit - daily_plays if last_play_date == time.strftime("%Y-%m-%d") else daily_limit
        
        stats_text = (
            f"--- {user_name} 的猜卡数据 ---\n"
            f"🏆 总分: {score} 分\n"
            f"🎯 正确率: {accuracy:.1f}%\n"

            f"🎮 游戏次数: {attempts} 次\n"
            f"✅ 答对次数: {correct_attempts} 次\n"
            f"🏅 当前排名: 第 {rank} 名\n"
            f"📅 今日剩余游戏次数: {remaining_plays} 次"
        )
        
        yield event.plain_result(stats_text)


    @filter.command("重置猜卡次数", alias={"resetgl"})
    async def reset_guess_limit(self, event: AstrMessageEvent):
        """重置用户猜卡次数（仅限管理员）"""
        if not self._is_group_allowed(event):
            return

        sender_id = event.get_sender_id()
        super_users = self.config.get("super_users", [])

        if str(sender_id) not in super_users:
            yield event.plain_result("......抱歉，您没有权限使用此指令......")
            return

        # 从消息中解析出可能的目标用户ID
        parts = event.message_str.strip().split()
        target_id = sender_id # 默认为自己
        if len(parts) > 1 and parts[1].isdigit():
            target_id = parts[1]
        
        target_id_str = str(target_id)

        if self._reset_user_limit(target_id_str):
            if target_id_str == sender_id:
                yield event.plain_result("......您的猜卡次数已重置。")
            else:
                yield event.plain_result(f"......用户 {target_id_str} 的猜卡次数已重置。")
        else:
            yield event.plain_result(f"......未找到用户 {target_id_str} 的游戏记录，无法重置。")


    @filter.command("猜卡排行榜", alias={"gcrank", "gctop"})
    async def show_ranking(self, event: AstrMessageEvent):
        """显示猜卡排行榜"""
        if not self._is_group_allowed(event):
            return

        # 每次生成前都清理一次
        self._cleanup_output_dir()

        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, user_name, score, attempts, correct_attempts FROM user_stats ORDER BY score DESC LIMIT 10"
            )
            rows = cursor.fetchall()

        if not rows:
            yield event.plain_result("......目前还没有人参与过猜卡游戏")
            return

        # --- 使用 Pillow 生成图片 ---
        try:
            # 1. 设置参数 (增加高度以容纳所有条目)
            width, height = 650, 950

            # 2. 创建默认的渐变背景
            bg_color_start = (230, 240, 255)
            bg_color_end = (200, 210, 240)
            img = Image.new("RGB", (width, height), bg_color_start)
            draw_bg = ImageDraw.Draw(img)
            for y in range(height):
                r = int(bg_color_start[0] + (bg_color_end[0] - bg_color_start[0]) * y / height)
                g = int(bg_color_start[1] + (bg_color_end[1] - bg_color_start[1]) * y / height)
                b = int(bg_color_start[2] + (bg_color_end[2] - bg_color_start[2]) * y / height)
                draw_bg.line([(0, y), (width, y)], fill=(r, g, b))
            
            # 3. 检查并叠加半透明的自定义背景 (修正：强制从本地加载)
            background_path = self.resources_dir / "ranking_bg.png"
            if background_path.exists():
                try:
                    custom_bg = Image.open(background_path).convert("RGBA")
                    custom_bg = custom_bg.resize((width, height), LANCZOS)
                    
                    # 设置自定义背景的透明度 (0-255)
                    custom_bg.putalpha(128)
                    
                    # 将渐变背景转为RGBA并与自定义背景混合
                    img = img.convert("RGBA")
                    img = Image.alpha_composite(img, custom_bg)

                except Exception as e:
                    logger.warning(f"加载或混合自定义背景图片失败: {e}. 将仅使用默认背景。")

            # 确保图像为RGBA模式以支持透明度
            if img.mode != 'RGBA':
                img = img.convert('RGBA')

            # 3. (新) 叠加一层半透明白色蒙版以提高可读性
            white_overlay = Image.new("RGBA", img.size, (255, 255, 255, 100)) # 调整透明度以获得泛白效果
            img = Image.alpha_composite(img, white_overlay)

            # 4. 设置文本和颜色
            title_text = "猜卡排行榜"
            font_color = (30, 30, 50)
            shadow_color = (180, 180, 190, 128)
            header_color = (80, 90, 120)
            score_color = (235, 120, 20)
            accuracy_color = (0, 128, 128)
            
            # 5. 准备字体
            try:
                font_path = self.resources_dir / "font.ttf"
                title_font = ImageFont.truetype(str(font_path), 48)
                header_font = ImageFont.truetype(str(font_path), 28)
                body_font = ImageFont.truetype(str(font_path), 26)
                id_font = ImageFont.truetype(str(font_path), 16)
                medal_font = ImageFont.truetype(str(font_path), 36) # 为奖牌使用更大的字体
            except IOError:
                logger.error(f"主要字体文件未找到: {font_path}. 将使用默认字体。")
                title_font, header_font, body_font, id_font = [ImageFont.load_default()] * 4
                medal_font = body_font # 如果主字体加载失败，奖牌回退到正文字体

            # 6. 使用 Pilmoji 进行绘制
            with Pilmoji(img) as pilmoji:
                # 绘制标题 (带阴影)
                center_x, title_y = int(width / 2), 80
                pilmoji.text((center_x + 2, title_y + 2), title_text, font=title_font, fill=shadow_color, anchor="mm", emoji_position_offset=(0, 6))
                pilmoji.text((center_x, title_y), title_text, font=title_font, fill=font_color, anchor="mm", emoji_position_offset=(0, 6))

                # 绘制表头
                headers = ["排名", "玩家", "总分", "正确率", "总次数"]
                col_positions_header = [40, 120, 320, 450, 560]
                title_height = pilmoji.getsize(title_text, font=title_font)[1]
                current_y = title_y + int(title_height / 2) + 45
                for header in headers:
                    pilmoji.text((col_positions_header.pop(0), current_y), header, font=header_font, fill=header_color)

                current_y += 55

                # 绘制排行榜数据
                rank_icons = ["🥇", "🥈", "🥉"]
                for i, row in enumerate(rows):
                    user_id, user_name, score, attempts, correct_attempts = str(row[0]), row[1], str(row[2]), str(row[3]), row[4]
                    accuracy = f"{(correct_attempts * 100 / int(attempts) if int(attempts) > 0 else 0):.1f}%"
                    
                    # --- 排名和奖牌对齐修正 ---
                    rank = i + 1
                    col_positions = [40, 120, 320, 450, 560]
                    rank_num_align_x = 100 # 数字右对齐的位置

                    # 绘制排名数字 (恢复之前的右上角对齐)
                    pilmoji.text((rank_num_align_x, current_y), str(rank), font=body_font, fill=font_color, anchor="ra")

                    # 为前三名绘制更大的奖牌 (使用默认的左上角对齐)
                    if i < 3:
                        # 使用更大的字体并微调Y轴位置以使其与数字视觉居中
                        pilmoji.text((col_positions[0], current_y - 2), rank_icons[i], font=medal_font, fill=font_color)
                    
                    max_name_width = col_positions[2] - col_positions[1] - 20
                    if body_font.getbbox(user_name)[2] > max_name_width:
                        while body_font.getbbox(user_name + "...")[2] > max_name_width and len(user_name) > 0:
                            user_name = user_name[:-1]
                        user_name += "..."
                    
                    # 恢复之前的默认对齐方式 (移除所有 anchor)
                    pilmoji.text((col_positions[1], current_y), user_name, font=body_font, fill=font_color)
                    pilmoji.text((col_positions[1], current_y + 32), f"ID: {user_id}", font=id_font, fill=header_color)
                    pilmoji.text((col_positions[2], current_y), score, font=body_font, fill=score_color)
                    pilmoji.text((col_positions[3], current_y), accuracy, font=body_font, fill=accuracy_color)
                    pilmoji.text((col_positions[4], current_y), attempts, font=body_font, fill=font_color)

                    # 绘制分割线
                    separator_y = current_y + 60
                    if i < len(rows) - 1:
                        draw = ImageDraw.Draw(img) # 需要一个普通Draw对象来画线
                        draw.line([(30, separator_y), (width - 30, separator_y)], fill=(200, 200, 210, 128), width=1)
                    
                    current_y += 70

                # 绘制页脚
                footer_text = f"GuessCard v{PLUGIN_VERSION} | Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                footer_y = height - 25
                pilmoji.text((center_x, footer_y), footer_text, font=id_font, fill=header_color, anchor="ms")

            # Pilmoji 上下文管理器会自动处理保存
            # 保存并发送图片
            output_dir = self.plugin_dir / "output"
            os.makedirs(output_dir, exist_ok=True)
            img_path = output_dir / f"ranking_{int(time.time())}.png"
            img.save(img_path)

            yield event.image_result(str(img_path))

        except Exception as e:
            logger.error(f"使用Pillow生成排行榜图片失败: {e}", exc_info=True)
            yield event.plain_result("生成排行榜图片时出错，请联系管理员。")
            
    # --- 数据更新与检查 ---
    def _record_game_start(self, user_id: str, user_name: str):
        """记录一次游戏开始，增加该用户的每日游戏次数"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            today = time.strftime("%Y-%m-%d")

            cursor.execute("SELECT last_play_date, daily_plays FROM user_stats WHERE user_id = ?", (user_id,))
            user_data = cursor.fetchone()

            if user_data:
                last_play_date, daily_plays = user_data
                if last_play_date == today:
                    new_daily_plays = daily_plays + 1
                else:
                    new_daily_plays = 1
                
                cursor.execute(
                    "UPDATE user_stats SET user_name = ?, last_play_date = ?, daily_plays = ? WHERE user_id = ?",
                    (user_name, today, new_daily_plays, user_id)
                )
            else:
                # 如果用户首次游戏，为其创建记录
                cursor.execute(
                    "INSERT INTO user_stats (user_id, user_name, last_play_date, daily_plays) VALUES (?, ?, ?, ?)",
                    (user_id, user_name, today, 1)
                )
            conn.commit()

    def _update_stats(self, user_id: str, user_name: str, score: int, correct: bool):
        """更新用户的得分和总尝试次数统计"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT score, attempts, correct_attempts FROM user_stats WHERE user_id = ?", (user_id,))
            user_data = cursor.fetchone()

            if user_data:
                new_score = user_data[0] + score
                new_attempts = user_data[1] + 1
                new_correct = user_data[2] + (1 if correct else 0)

                cursor.execute(
                    """
                    UPDATE user_stats 
                    SET score = ?, attempts = ?, correct_attempts = ?, user_name = ?
                    WHERE user_id = ?
                    """,
                    (new_score, new_attempts, new_correct, user_name, user_id),
                )
            else:
                # 如果一个未开始过游戏的用户直接回答，也为他创建记录，但每日游戏次数为0
                today = time.strftime("%Y-%m-%d")
                cursor.execute(
                    "INSERT INTO user_stats (user_id, user_name, score, attempts, correct_attempts, last_play_date, daily_plays) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, user_name, score, 1, 1 if correct else 0, today, 0),
                )
            conn.commit()

    def _can_play(self, user_id: str) -> bool:
        """检查用户今天是否还能玩"""
        daily_limit = self.config.get("daily_play_limit", 10)
        with self.get_conn() as conn:
            cursor = conn.cursor()
            today = time.strftime("%Y-%m-%d")
            cursor.execute("SELECT daily_plays, last_play_date FROM user_stats WHERE user_id = ?", (user_id,))
            user_data = cursor.fetchone()
            if user_data and user_data[1] == today:
                return user_data[0] < daily_limit
            return True

    def _reset_user_limit(self, user_id: str) -> bool:
        """重置指定用户的每日游戏次数"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM user_stats WHERE user_id = ?", (user_id,))
            if cursor.fetchone():
                cursor.execute("UPDATE user_stats SET daily_plays = 0 WHERE user_id = ?", (user_id,))
                conn.commit()
                return True
            return False

    async def terminate(self):
        """插件卸载或停用时调用"""
        logger.info("正在关闭猜卡插件的后台任务...")
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("aiohttp session已关闭。")
        logger.info("猜卡插件已终止。")
        pass