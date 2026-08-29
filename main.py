import asyncio
import json
import random
import re
import time
import os
import sqlite3
import io
from contextlib import closing
from pathlib import Path
from typing import Optional, Union
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from datetime import datetime
from pilmoji import Pilmoji
from urllib.parse import urlparse, quote

# 尝试导入 aiohttp，作为可选依赖
try:
    import aiohttp
except ImportError:
    aiohttp = None


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
    from astrbot.core.utils.session_waiter import session_waiter, SessionController, SessionFilter
    from astrbot.api import AstrBotConfig
except ImportError:
    # Fallback for older versions or different project structures.
    logger.error("Failed to import from astrbot.api, attempting fallback. This may indicate an old version of AstrBot.")
    from astrbot.core.plugin import Plugin as Star, Context, register, filter, AstrMessageEvent  # type: ignore
    import astrbot.core.message_components as Comp  # type: ignore
    from astrbot.core.utils.session_waiter import session_waiter, SessionController, SessionFilter  # type: ignore
    # Fallback for StarTools if it's missing in older versions
    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str) -> Path:
            # Provide a fallback implementation that mimics the original get_db_path logic
            # This path is relative to the directory containing the 'plugins' folder
            return Path(__file__).parent.parent.parent.parent / 'data' / 'plugins_data' / plugin_name


try:
    from .master_data_service import SERVER_JP, SERVER_SC, MasterDataService
except ImportError:  # 直接以脚本方式加载（单测）时使用绝对导入
    from master_data_service import SERVER_JP, SERVER_SC, MasterDataService



# --- 插件元数据 ---
PLUGIN_NAME = "pjsk_guess_card"
PLUGIN_AUTHOR = "慵懒午睡"
PLUGIN_DESCRIPTION = "PJSK猜卡面插件"
PLUGIN_VERSION = "2.0.0"
PLUGIN_REPO_URL = "https://github.com/yonglanws/astrbot_plugin_pjsk_guess_card"
DEFAULT_PLATFORM_NAME = "aiocqhttp"
OFFICIAL_PLATFORM_NAME = "qq_official"
OFFICIAL_QID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")

# --- 题库服务器与官机 markdown 机制（与 PJSK Wordle 保持一致） ---
SERVER_JP = "jp"
SERVER_SC = "sc"
SERVER_LABELS = {SERVER_JP: "日服", SERVER_SC: "国服"}
SERVER_BADGES = {SERVER_JP: "日服题库", SERVER_SC: "国服题库"}
# 结算尾部显示的切换指令（当前日服 → 提示切换国服）
SWITCH_COMMANDS = {SERVER_JP: "猜卡面切换国服题库", SERVER_SC: "猜卡面切换日服题库"}
# 结算连接入口使用的指令名（与注册指令别名一一对应）
CONNECT_SWITCH_COMMANDS = {SERVER_JP: "猜卡面切换国服题库", SERVER_SC: "猜卡面切换日服题库"}

# 指令连接的默认 markdown 模板：QQ 官方机器人 markdown 消息的参数指令标签
# （见 bot.q.qq.com/wiki 的 markdown / text-chain 文档）。
# text 只放指令本身：QQ 客户端在群聊发送时会自动 @ 官方机器人，
# 拼进 "@id" 反而会出现双重 @。
DEFAULT_CONNECT_TEMPLATE = '<qqbot-cmd-input text="{encoded_command}" show="{encoded_name}" />'
# 旧版本默认模板特征：命中即视为未自定义，自动升级到新默认模板
_LEGACY_TEMPLATE_MARKERS = ("{encoded_at_text}", "mqqapi://")

# 快捷入口：所有 PJSK 娱乐插件的触发指令，Wordle 固定排最后
_QUICK_ENTRIES = ["猜歌", "猜曲绘", "猜卡面", "歌词猜曲", "Wordle"]


class BindingSessionFilter(SessionFilter):
    """只接收发起绑定的同一用户在同一会话中的确认消息。"""

    def __init__(self, session_id: str, user_id: str):
        self.session_id = str(session_id)
        self.user_id = str(user_id)

    def filter(self, event: AstrMessageEvent) -> str:
        if (
            str(event.unified_msg_origin) != self.session_id
            or str(event.get_sender_id()) != self.user_id
        ):
            return ""
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


# --- 数据库管理 ---
def get_db_path(context: Context, plugin_dir: Path) -> str:
    """获取数据库文件的路径，确保它在插件的数据目录中"""
    plugin_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
    os.makedirs(plugin_data_dir, exist_ok=True)
    return str(plugin_data_dir / "guess_card_data.db")


def init_db(db_path: str):
    """初始化数据库和表，支持数据库升级"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id TEXT PRIMARY KEY,
                user_name TEXT,
                custom_name TEXT,
                score INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                correct_attempts INTEGER DEFAULT 0,
                last_play_date TEXT,
                daily_plays INTEGER DEFAULT 0
            )
            """
        )
        # 检查并添加 custom_name 列（用于数据库升级）
        cursor.execute("PRAGMA table_info(user_stats)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'custom_name' not in columns:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN custom_name TEXT")
        if 'platform_name' not in columns:
            cursor.execute(
                "ALTER TABLE user_stats ADD COLUMN platform_name TEXT NOT NULL DEFAULT 'aiocqhttp'"
            )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS account_bindings (
                official_platform TEXT NOT NULL,
                official_user_id TEXT NOT NULL,
                qq_user_id TEXT NOT NULL,
                bound_at TEXT NOT NULL,
                PRIMARY KEY (official_platform, official_user_id)
            )
        """)
        # 旧版本没有平台字段，只能根据 QQ 官方机器人的 32 位 QID 进行一次性识别。
        legacy_qids = cursor.execute(
            "SELECT user_id FROM user_stats WHERE platform_name = ?",
            (DEFAULT_PLATFORM_NAME,),
        ).fetchall()
        for (legacy_user_id,) in legacy_qids:
            if OFFICIAL_QID_PATTERN.fullmatch(str(legacy_user_id)):
                cursor.execute(
                    "UPDATE user_stats SET platform_name = ? WHERE user_id = ?",
                    (OFFICIAL_PLATFORM_NAME, legacy_user_id),
                )
        conn.commit()

# --- 卡牌数据加载 ---
def load_card_data(resources_dir: Path) -> tuple[Optional[list[dict]], Optional[dict]]:
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


# --- 性能优化工具 ---
class LRUCache:
    """简单的LRU缓存实现，用于缓存常用资源"""
    
    def __init__(self, max_size: int = 50):
        self.cache = OrderedDict()
        self.max_size = max_size
    
    def get(self, key: str) -> Optional[any]:
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]
    
    def set(self, key: str, value: any) -> None:
        if key in self.cache:
            self.cache.move_to_end(key)
        else:
            if len(self.cache) >= self.max_size:
                self.cache.popitem(last=False)
        self.cache[key] = value
    
    def clear(self) -> None:
        self.cache.clear()


# --- 游戏状态管理 ---
class GameSession:
    """管理单个游戏会话的所有状态，确保会话间完全隔离"""

    def __init__(self):
        self.guess_attempts_count = 0
        self.game_ended_by_timeout = False
        self.game_ended_by_attempts = False
        self.game_ended_by_quit = False
        self.winner_info = None
        self.user_stats_recorded = set()
        self.game_data = None


# --- 图片效果处理系统 ---
class ImageEffectProcessor:
    """图片效果处理器类，实现多种图片处理效果算法"""
    
    EFFECT_NAMES = {
        "light_blur": "轻度模糊",
        "heavy_blur": "重度模糊",
        "shuffle_blocks_easy": "分块打乱(简易)",
        "shuffle_blocks_hard": "分块打乱(困难)",
        "horizontal_slice": "横向切割",
        "vertical_slice": "纵向切割",
        "crop_area": "截取区域",
        "two_strips": "两长条截取",
        "three_strips": "三长条截取",
    }
    
    EFFECT_PARAMS = {
        "light_blur": ["blur_radius"],
        "heavy_blur": ["blur_radius"],
        "shuffle_blocks_easy": ["block_size"],
        "shuffle_blocks_hard": ["block_size"],
        "horizontal_slice": ["slice_count"],
        "vertical_slice": ["slice_count"],
        "crop_area": ["crop_ratio"],
        "two_strips": [],
        "three_strips": [],
    }
    
    EFFECT_NAME_TO_KEY = {v: k for k, v in EFFECT_NAMES.items()}
    
    COMBINATIONS = {}
    
    def __init__(self, config=None):
        """初始化效果处理器，从配置构建效果设置"""
        self.EFFECTS = {}
        effects_config = config.get("effects", {}) if config else {}
        logger.info(f"加载效果配置: {effects_config}")
        
        for effect_name in self.EFFECT_NAMES:
            effect_cfg = effects_config.get(effect_name, {})
            self.EFFECTS[effect_name] = {
                "name": self.EFFECT_NAMES[effect_name],
                "enabled": effect_cfg.get("enabled", True),
                "difficulty": effect_cfg.get("difficulty", 1),
            }
            for param in self.EFFECT_PARAMS.get(effect_name, []):
                if param in effect_cfg:
                    self.EFFECTS[effect_name][param] = effect_cfg[param]
        
        logger.info(f"最终效果配置: {self.EFFECTS}")
    
    def get_enabled_effects(self):
        """获取所有启用的效果列表"""
        return [k for k, v in self.EFFECTS.items() if v["enabled"]]
    
    def calculate_difficulty(self, effect_names):
        """计算效果组合的综合分数"""
        if not effect_names:
            return 1
        
        total = 0
        count = 0
        for name in effect_names:
            if name in self.EFFECTS:
                total += self.EFFECTS[name]["difficulty"]
                count += 1
        
        if count == 0:
            return 1
        
        avg = total / count
        return min(5, max(1, round(avg)))
    
    @classmethod
    def apply_light_blur(cls, img, radius=8):
        """应用轻度模糊效果"""
        return img.filter(ImageFilter.GaussianBlur(radius=radius))
    
    @classmethod
    def apply_heavy_blur(cls, img, radius=25):
        """应用重度模糊效果"""
        return img.filter(ImageFilter.GaussianBlur(radius=radius))
    
    @classmethod
    def apply_shuffle_blocks(cls, img, block_size=50):
        """应用分块打乱效果，确保无空白块"""
        try:
            w, h = img.size
            result = Image.new(img.mode, (w, h))

            # 创建网格位置列表
            positions = []
            for y in range(0, h, block_size):
                for x in range(0, w, block_size):
                    positions.append((x, y))

            # 打乱位置顺序
            shuffled_positions = positions.copy()
            random.shuffle(shuffled_positions)

            # 为每个原始位置分配一个新的打乱位置
            for idx, (orig_x, orig_y) in enumerate(positions):
                # 计算当前区块的实际大小
                current_w = min(block_size, w - orig_x)
                current_h = min(block_size, h - orig_y)
                
                # 从原始位置切割区块
                block = img.crop((orig_x, orig_y, orig_x + current_w, orig_y + current_h))
                
                # 获取打乱后的新位置
                new_x, new_y = shuffled_positions[idx]
                
                # 确保新位置也有对应的区块大小
                new_w = min(block_size, w - new_x)
                new_h = min(block_size, h - new_y)
                
                # 如果大小不一致，需要调整区块大小
                if current_w != new_w or current_h != new_h:
                    try:
                        block = block.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    except AttributeError:
                        block = block.resize((new_w, new_h), Image.LANCZOS)
                
                # 将区块粘贴到新位置
                result.paste(block, (new_x, new_y))

            return result
        except Exception as e:
            logger.error(f"分块打乱处理失败: {e}", exc_info=True)
            return img
    
    @classmethod
    def apply_horizontal_slice(cls, img, slice_count=8):
        """应用横向切割效果：将图片横向切割为多个等宽长条并随机打乱重排"""
        try:
            w, h = img.size
            slice_height = h // slice_count
            slices = []

            # 切割图片为多个横向长条
            for i in range(slice_count):
                y_start = i * slice_height
                # 最后一个长条可能高度不同，确保覆盖整个图片
                y_end = (i + 1) * slice_height if i < slice_count - 1 else h
                slice_img = img.crop((0, y_start, w, y_end))
                slices.append(slice_img)

            # 随机打乱长条顺序
            random.shuffle(slices)
            
            # 合并长条为一张新图片
            result = Image.new(img.mode, (w, h))
            y_offset = 0
            for slice_img in slices:
                result.paste(slice_img, (0, y_offset))
                y_offset += slice_img.size[1]
            
            return result
        except Exception as e:
            logger.error(f"横向切割处理失败: {e}", exc_info=True)
            return img
    
    @classmethod
    def apply_vertical_slice(cls, img, slice_count=8):
        """应用纵向切割效果：将图片纵向切割为多个等高长条并随机打乱重排"""
        try:
            w, h = img.size
            slice_width = w // slice_count
            slices = []
            
            # 切割图片为多个纵向长条
            for i in range(slice_count):
                x_start = i * slice_width
                # 最后一个长条可能宽度不同，确保覆盖整个图片
                x_end = (i + 1) * slice_width if i < slice_count - 1 else w
                slice_img = img.crop((x_start, 0, x_end, h))
                slices.append(slice_img)
            
            # 随机打乱长条顺序
            random.shuffle(slices)
            
            # 合并长条为一张新图片
            result = Image.new(img.mode, (w, h))
            x_offset = 0
            for slice_img in slices:
                result.paste(slice_img, (x_offset, 0))
                x_offset += slice_img.size[0]
            
            return result
        except Exception as e:
            logger.error(f"纵向切割处理失败: {e}", exc_info=True)
            return img
    
    @classmethod
    def apply_crop_area(cls, img, crop_ratio=0.5):
        """随机截取图片中的一部分区域"""
        try:
            w, h = img.size
            crop_w = max(int(w * crop_ratio), 50)
            crop_h = max(int(h * crop_ratio), 50)
            
            max_x = w - crop_w
            max_y = h - crop_h
            x = random.randint(0, max_x) if max_x > 0 else 0
            y = random.randint(0, max_y) if max_y > 0 else 0
            
            return img.crop((x, y, x + crop_w, y + crop_h))
        except Exception as e:
            logger.error(f"截取区域处理失败: {e}", exc_info=True)
            return img
    
    @classmethod
    def apply_two_strips(cls, img):
        """随机截取两个高度一致的等宽长条并合成矩形，中间有白色分界线"""
        try:
            w, h = img.size
            strip_h = random.randint(max(int(h * 0.1), 25), max(int(h * 0.35), 35))
            strip_h = min(strip_h, h // 2 - 1)
            strip_w = random.randint(max(int(w * 0.5), 60), max(int(w * 0.85), 80))
            
            possible_ys = list(range(0, h - strip_h + 1))
            random.shuffle(possible_ys)
            
            found = []
            for y in possible_ys:
                if not any(y < ey + strip_h and y + strip_h > ey for ey in found):
                    found.append(y)
                if len(found) == 2:
                    break
            
            if len(found) < 2:
                return img
            
            found.sort()
            
            divider_h = 3
            total_h = strip_h * 2 + divider_h
            result = Image.new(img.mode, (strip_w, total_h))
            
            y_offset = 0
            for i, orig_y in enumerate(found):
                max_x = w - strip_w
                x = random.randint(0, max_x) if max_x > 0 else 0
                strip = img.crop((x, orig_y, x + strip_w, orig_y + strip_h))
                result.paste(strip, (0, y_offset))
                y_offset += strip_h
                if i < len(found) - 1:
                    draw = ImageDraw.Draw(result)
                    draw.rectangle([(0, y_offset), (strip_w - 1, y_offset + divider_h - 1)], fill=(255, 255, 255))
                    y_offset += divider_h
            
            return result
        except Exception as e:
            logger.error(f"两长条截取处理失败: {e}", exc_info=True)
            return img
    
    @classmethod
    def apply_three_strips(cls, img):
        """随机截取三个高度一致的等宽长条并合成矩形，中间有白色分界线"""
        try:
            w, h = img.size
            strip_h = random.randint(max(int(h * 0.08), 20), max(int(h * 0.2), 28))
            strip_h = min(strip_h, h // 3 - 1)
            strip_w = random.randint(max(int(w * 0.5), 60), max(int(w * 0.85), 80))
            
            possible_ys = list(range(0, h - strip_h + 1))
            random.shuffle(possible_ys)
            
            found = []
            for y in possible_ys:
                if not any(y < ey + strip_h and y + strip_h > ey for ey in found):
                    found.append(y)
                if len(found) == 3:
                    break
            
            if len(found) < 3:
                return img
            
            found.sort()
            
            divider_h = 3
            total_h = strip_h * 3 + divider_h * 2
            result = Image.new(img.mode, (strip_w, total_h))
            
            y_offset = 0
            for i, orig_y in enumerate(found):
                max_x = w - strip_w
                x = random.randint(0, max_x) if max_x > 0 else 0
                strip = img.crop((x, orig_y, x + strip_w, orig_y + strip_h))
                result.paste(strip, (0, y_offset))
                y_offset += strip_h
                if i < len(found) - 1:
                    draw = ImageDraw.Draw(result)
                    draw.rectangle([(0, y_offset), (strip_w - 1, y_offset + divider_h - 1)], fill=(255, 255, 255))
                    y_offset += divider_h
            
            return result
        except Exception as e:
            logger.error(f"三长条截取处理失败: {e}", exc_info=True)
            return img
    
    def apply_effect(self, img, effect_name, **kwargs):
        """应用指定的图片效果"""
        if effect_name == "none":
            return img
        elif effect_name == "light_blur":
            radius = kwargs.get("blur_radius", self.EFFECTS["light_blur"]["blur_radius"])
            return self.apply_light_blur(img, radius)
        elif effect_name == "heavy_blur":
            radius = kwargs.get("blur_radius", self.EFFECTS["heavy_blur"]["blur_radius"])
            return self.apply_heavy_blur(img, radius)
        elif effect_name == "shuffle_blocks_easy":
            block_size = kwargs.get("block_size", self.EFFECTS["shuffle_blocks_easy"]["block_size"])
            return self.apply_shuffle_blocks(img, block_size)
        elif effect_name == "shuffle_blocks_hard":
            block_size = kwargs.get("block_size", self.EFFECTS["shuffle_blocks_hard"]["block_size"])
            return self.apply_shuffle_blocks(img, block_size)
        elif effect_name == "horizontal_slice":
            slice_count = kwargs.get("slice_count", self.EFFECTS["horizontal_slice"]["slice_count"])
            return self.apply_horizontal_slice(img, slice_count)
        elif effect_name == "vertical_slice":
            slice_count = kwargs.get("slice_count", self.EFFECTS["vertical_slice"]["slice_count"])
            return self.apply_vertical_slice(img, slice_count)
        elif effect_name == "crop_area":
            crop_ratio = kwargs.get("crop_ratio", self.EFFECTS["crop_area"]["crop_ratio"])
            return self.apply_crop_area(img, crop_ratio)
        elif effect_name == "two_strips":
            return self.apply_two_strips(img)
        elif effect_name == "three_strips":
            return self.apply_three_strips(img)
        return img
    
    def apply_effects(self, img, effect_names):
        """应用多个图片效果"""
        result = img.copy()
        for name in effect_names:
            if name in self.EFFECTS:
                result = self.apply_effect(result, name)
        return result
    
    def random_effect(self):
        """随机选择一个效果"""
        enabled = self.get_enabled_effects()
        logger.info(f"启用的效果列表: {enabled}")
        return random.choice(enabled)
    
    def random_effect_combination(self):
        """随机选择一个效果组合"""
        if self.COMBINATIONS and random.random() < 0.3:
            combo_key = random.choice(list(self.COMBINATIONS.keys()))
            combo = self.COMBINATIONS[combo_key]
            return combo["effects"], combo["name"]
        else:
            effect = self.random_effect()
            return [effect], self.EFFECTS[effect]["name"]


# --- 核心插件类 ---
@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESCRIPTION, PLUGIN_VERSION, PLUGIN_REPO_URL)
class GuessCardPlugin(Star):  # type: ignore
    REMOTE_IMAGE_MAX_ATTEMPTS = 3
    REMOTE_IMAGE_RETRY_DELAY = 0.5

    def __init__(self, context: Context, config: 'AstrBotConfig'):
        super().__init__(context)
        self.config = config
        self.plugin_dir = Path(os.path.dirname(__file__))
        self.resources_dir = self.plugin_dir / "resources"
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.output_dir = self.data_dir / "output"
        os.makedirs(self.output_dir, exist_ok=True)
        self.db_path = get_db_path(context, self.plugin_dir)
        init_db(self.db_path)
        self.guess_cards, self.characters_map = load_card_data(self.resources_dir)
        self.last_game_end_time = {} # 存储每个会话的最后游戏结束时间
        self.http_session = None

        # 为每个会话的游戏启动过程添加锁，防止竞态条件
        self.session_locks = {}

        # 使用插件自身的游戏会话状态，而不是共享的context
        self.active_game_sessions = set()

        # 游戏会话状态管理，按session_id存储GameSession实例
        self.game_sessions = {}

        # 自动猜卡面会话管理，存储启用了自动猜卡面的 session_id 及连续无人回答次数
        self.auto_game_sessions: dict[str, int] = {}  # session_id -> consecutive_no_answer_count
        
        # 图片处理缓存，提高重复图片处理性能
        self.image_cache = LRUCache(max_size=30)
        
        # 卡面路径缓存，避免重复构造路径
        self.card_path_cache = LRUCache(max_size=100)
        
        # 初始化图片效果处理器，从配置读取效果设置
        logger.info(f"完整配置: {dict(self.config)}")
        self.effect_processor = ImageEffectProcessor(self.config)
        
        # 添加会话初始化锁，防止并发创建多个 aiohttp ClientSession
        self.session_lock = asyncio.Lock()

        if not self.guess_cards or not self.characters_map:
            logger.error("插件初始化失败，缺少必要的卡牌数据文件。插件功能将受限。")
        
        if not aiohttp:
            logger.warning("`aiohttp` 模块未安装，远程图片功能将受限或性能较差。建议安装: pip install aiohttp")

        self._cleanup_task = None
        self._background_tasks: set = set()

        # --- 题库服务器偏好与 master 数据自动同步 ---
        self.server_prefs_path = self.data_dir / "session_servers.json"
        self.server_prefs: dict = self._load_server_prefs()
        self.master_data = MasterDataService(
            self.data_dir,
            update_interval_hours=int(config.get("update_interval_hours", 24)),
        )
        self.master_data.on_cards_updated = self._on_cards_updated

        self._load_fonts()
        self._build_valid_answers_set()

        # 统一白名单类型为字符串集合
        self._normalize_group_whitelist()

        # 统一黑名单类型为字符串集合
        self._normalize_blacklist()

        self._cleanup_output_dir()
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task())
        self._track_task(self._cleanup_task)
        self._master_task = asyncio.create_task(self._start_master_data())

    async def _start_master_data(self):
        """启动卡牌题库自动同步服务。"""
        try:
            await self.master_data.start()
        except Exception as e:
            logger.error(f"卡牌题库同步服务启动失败: {e}", exc_info=True)

    def _on_cards_updated(self):
        """题库更新后的回调（当前卡池在开局时按会话实时读取，无需额外重建）。"""
        logger.info("[猜卡插件] 卡牌题库已随 master 数据更新。")

    # --- 题库服务器偏好 ---

    def _load_server_prefs(self) -> dict:
        try:
            if self.server_prefs_path.exists():
                return json.loads(self.server_prefs_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"加载题库服务器偏好失败: {e}")
        return {}

    def _save_server_prefs(self):
        try:
            self.server_prefs_path.write_text(
                json.dumps(self.server_prefs, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存题库服务器偏好失败: {e}")

    def _server_for_session(self, session_id: str) -> str:
        saved = self.server_prefs.get(session_id)
        if saved in (SERVER_JP, SERVER_SC):
            return saved
        default = str(self.config.get("default_server", SERVER_JP)).lower()
        return SERVER_SC if default == SERVER_SC else SERVER_JP

    def _get_cards_for_session(self, session_id: str) -> Optional[list[dict]]:
        """获取会话当前服务器的卡池；master 题库未就绪时回退内置卡池（日服）。"""
        server = self._server_for_session(session_id)
        cards = self.master_data.get_cards(server)
        if cards:
            return cards
        return self.guess_cards

    # --- 官机 markdown 机制（与 PJSK Wordle 一致） ---

    def _build_connect_link(self, command: str, self_id: str, show: str | None = None) -> str:
        """按配置模板生成 markdown 格式的指令连接。

        默认使用 QQ 官方机器人 markdown 消息的参数指令标签
        <qqbot-cmd-input>：点击后在聊天框填入指令，QQ 客户端发送时会自动 @ 官方机器人。
        show 为展示名（默认与指令一致）。可通过 connect_link_template 配置项适配环境，
        模板占位符：{name}=指令显示名 {command}=指令原文 {self_id}=官机 ID
        {at_text}=@官机+指令原文 {encoded_name}/{encoded_command}/{encoded_at_text}=对应 URL 编码。
        模板显式置空则退回纯文本"（连接：@官机 指令）"。
        """
        template = self.config.get("connect_link_template")
        if template is None or any(marker in str(template) for marker in _LEGACY_TEMPLATE_MARKERS):
            # 未配置，或为旧版本默认模板时，使用新默认模板
            template = DEFAULT_CONNECT_TEMPLATE
        template = str(template).strip()
        if not template:
            return f"（连接：@{self_id} {command}）"
        display = show or command
        at_text = f"@{self_id} {command}"
        return template.format(
            name=command,
            command=command,
            self_id=self_id,
            at_text=at_text,
            encoded_command=quote(command, safe=""),
            encoded_name=quote(display, safe=""),
            encoded_at_text=quote(at_text, safe=""),
        )

    def _get_official_self_id(self, event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "self_id", "") or "").strip()

    async def _send_markdown_text(self, event: AstrMessageEvent, text: str):
        """以 QQ 官方机器人 markdown 消息发送纯文本（含连接标签）。"""
        result = event.make_result()
        result.chain = [Comp.Plain(text)]
        result.use_markdown(True)
        await event.send(result)

    def _build_server_footer(self, event: AstrMessageEvent, server: str) -> list:
        """构建结算消息的题库服务器尾部：官机附 markdown 连接入口与快捷入口，普通 QQ 仅提示指令。"""
        other = SERVER_SC if server == SERVER_JP else SERVER_JP
        switch_cmd = SWITCH_COMMANDS[server]  # 当前日服 → 猜卡面切换国服题库
        connect_switch_cmd = CONNECT_SWITCH_COMMANDS[server]
        lines = [f"本局题库服务器：{SERVER_LABELS[server]}"]

        if self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME:
            self_id = self._get_official_self_id(event)
            if self_id:
                lines.append(self._build_connect_link(connect_switch_cmd, self_id))
                # 切换指令下方：绑定 / 查分 / 排行榜连接
                account_links = ["猜卡面绑定QQ", "猜卡面个人分数", "猜卡面排行榜"]
                lines.append(
                    "  ".join(self._build_connect_link(name, self_id) for name in account_links)
                )
                if _QUICK_ENTRIES:
                    lines.append("快捷入口：")
                    lines.append(
                        "  ".join(self._build_connect_link(name, self_id) for name in _QUICK_ENTRIES)
                    )
                return lines
        # 普通号（或拿不到官机 self_id 时的兜底）
        lines.append(f"你可以使用{switch_cmd}指令切换{SERVER_LABELS[other]}题库。")
        return lines

    async def _switch_server(self, event: AstrMessageEvent, server: str):
        """切换当前会话的题库服务器。"""
        session_id = event.unified_msg_origin
        if session_id in self.active_game_sessions:
            await event.send(event.plain_result("本局游戏还在进行中，结束后再切换题库服务器吧。"))
            return
        current = self._server_for_session(session_id)
        if current == server:
            await event.send(event.plain_result(f"当前题库已经是{SERVER_LABELS[server]}题库了。"))
            return
        self.server_prefs[session_id] = server
        self._save_server_prefs()
        count = self.master_data.get_card_count(server)
        version = self.master_data.get_version(server)
        await event.send(
            event.plain_result(
                f"已切换为{SERVER_BADGES[server]}（共 {count} 张，版本 {version}），下一局生效。"
            )
        )

    def _load_fonts(self):
        """预加载字体文件，避免重复IO"""
        font_path = self.resources_dir / "font.ttf"
        try:
            self.title_font = ImageFont.truetype(str(font_path), 48)
            self.header_font = ImageFont.truetype(str(font_path), 28)
            self.body_font = ImageFont.truetype(str(font_path), 26)
            self.id_font = ImageFont.truetype(str(font_path), 16)
            self.medal_font = ImageFont.truetype(str(font_path), 36)
        except IOError:
            logger.error(f"主要字体文件未找到: {font_path}. 将使用默认字体。")
            default_font = ImageFont.load_default()
            self.title_font = default_font
            self.header_font = default_font
            self.body_font = default_font
            self.id_font = default_font
            self.medal_font = default_font

    def _track_task(self, task: asyncio.Task):
        """跟踪后台任务，防止被GC回收"""
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _build_valid_answers_set(self):
        """
        构建所有有效的角色名称和别名的集合，用于快速验证用户输入
        """
        self.valid_answers = set()
        if not self.characters_map:
            return
        
        for character in self.characters_map.values():
            # 添加英文缩写
            if character.get("name"):
                self.valid_answers.add(character["name"].lower())
            # 添加中文名称
            if character.get("fullNameChinese"):
                self.valid_answers.add(character["fullNameChinese"].lower())
            # 添加所有别名
            aliases = character.get("aliases", [])
            for alias in aliases:
                self.valid_answers.add(alias.lower())

    def _normalize_group_whitelist(self):
        """统一白名单类型为字符串集合"""
        whitelist = self.config.get("group_whitelist", [])
        self.group_whitelist = {str(x) for x in whitelist}

    def _normalize_blacklist(self):
        """统一黑名单类型为字符串集合"""
        blacklist = self.config.get("blacklist", [])
        self.blacklist = {str(x) for x in blacklist}

    def _is_user_blacklisted(self, user_id: str) -> bool:
        """检查用户是否在黑名单中"""
        # 重新读取配置以支持热加载
        self._normalize_blacklist()
        return str(user_id) in self.blacklist

    @staticmethod
    def _normalize_platform_name(platform_name: Optional[str]) -> str:
        return str(platform_name or DEFAULT_PLATFORM_NAME).strip().lower()

    @staticmethod
    def _get_event_platform_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        try:
            platform_name = getter() if callable(getter) else DEFAULT_PLATFORM_NAME
        except Exception:
            platform_name = DEFAULT_PLATFORM_NAME
        return str(platform_name or DEFAULT_PLATFORM_NAME).strip().lower()

    def _is_qq_official_event(self, event: AstrMessageEvent) -> bool:
        return self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME

    def _resolve_account_id(self, platform_name: str, user_id: str) -> str:
        platform = self._normalize_platform_name(platform_name)
        raw_user_id = str(user_id)
        if platform != OFFICIAL_PLATFORM_NAME:
            return raw_user_id
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            row = conn.execute(
                "SELECT qq_user_id FROM account_bindings "
                "WHERE official_platform = ? AND official_user_id = ?",
                (platform, raw_user_id),
            ).fetchone()
        return str(row[0]) if row else raw_user_id

    def _get_account_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        platform_name = self._get_event_platform_name(event)
        raw_user_id = str(event.get_sender_id())
        resolved_user_id = self._resolve_account_id(platform_name, raw_user_id)
        if resolved_user_id != raw_user_id:
            return resolved_user_id, DEFAULT_PLATFORM_NAME
        return raw_user_id, platform_name

    def _is_event_user_blacklisted(self, event: AstrMessageEvent, canonical_user_id: Optional[str] = None) -> bool:
        raw_user_id = str(event.get_sender_id())
        return self._is_user_blacklisted(raw_user_id) or (
            canonical_user_id is not None and self._is_user_blacklisted(canonical_user_id)
        )

    @staticmethod
    def _build_binding_confirmation_message(qq_user_id: str) -> str:
        return (
            f"你确认将账号绑定至  {qq_user_id} ？官方机作答的分数将迁移至该账号。\n"
            "发送“确认”将开始绑定。发送“取消”将取消绑定。"
        )

    def _bind_official_account(self, official_user_id: str, qq_user_id: str) -> bool:
        source_id = str(official_user_id).strip()
        target_id = str(qq_user_id).strip()
        if (
            not source_id
            or not re.fullmatch(r"\d{5,12}", target_id)
            or source_id == target_id
        ):
            return False

        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.cursor()
                if cursor.execute(
                    "SELECT 1 FROM account_bindings WHERE official_platform = ? AND official_user_id = ?",
                    (OFFICIAL_PLATFORM_NAME, source_id),
                ).fetchone():
                    conn.rollback()
                    return False

                source_row = cursor.execute(
                    "SELECT user_name, custom_name, score, attempts, correct_attempts, "
                    "last_play_date, daily_plays FROM user_stats "
                    "WHERE user_id = ? AND platform_name = ?",
                    (source_id, OFFICIAL_PLATFORM_NAME),
                ).fetchone()
                target_row = cursor.execute(
                    "SELECT user_name, custom_name, score, attempts, correct_attempts, "
                    "last_play_date, daily_plays FROM user_stats "
                    "WHERE user_id = ? AND platform_name = ?",
                    (target_id, DEFAULT_PLATFORM_NAME),
                ).fetchone()

                if source_row and target_row:
                    source_name, source_custom, source_score, source_attempts, source_correct, source_date, source_daily = source_row
                    target_name, target_custom, target_score, target_attempts, target_correct, target_date, target_daily = target_row
                    source_date = source_date or ""
                    target_date = target_date or ""
                    if source_date == target_date:
                        merged_date = source_date or None
                        merged_daily = int(source_daily or 0) + int(target_daily or 0)
                    elif source_date > target_date:
                        merged_date = source_date or None
                        merged_daily = int(source_daily or 0)
                    else:
                        merged_date = target_date or None
                        merged_daily = int(target_daily or 0)
                    cursor.execute(
                        "UPDATE user_stats SET user_name = ?, custom_name = ?, score = ?, "
                        "attempts = ?, correct_attempts = ?, last_play_date = ?, daily_plays = ? "
                        "WHERE user_id = ? AND platform_name = ?",
                        (
                            target_name or source_name,
                            target_custom or source_custom,
                            int(target_score or 0) + int(source_score or 0),
                            int(target_attempts or 0) + int(source_attempts or 0),
                            int(target_correct or 0) + int(source_correct or 0),
                            merged_date,
                            merged_daily,
                            target_id,
                            DEFAULT_PLATFORM_NAME,
                        ),
                    )
                elif source_row:
                    source_name, source_custom, source_score, source_attempts, source_correct, source_date, source_daily = source_row
                    cursor.execute(
                        "INSERT OR REPLACE INTO user_stats "
                        "(user_id, user_name, custom_name, platform_name, score, attempts, "
                        "correct_attempts, last_play_date, daily_plays) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            target_id,
                            source_name,
                            source_custom,
                            DEFAULT_PLATFORM_NAME,
                            int(source_score or 0),
                            int(source_attempts or 0),
                            int(source_correct or 0),
                            source_date,
                            int(source_daily or 0),
                        ),
                    )

                cursor.execute(
                    "INSERT INTO account_bindings (official_platform, official_user_id, qq_user_id, bound_at) "
                    "VALUES (?, ?, ?, ?)",
                    (OFFICIAL_PLATFORM_NAME, source_id, target_id, time.strftime("%Y-%m-%d %H:%M:%S")),
                )
                cursor.execute(
                    "DELETE FROM user_stats WHERE user_id = ? AND platform_name = ?",
                    (source_id, OFFICIAL_PLATFORM_NAME),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            except Exception as exc:
                conn.rollback()
                logger.error(f"绑定 QQ 官方机器人账号失败: {exc}", exc_info=True)
                return False

    def _get_display_name(self, user_id: str, original_name: Optional[str] = None) -> str:
        """获取用户显示名称，黑名单用户显示统一标识"""
        if self._is_user_blacklisted(user_id):
            return "[此用户已被BOT拉黑]"
        return original_name if original_name else "未知用户"



    async def _get_session(self) -> 'aiohttp.ClientSession':
        """延迟初始化并获取 aiohttp session"""
        if not aiohttp:
            return None
        if self.http_session is None or self.http_session.closed:
            async with self.session_lock:
                # 再次检查，防止在等待锁的过程中已经被其他协程创建
                if self.http_session is None or self.http_session.closed:
                    self.http_session = aiohttp.ClientSession()
        return self.http_session

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

    def _get_resource_url(self, relative_path: str) -> str:
        """返回远程资源的URL字符串"""
        base_url = "https://storage.exmeaning.com/sekai-jp-assets/character"
        return f"{base_url}/{'/'.join(Path(relative_path).parts)}"

    async def _open_image(self, image_source: Union[Path, str]) -> Optional[Image.Image]:
        """打开一个资源图片，无论是本地路径还是远程URL。"""
        if not image_source:
            return None

        if isinstance(image_source, str) and image_source.startswith(('http://', 'https://')):
            session = await self._get_session()
            if not session:
                logger.error("无法获取远程图片: `aiohttp` 模块未安装。")
                return None

            max_size = 10 * 1024 * 1024  # 10MB
            for attempt in range(1, self.REMOTE_IMAGE_MAX_ATTEMPTS + 1):
                try:
                    async with session.get(image_source, timeout=10) as response:
                        response.raise_for_status()

                        content_length = response.headers.get('Content-Length')
                        if content_length and int(content_length) > max_size:
                            logger.error(f"远程图片过大: {content_length} bytes，超过限制 {max_size} bytes")
                            return None

                        image_data = bytearray()
                        async for chunk in response.content.iter_chunked(8192):
                            image_data.extend(chunk)
                            if len(image_data) > max_size:
                                logger.error(f"远程图片下载超过大小限制 {max_size} bytes")
                                return None

                        image = Image.open(io.BytesIO(image_data))
                        image.load()
                        return image
                except Exception as e:
                    if not self._is_retryable_image_error(e) or attempt >= self.REMOTE_IMAGE_MAX_ATTEMPTS:
                        logger.error(f"无法打开图片资源 {image_source}: {e}", exc_info=True)
                        return None

                    logger.warning(
                        f"打开远程图片失败，将重试 ({attempt}/{self.REMOTE_IMAGE_MAX_ATTEMPTS - 1}): "
                        f"{image_source}: {e}"
                    )
                    await asyncio.sleep(self.REMOTE_IMAGE_RETRY_DELAY * attempt)
            return None

        try:
            return Image.open(image_source)
        except Exception as e:
            logger.error(f"无法打开图片资源 {image_source}: {e}", exc_info=True)
            return None

    @staticmethod
    def _is_retryable_image_error(error: Exception) -> bool:
        """判断图片下载错误是否可能通过重新请求恢复。"""
        if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True

        if aiohttp and isinstance(error, aiohttp.ClientResponseError):
            return False
        return bool(aiohttp and isinstance(error, aiohttp.ClientError))

    def _compress_image(
        self,
        img: Image.Image,
        max_size: int = 800,
        quality: int = 85,
        output_prefix: str = "answer",
    ) -> Optional[str]:
        """压缩图片并保存到插件输出目录，返回 AstrBot 可接受的字符串路径。"""
        try:
            # 缩放至最大尺寸
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            # 转为 RGB（JPEG 不支持 alpha 通道）
            if img.mode in ("RGBA", "P", "LA"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            os.makedirs(self.output_dir, exist_ok=True)
            img_path = self.output_dir / f"{output_prefix}_{time.time_ns()}.jpg"
            img.save(img_path, format="JPEG", quality=quality, optimize=True)
            return str(img_path)
        except Exception as e:
            logger.error(f"压缩图片失败: {e}")
            return None

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """
        检查当前消息是否被允许.
        - 如果白名单为空, 则允许所有群聊和私聊.
        - 如果白名单不为空, 则只允许在白名单内的群聊中触发, 并禁用所有私聊.
        """
        if not self.group_whitelist:
            return True # 白名单为空, 允许所有

        # 白名单不为空, 开始严格检查
        group_id = event.get_group_id()
        if group_id and str(group_id) in self.group_whitelist:
            return True # 是白名单中的群聊, 允许

        return False # 是私聊, 或非白名单群聊, 均不允许

    def _get_whitelist_reject_message(self) -> Optional[str]:
        """获取白名单拒绝提示信息"""
        msg = self.config.get("whitelist_reject_message", "")
        if msg and msg.strip():
            return msg.strip()
        return None

    def get_conn(self) -> sqlite3.Connection:
        """获取数据库连接，设置超时防止锁冲突"""
        return sqlite3.connect(self.db_path, timeout=30.0)



    def _cleanup_output_dir(self, max_age_seconds: int = 3600):
        """清理旧的排行榜图片和模糊处理的图片，同时清理相关缓存"""
        if not self.output_dir.exists():
            return

        now = time.time()
        try:
            # 收集要删除的文件路径
            files_to_delete = []
            for filename in os.listdir(self.output_dir):
                file_path = self.output_dir / filename
                # 确保只删除本插件生成的 png 和 jpg 图片 (包括排行榜、优化后的答案图和模糊处理的图片)
                if file_path.is_file() and (
                    filename.startswith("ranking_") or 
                    filename.startswith("answer_") or
                    filename.startswith("compressed_") or
                    filename.startswith("blurred_") or
                    filename.startswith("processed_")
                ) and (filename.endswith(".png") or filename.endswith(".jpg")):
                    file_mtime = file_path.stat().st_mtime
                    if (now - file_mtime) > max_age_seconds:
                        files_to_delete.append(str(file_path))
                        os.remove(file_path)
                        logger.info(f"已清理旧图片: {filename}")
            
            # 清理缓存中已删除的文件
            if files_to_delete:
                keys_to_remove = []
                for key, cached_path in self.image_cache.cache.items():
                    if cached_path in files_to_delete:
                        keys_to_remove.append(key)
                for key in keys_to_remove:
                    del self.image_cache.cache[key]
                    
        except Exception as e:
            logger.error(f"清理图片时出错: {e}")
    
    def _apply_effects_sync(self, image_source: Union[Path, str], effect_names: list) -> Optional[str]:
        """对图片应用多种效果处理（同步版本），带缓存支持"""
        try:
            if isinstance(image_source, str) and image_source.startswith(('http://', 'https://')):
                return None
            
            # 生成缓存键
            cache_key = f"{str(image_source)}:{','.join(sorted(effect_names))}"
            cached_result = self.image_cache.get(cache_key)
            if cached_result and os.path.exists(cached_result):
                return cached_result
            
            with Image.open(image_source) as img:
                if not img:
                    return None
                
                processed_img = self.effect_processor.apply_effects(img, effect_names)
                
                os.makedirs(self.output_dir, exist_ok=True)
                img_path = self.output_dir / f"processed_{time.time_ns()}.png"
                processed_img.save(img_path)
                
                # 存入缓存
                result_path = str(img_path)
                self.image_cache.set(cache_key, result_path)
                
                return result_path
        except Exception as e:
            logger.error(f"应用图片效果失败: {e}")
            return None

    async def _apply_effects(self, image_source: Union[Path, str], effect_names: list) -> Optional[str]:
        """对图片应用多种效果处理（异步包装）"""
        if isinstance(image_source, str) and image_source.startswith(('http://', 'https://')):
            img = await self._open_image(image_source)
            if not img:
                return None
            try:
                processed_img = await asyncio.to_thread(self.effect_processor.apply_effects, img, effect_names)
                os.makedirs(self.output_dir, exist_ok=True)
                img_path = self.output_dir / f"processed_{time.time_ns()}.png"
                await asyncio.to_thread(processed_img.save, img_path)
                return str(img_path)
            finally:
                img.close()
        else:
            return await asyncio.to_thread(self._apply_effects_sync, image_source, effect_names)

    # --- 游戏逻辑 ---
    def start_new_game(self, force_effect_names: Optional[list] = None, card_pool: Optional[list] = None) -> Optional[dict]:
        """准备一轮新游戏，加入花前/花后逻辑和图片效果"""
        if not self.characters_map:
            logger.error("无法开始游戏，因为角色数据未成功加载。")
            return None

        card_pool = card_pool or self.guess_cards
        if not card_pool:
            logger.error("无法开始游戏，卡池为空。")
            return None
        card = random.choice(card_pool)
        card_type = random.choice(["normal", "after_training"])
        
        # 使用原始卡面图片作为问题图片
        answer_image_filename = f"card_{card_type}.webp"

        character = self.characters_map.get(card["characterId"])
        if not character:
            logger.error(f"未找到ID为 {card['characterId']} 的角色")
            return None

        # 选择随机效果或效果组合
        if force_effect_names:
            effect_names = force_effect_names
            effect_name = " + ".join([self.effect_processor.EFFECT_NAMES.get(name, name) for name in effect_names])
        else:
            effect_names, effect_name = self.effect_processor.random_effect_combination()
            
        difficulty = self.effect_processor.calculate_difficulty(effect_names)
        
        # 根据难度计算基础分数，难度越高分数越高
        base_score = difficulty
        
        show_rarity_hint = random.choice([True, False])
        show_training_hint = random.choice([True, False])

        # 获取卡面图片路径
        
        return {
            "card": card,
            "card_state": card_type,
            "card_image_source": self._get_resource_url(f'member/{card["assetbundleName"]}/{answer_image_filename}'),
            "character": character,
            "score": base_score,
            "show_rarity_hint": show_rarity_hint,
            "show_training_hint": show_training_hint,
            "effect_names": effect_names,
            "effect_name": effect_name,
            "difficulty": difficulty,
        }

    # --- 指令处理 ---
    @filter.command("pjsk自动猜卡面", alias={"自动猜卡", "自动猜卡面", "agc"})
    async def start_auto_guess_card(self, event: AstrMessageEvent):
        """开启自动猜卡面模式"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return

        user_id, _ = self._get_account_identity(event)
        if self._is_event_user_blacklisted(event, user_id):
            yield event.plain_result("抱歉，你已被禁止使用猜卡功能 😔")
            return

        session_id = event.unified_msg_origin
        if session_id in self.auto_game_sessions:
            yield event.plain_result("当前会话已经处于自动猜卡面模式中啦~")
            return

        self.auto_game_sessions[session_id] = 0
        yield event.plain_result("已开启自动猜卡面模式！本局结束后将自动开始下一局。发送“退出自动模式”可关闭自动模式。")

        # 触发第一局
        async for res in self.start_guess_card(event):
            yield res

    @filter.command("猜卡面切换国服题库", alias={"猜卡切换国服题库"})
    async def switch_to_sc(self, event: AstrMessageEvent):
        """切换为国服题库。"""
        await self._switch_server(event, SERVER_SC)

    @filter.command("猜卡面切换日服题库", alias={"猜卡切换日服题库"})
    async def switch_to_jp(self, event: AstrMessageEvent):
        """切换为日服题库。"""
        await self._switch_server(event, SERVER_JP)

    @filter.command("猜卡面绑定", alias={"pjsk猜卡面绑定", "猜卡绑定", "猜卡面绑定QQ"})
    async def bind_card_account(self, event: AstrMessageEvent):
        """QQ 官方机器人账号绑定到普通 QQ 账号。"""
        if not self._is_qq_official_event(event):
            yield event.plain_result("此绑定功能仅支持 QQ 官方机器人使用。")
            return

        parts = event.message_str.strip().split(maxsplit=1)
        qq_user_id = parts[1].strip() if len(parts) > 1 else ""
        if not re.fullmatch(r"\d{5,12}", qq_user_id):
            yield event.plain_result("请按“猜卡面绑定 QQ号”的格式输入，例如：猜卡面绑定 21555762216。")
            return

        official_user_id = str(event.get_sender_id())
        if self._resolve_account_id(OFFICIAL_PLATFORM_NAME, official_user_id) != official_user_id:
            current_id = self._resolve_account_id(OFFICIAL_PLATFORM_NAME, official_user_id)
            yield event.plain_result(f"当前官方机器人账号已经绑定至 QQ号 {current_id}。")
            return

        yield event.plain_result(self._build_binding_confirmation_message(qq_user_id))
        decision = None

        @session_waiter(timeout=60)
        async def binding_waiter(controller: SessionController, answer_event: AstrMessageEvent):
            nonlocal decision
            if answer_event.message_str.strip() == "确认":
                decision = "confirm"
                controller.stop()
            elif answer_event.message_str.strip() == "取消":
                decision = "cancel"
                controller.stop()

        try:
            await binding_waiter(
                event,
                session_filter=BindingSessionFilter(event.unified_msg_origin, official_user_id),
            )
        except TimeoutError:
            yield event.plain_result("绑定确认已超时，绑定操作已取消。")
            return

        if decision == "cancel":
            yield event.plain_result("已取消绑定。")
            return
        if decision != "confirm":
            yield event.plain_result("未收到有效的绑定确认，绑定操作已取消。")
            return

        bound = await asyncio.to_thread(self._bind_official_account, official_user_id, qq_user_id)
        if bound:
            yield event.plain_result(f"绑定成功！官方机的历史分数已迁移至 QQ号 {qq_user_id}。")
        else:
            yield event.plain_result("绑定失败：该官方账号可能已绑定，请稍后重试。")

    @filter.command("pjsk猜卡面", alias={"猜卡", "gc","猜卡面"})
    async def start_guess_card(self, event: AstrMessageEvent):
        """开始一轮猜卡游戏"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return
            
        user_id, _ = self._get_account_identity(event)
        if self._is_event_user_blacklisted(event, user_id):
            yield event.plain_result("抱歉，你已被禁止使用猜卡功能 😔")
            return
            
        session_id = event.unified_msg_origin

        # --- 锁定会话以防止竞态条件 ---
        if session_id not in self.session_locks:
            self.session_locks[session_id] = asyncio.Lock()
        lock = self.session_locks[session_id]

        async with lock:
            if session_id in self.active_game_sessions:
                yield event.plain_result("当前已经有一个游戏在进行中啦~ 等它结束后再来玩吧！")
                return

            # 自动模式下不检查冷却时间
            if session_id not in self.auto_game_sessions:
                cooldown = self.config.get("game_cooldown_seconds", 60)
                last_end_time = self.last_game_end_time.get(session_id, 0)
                time_since_last_game = time.time() - last_end_time

                if time_since_last_game < cooldown:
                    remaining_time = cooldown - time_since_last_game
                    time_display = f"{remaining_time:.3f}" if remaining_time < 1 else str(int(remaining_time))
                    yield event.plain_result(f"让我们休息一下吧！{time_display}秒后再来玩哦~ 😊")
                    return

            current_user_id, current_platform_name = self._get_account_identity(event)
            if not self._can_play(current_user_id, current_platform_name):
                # 如果是自动模式，次数用完时需要关闭自动模式并提示
                if session_id in self.auto_game_sessions:
                    self.auto_game_sessions.pop(session_id, None)
                yield event.plain_result(f"今天的游戏次数已经用完啦~ 明天再来玩吧！每天最多可以玩{self.config.get('daily_play_limit', 10)}次哦~ ✨")
                return
            
            # 标记游戏会话为活动状态，然后释放锁
            self.active_game_sessions.add(session_id)

        try:
            game_data = self.start_new_game(
                card_pool=self._get_cards_for_session(session_id)
            )
            if not game_data:
                yield event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误，请联系管理员。")
                if session_id in self.auto_game_sessions:
                    self.auto_game_sessions.pop(session_id, None)
                return

            # 对卡面图片应用多种效果处理
            card_image_source = game_data.get("card_image_source")
            effect_names = game_data.get("effect_names", ["heavy_blur"])
            processed_image_path = await self._apply_effects(card_image_source, effect_names)
            
            if not processed_image_path:
                yield event.plain_result("哎呀，处理图片时遇到了一点小问题呢~ 游戏暂时中断了，稍后再试试吧！")
                if session_id in self.auto_game_sessions:
                    self.auto_game_sessions.pop(session_id, None)
                return

            # 在后台日志中输出答案和效果信息，方便测试
            logger.info(f"[猜卡插件] 新游戏开始. 答案: {game_data['character']['fullNameChinese']}, 效果: {game_data.get('effect_name', '未知')}, 难度: {game_data.get('difficulty', 1)}")
                
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
            effect_name = game_data.get("effect_name", "无效果")
            difficulty = game_data.get("difficulty", 1)
            difficulty_stars = "⭐" * difficulty

            intro_text = f"请在{timeout_seconds}秒内发送角色名称缩写进行回答哦(无需@机器人)\n"
            effect_text = f"本轮图片效果: {effect_name}\n猜对得分: {difficulty}分\n"

            hint_text = "\n".join(hints) + "\n" if hints else ""

            intro_full = intro_text + effect_text + hint_text
            in_auto_mode = session_id in self.auto_game_sessions
            official_self_id = self._get_official_self_id(event) if is_official_round else ""

            if in_auto_mode:
                # 自动模式：不出现 markdown 按钮，用文字提示退出方式
                intro_full += "\n发送「仅退出本局」可结束本局，发送「退出自动模式」可停止自动模式。"
                yield event.chain_result([Comp.Plain(intro_full)])
            elif is_official_round and official_self_id:
                # 官方机器人以 markdown 发送开局消息，附"仅退出本局 / 退出自动模式"连接
                # （回答仍监听全部消息，无需点击回答）
                intro_full += (
                    "\n"
                    + self._build_connect_link("仅退出本局", official_self_id)
                    + "  "
                    + self._build_connect_link("退出自动模式", official_self_id)
                )
                try:
                    await self._send_markdown_text(event, intro_full)
                except Exception as e:
                    logger.error(f"发送开局消息失败: {e}", exc_info=True)
                    yield event.plain_result("发送开局消息时出错，游戏中断。")
                    return
            else:
                try:
                    yield event.chain_result([Comp.Plain(intro_full)])
                except Exception as e:
                    logger.error(f"发送开局消息失败: {e}", exc_info=True)
                    yield event.plain_result("发送开局消息时出错，游戏中断。")
                    return

            # 卡面图片单独发送
            try:
                if processed_image_path:
                    image_chain: list = []
                    try:
                        effect_img = await asyncio.to_thread(Image.open, processed_image_path)
                        compressed = await asyncio.to_thread(
                            self._compress_image,
                            effect_img,
                            output_prefix="compressed",
                        )
                        effect_img.close()
                        if compressed:
                            image_chain.append(Comp.Image(file=compressed))
                        else:
                            image_chain.append(Comp.Image(file=processed_image_path))
                    except Exception:
                        image_chain.append(Comp.Image(file=processed_image_path))
                    yield event.chain_result(image_chain)
            except Exception as e:
                logger.error(f"发送图片失败: {e}. Check if the file path is correct and accessible.")
                yield event.plain_result("发送问题图片时出错，游戏中断。")
                return
            
            # 记录游戏开始，并增加该用户的每日游戏次数（在成功发送图片后才记次）
            self._record_game_start(
                current_user_id,
                event.get_sender_name(),
                current_platform_name,
            )
            
            # 创建并初始化游戏会话状态，确保完全按会话隔离
            game_session = GameSession()
            game_session.game_data = game_data
            self.game_sessions[session_id] = game_session
            max_guess_attempts = self.config.get("max_guess_attempts", 10)
            
            winners_list = []  # 记录所有获奖者（用于奖励有效时间功能）
            first_correct_time = None  # 记录第一个答对的时间
            reward_valid_time = self.config.get("reward_valid_time", 0)  # 奖励有效时间配置
            
            logger.info(f"[猜卡面] 奖励有效时间配置: {reward_valid_time}秒")

            @session_waiter(timeout=timeout_seconds)  # type: ignore
            async def guess_waiter(controller: SessionController, answer_event: AstrMessageEvent):
                nonlocal first_correct_time, winners_list
                
                answer_text = answer_event.message_str.strip()

                # 仅退出本局：只在游玩时生效，立即结束当前对局（不影响自动模式）
                if answer_text == "仅退出本局":
                    game_session.game_ended_by_quit = True
                    controller.stop()
                    return

                # 退出自动模式：任何时候可触发，本局继续、自动模式停止
                if answer_text in ["退出自动模式", "退出", "停止自动猜卡", "停止自动猜卡面"]:
                    if self.auto_game_sessions.pop(session_id, None) is not None:
                        await answer_event.send(answer_event.plain_result("已退出自动模式，本局结束后将停止自动猜卡面模式。"))
                    return

                # 移除对!前缀的强制要求
                answer_name = re.sub(r"^[!！]", "", answer_text).lower()

                if answer_name:
                    # 验证输入是否为有效的角色名称或别名
                    if answer_name not in self.valid_answers:
                        # 输入不是有效的角色名称或别名，忽略该输入
                        return
                        
                    game_session.guess_attempts_count += 1
                    user_id, platform_name = self._get_account_identity(answer_event)
                    if self._is_event_user_blacklisted(answer_event, user_id):
                        return
                    
                    try:
                        correct_name_abbr = game_session.game_data["character"]["name"].lower()
                        correct_name_chinese = game_session.game_data["character"]["fullNameChinese"].lower()
                        aliases = game_session.game_data["character"].get("aliases", [])
                        
                        # 检查是否匹配任何一个可能的答案（缩写、中文名称、别名）
                        is_correct = answer_name == correct_name_abbr or answer_name == correct_name_chinese
                        
                        # 检查是否匹配任何一个别名
                        if not is_correct:
                            for alias in aliases:
                                if answer_name == alias.lower():
                                    is_correct = True
                                    break

                        if is_correct:
                            winner_name = answer_event.get_sender_name()
                            score = game_session.game_data["score"]
                            current_time = time.time()
                            
                            if not game_session.winner_info:
                                first_correct_time = current_time
                                
                                game_session.winner_info = {
                                    "name": winner_name,
                                    "id": user_id,
                                    "platform_name": platform_name,
                                    "score": score,
                                }
                                
                                winners_list.append({
                                    'user_id': user_id,
                                    'platform_name': platform_name,
                                    'user_name': winner_name,
                                    'answer_time': current_time,
                                    'is_first': True
                                })
                                
                                if reward_valid_time > 0:
                                    logger.info(f"[猜卡面] 第一个答对者: {winner_name}，启动{reward_valid_time}秒奖励有效时间")
                                    async def stop_after_delay():
                                        await asyncio.sleep(reward_valid_time)
                                        controller.stop()
                                    asyncio.create_task(stop_after_delay())
                                else:
                                    controller.stop()
                                    return
                            else:
                                time_since_first_correct = current_time - first_correct_time
                                if time_since_first_correct <= reward_valid_time and reward_valid_time > 0:
                                    if not any(
                                        w['user_id'] == user_id
                                        and w.get('platform_name') == platform_name
                                        for w in winners_list
                                    ):
                                        winners_list.append({
                                            'user_id': user_id,
                                            'platform_name': platform_name,
                                            'user_name': winner_name,
                                            'answer_time': current_time,
                                            'is_first': False
                                        })
                                        logger.info(f"[猜卡面] 奖励有效时间内额外答对: {winner_name} (+{time_since_first_correct:.2f}s)")
                        else:
                            if (user_id, platform_name) not in game_session.user_stats_recorded:
                                self._update_stats(
                                    user_id,
                                    answer_event.get_sender_name(),
                                    0,
                                    correct=False,
                                    platform_name=platform_name,
                                )
                                game_session.user_stats_recorded.add((user_id, platform_name))
                    except (ValueError, IndexError):
                        pass

                    # 如果达到猜测次数上限，则结束游戏（-1表示无限制）
                    if max_guess_attempts != -1 and game_session.guess_attempts_count >= max_guess_attempts:
                        game_session.game_ended_by_attempts = True
                        controller.stop()

            try:
                await guess_waiter(event)
            except TimeoutError:
                game_session.game_ended_by_timeout = True

            # 记录游戏结束时间，无论游戏如何结束
            self.last_game_end_time[session_id] = time.time()

            # 自动模式：只要有人作答（无论对错）就重置连续无人回答计数，
            # 只有整局零作答的超时局才累计，避免有人答题却被判“无人回答”
            if session_id in self.auto_game_sessions:
                if game_session.winner_info or game_session.guess_attempts_count > 0:
                    self.auto_game_sessions[session_id] = 0
                elif game_session.game_ended_by_timeout:
                    self.auto_game_sessions[session_id] = self.auto_game_sessions.get(session_id, 0) + 1

            # --- 统一在游戏结束后公布结果 ---
            correct_name = game_session.game_data['character']['fullNameChinese']
            round_server = self._server_for_session(session_id)

            if game_session.winner_info:
                if len(winners_list) == 1:
                    self._update_stats(
                        game_session.winner_info['id'],
                        game_session.winner_info['name'],
                        game_session.winner_info['score'],
                        correct=True,
                        platform_name=game_session.winner_info['platform_name'],
                    )
                    result_text = f"{game_session.winner_info['name']}答对了呢!获得了{game_session.winner_info['score']}分！继续加油哦~\n正确答案是: {correct_name}"
                else:
                    winner_names = [w['user_name'] for w in winners_list]
                    for winner in winners_list:
                        self._update_stats(
                            winner['user_id'],
                            winner['user_name'],
                            game_session.winner_info['score'],
                            correct=True,
                            platform_name=winner['platform_name'],
                        )
                    result_text = (
                        f"🎉 恭喜以下玩家答对！每人获得{game_session.winner_info['score']}分！\n"
                        f"{'、'.join(winner_names)}\n\n"
                        f"正确答案是: {correct_name}"
                    )

            elif game_session.game_ended_by_quit:
                result_text = f"本局已结束（仅退出本局）\n正确答案是: {correct_name}"
            elif game_session.game_ended_by_attempts:
                result_text = f"哎呀，本轮猜测次数已经用完了呢~ 没关系，下次一定可以的！\n正确答案是: {correct_name}"
            elif game_session.game_ended_by_timeout:
                result_text = f"时间到啦~ 大家有没有猜出来呢？\n正确答案是: {correct_name}"
            else:
                result_text = ""

            if result_text:
                if session_id in self.auto_game_sessions:
                    # 自动模式：只显示结果与卡名，随后自动开始下一局，不出现 markdown 按钮
                    result_text += "\n即将开始下一局，发送「退出自动模式」可停止自动模式。"
                    yield event.plain_result(result_text)
                else:
                    result_text += "\n" + "\n".join(self._build_server_footer(event, round_server))
                    if self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME:
                        # 官方机器人以 markdown 渲染结算消息，附切换/绑定/查分/排行榜连接与快捷入口
                        await self._send_markdown_text(event, result_text)
                    else:
                        yield event.plain_result(result_text)

            # 使用原始卡面图片作为答案图片（压缩后发送）
            card_image_source = game_data.get("card_image_source")
            image_msg = []
            if card_image_source:
                img = None
                try:
                    img = await self._open_image(card_image_source)
                    if img:
                        compressed = await asyncio.to_thread(self._compress_image, img, max_size=800, quality=90)
                        img.close()
                        if compressed:
                            image_msg.append(Comp.Image(file=compressed))
                        else:
                            image_msg.append(Comp.Image(file=str(card_image_source)))
                    else:
                        image_msg.append(Comp.Image(file=str(card_image_source)))
                except Exception as e:
                    logger.error(f"处理答案图片失败: {e}")
                    image_msg.append(Comp.Image(file=str(card_image_source)))
            
            if image_msg:
                yield event.chain_result(image_msg)
        finally:
            if session_id in self.active_game_sessions:
                self.active_game_sessions.remove(session_id)
            # 清理游戏会话状态
            if session_id in self.game_sessions:
                del self.game_sessions[session_id]
            # 不再删除锁实例，保留锁以维持互斥机制

        # 自动下一局逻辑（放在 finally 块之后，确保当前游戏状态已清理）
        if session_id in self.auto_game_sessions:
            # 使用 session_waiter 监听退出指令，同时等待 3 秒延迟
            class AutoModeFilter(SessionFilter):
                def filter(self, ev: AstrMessageEvent) -> str:
                    return ev.unified_msg_origin

            @session_waiter(timeout=3)  # type: ignore
            async def auto_mode_waiter(controller: SessionController, answer_event: AstrMessageEvent):
                text = answer_event.message_str.strip()
                if text in ["退出自动模式", "退出", "停止自动猜卡", "停止自动猜卡面"]:
                    if self.auto_game_sessions.pop(session_id, None) is not None:
                        await answer_event.send(answer_event.plain_result("自动猜卡面模式已停止。"))
                    controller.stop()

            try:
                await auto_mode_waiter(event, session_filter=AutoModeFilter())
            except TimeoutError:
                pass  # 超时正常，继续下一局

            if session_id in self.auto_game_sessions:
                # 检查连续无人回答是否达到 3 局
                if self.auto_game_sessions.get(session_id, 0) >= 3:
                    self.auto_game_sessions.pop(session_id, None)
                    yield event.plain_result("连续3局没有人回答呢，自动猜卡面模式已停止。")
                    return

                # 检查是否还能继续玩（次数限制）
                current_user_id, current_platform_name = self._get_account_identity(event)
                if self._can_play(current_user_id, current_platform_name):
                    async for res in self.start_guess_card(event):
                        yield res
                else:
                    self.auto_game_sessions.pop(session_id, None)
                    yield event.plain_result("今天的游戏次数已经用完啦，自动猜卡面模式已停止。")


    @filter.command("测试猜卡")
    async def test_guess_card(self, event: AstrMessageEvent):
        """测试模式：指定效果开始一轮猜卡游戏"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return
            
        user_id = event.get_sender_id()
        if self._is_user_blacklisted(user_id):
            return
            
        # 校验管理员权限
        super_users = self.config.get("super_users", [])
        if str(user_id) not in super_users:
            yield event.plain_result("哎呀，测试猜卡指令只有管理员才能使用哦~ 😊")
            return

        # 解析效果名称
        msg_str = event.message_str.strip()
        effect_arg = re.sub(r"^测试猜卡\s*", "", msg_str).strip()
        
        if not effect_arg:
            available_effects = "、".join(self.effect_processor.EFFECT_NAMES.values())
            yield event.plain_result(f"请输入要测试的效果名称哦！\n可用效果: {available_effects}\n例如: 测试猜卡 轻度模糊")
            return
            
        # 查找对应的效果 key
        effect_key = self.effect_processor.EFFECT_NAME_TO_KEY.get(effect_arg)
        if not effect_key:
            # 尝试直接匹配 key
            if effect_arg in self.effect_processor.EFFECT_NAMES:
                effect_key = effect_arg
            else:
                available_effects = "、".join(self.effect_processor.EFFECT_NAMES.values())
                yield event.plain_result(f"未找到效果 '{effect_arg}' 呢~\n可用效果: {available_effects}")
                return

        session_id = event.unified_msg_origin

        # --- 锁定会话以防止竞态条件 ---
        if session_id not in self.session_locks:
            self.session_locks[session_id] = asyncio.Lock()
        lock = self.session_locks[session_id]

        async with lock:
            if session_id in self.active_game_sessions:
                yield event.plain_result("当前已经有一个游戏在进行中啦~ 等它结束后再来玩吧！")
                return
            
            # 标记游戏会话为活动状态，然后释放锁
            self.active_game_sessions.add(session_id)

        try:
            # 强制使用指定效果
            game_data = self.start_new_game(force_effect_names=[effect_key])
            if not game_data:
                yield event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误，请联系管理员。")
                return

            # 对卡面图片应用多种效果处理
            card_image_source = game_data.get("card_image_source")
            effect_names = game_data.get("effect_names", ["heavy_blur"])
            processed_image_path = await self._apply_effects(card_image_source, effect_names)
            
            if not processed_image_path:
                yield event.plain_result("哎呀，处理图片时遇到了一点小问题呢~ 游戏暂时中断了，稍后再试试吧！")
                return

            # 在后台日志中输出答案和效果信息，方便测试
            logger.info(f"[猜卡插件][测试模式] 新游戏开始. 答案: {game_data['character']['fullNameChinese']}, 效果: {game_data.get('effect_name', '未知')}, 难度: {game_data.get('difficulty', 1)}")
                
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
            effect_name = game_data.get("effect_name", "无效果")
            difficulty = game_data.get("difficulty", 1)
            
            intro_text = f"【测试模式】请在{timeout_seconds}秒内发送角色名称缩写进行回答哦(无需@机器人)\n"
            effect_text = f"本轮图片效果: {effect_name}\n猜对得分: {difficulty}分\n"
            
            hint_text = "\n".join(hints) + "\n" if hints else ""
            
            msg_chain: list = [Comp.Plain(intro_text + effect_text + hint_text)]

            try:
                # 发送效果处理后的图片（压缩后发送）
                if processed_image_path:
                    try:
                        effect_img = await asyncio.to_thread(Image.open, processed_image_path)
                        compressed = await asyncio.to_thread(
                            self._compress_image,
                            effect_img,
                            output_prefix="compressed",
                        )
                        effect_img.close()
                        if compressed:
                            msg_chain.append(Comp.Image(file=compressed))
                        else:
                            msg_chain.append(Comp.Image(file=processed_image_path))
                    except Exception:
                        msg_chain.append(Comp.Image(file=processed_image_path))
                yield event.chain_result(msg_chain)
            except Exception as e:
                logger.error(f"发送图片失败: {e}. Check if the file path is correct and accessible.")
                yield event.plain_result("发送问题图片时出错，游戏中断。")
                return
            
            # 记录游戏开始，测试模式不增加该用户的每日游戏次数
            
            # 创建并初始化游戏会话状态，确保完全按会话隔离
            game_session = GameSession()
            game_session.game_data = game_data
            self.game_sessions[session_id] = game_session
            max_guess_attempts = self.config.get("max_guess_attempts", 10)
            
            winners_list = []  # 记录所有获奖者（用于奖励有效时间功能）
            first_correct_time = None  # 记录第一个答对的时间
            reward_valid_time = self.config.get("reward_valid_time", 0)  # 奖励有效时间配置
            
            @session_waiter(timeout=timeout_seconds)  # type: ignore
            async def guess_waiter(controller: SessionController, answer_event: AstrMessageEvent):
                nonlocal first_correct_time, winners_list
                
                answer_text = answer_event.message_str.strip()
                
                # 移除对!前缀的强制要求
                answer_name = re.sub(r"^[!！]", "", answer_text).lower()

                if answer_name:
                    # 验证输入是否为有效的角色名称或别名
                    if answer_name not in self.valid_answers:
                        # 输入不是有效的角色名称或别名，忽略该输入
                        return
                        
                    game_session.guess_attempts_count += 1
                    user_id = answer_event.get_sender_id()
                    
                    try:
                        correct_name_abbr = game_session.game_data["character"]["name"].lower()
                        correct_name_chinese = game_session.game_data["character"]["fullNameChinese"].lower()
                        aliases = game_session.game_data["character"].get("aliases", [])
                        
                        # 检查是否匹配任何一个可能的答案（缩写、中文名称、别名）
                        is_correct = answer_name == correct_name_abbr or answer_name == correct_name_chinese
                        
                        # 检查是否匹配任何一个别名
                        if not is_correct:
                            for alias in aliases:
                                if answer_name == alias.lower():
                                    is_correct = True
                                    break

                        if is_correct:
                            winner_name = answer_event.get_sender_name()
                            score = game_session.game_data["score"]
                            current_time = time.time()
                            
                            if not game_session.winner_info:
                                first_correct_time = current_time
                                
                                game_session.winner_info = {"name": winner_name, "id": user_id, "score": score}
                                
                                winners_list.append({
                                    'user_id': user_id,
                                    'user_name': winner_name,
                                    'answer_time': current_time,
                                    'is_first': True
                                })
                                
                                if reward_valid_time > 0:
                                    async def stop_after_delay():
                                        await asyncio.sleep(reward_valid_time)
                                        controller.stop()
                                    asyncio.create_task(stop_after_delay())
                                else:
                                    controller.stop()
                                    return
                            else:
                                time_since_first_correct = current_time - first_correct_time
                                if time_since_first_correct <= reward_valid_time and reward_valid_time > 0:
                                    if not any(w['user_id'] == user_id for w in winners_list):
                                        winners_list.append({
                                            'user_id': user_id,
                                            'user_name': winner_name,
                                            'answer_time': current_time,
                                            'is_first': False
                                        })
                        else:
                            # 测试模式不扣分或记录错误次数
                            pass
                    except (ValueError, IndexError):
                        pass

                    # 如果达到猜测次数上限，则结束游戏（-1表示无限制）
                    if max_guess_attempts != -1 and game_session.guess_attempts_count >= max_guess_attempts:
                        game_session.game_ended_by_attempts = True
                        controller.stop()

            try:
                await guess_waiter(event)
            except TimeoutError:
                game_session.game_ended_by_timeout = True
            
            # 记录游戏结束时间，无论游戏如何结束
            self.last_game_end_time[session_id] = time.time()

            # --- 统一在游戏结束后公布结果 ---
            correct_name = game_session.game_data['character']['fullNameChinese']

            text_msg = []
            if game_session.winner_info:
                if len(winners_list) == 1:
                    # 测试模式不加分
                    text_msg.append(Comp.Plain(f"【测试模式】{game_session.winner_info['name']}答对了呢!（测试模式不计分）\n正确答案是: {correct_name}"))
                else:
                    winner_names = [w['user_name'] for w in winners_list]
                    text_msg.append(Comp.Plain(
                        f"🎉 【测试模式】恭喜以下玩家答对！（测试模式不计分）\n"
                        f"{'、'.join(winner_names)}\n\n"
                        f"正确答案是: {correct_name}"
                    ))
                    
            elif game_session.game_ended_by_attempts:
                text_msg.append(Comp.Plain(f"哎呀，本轮猜测次数已经用完了呢~ 没关系，下次一定可以的！\n"))
                text_msg.append(Comp.Plain(f"正确答案是: {correct_name}\n"))
            elif game_session.game_ended_by_timeout:
                text_msg.append(Comp.Plain("时间到啦~ 大家有没有猜出来呢？\n"))
                text_msg.append(Comp.Plain(f"正确答案是: {correct_name}\n"))
            
            # --- 发送答案图片 ---
            image_msg = []
            if text_msg:
                yield event.chain_result(text_msg)
                
                # 延迟一小会儿发送答案图片，提升体验
                await asyncio.sleep(0.5)
                
                # 检查答案图片是否存在（压缩后发送）
                card_image_source = game_data.get("card_image_source")
                if card_image_source:
                    img = None
                    try:
                        img = await self._open_image(card_image_source)
                        if img:
                            compressed = await asyncio.to_thread(self._compress_image, img, max_size=800, quality=90)
                            img.close()
                            if compressed:
                                image_msg.append(Comp.Image(file=compressed))
                            else:
                                image_msg.append(Comp.Image(file=str(card_image_source)))
                        else:
                            image_msg.append(Comp.Image(file=str(card_image_source)))
                    except Exception as e:
                        logger.error(f"处理答案图片失败: {e}")
                        image_msg.append(Comp.Image(file=str(card_image_source)))
                
                if image_msg:
                    yield event.chain_result(image_msg)

        finally:
            if session_id in self.active_game_sessions:
                self.active_game_sessions.remove(session_id)
            # 清理游戏会话状态
            if session_id in self.game_sessions:
                del self.game_sessions[session_id]
            # 不再删除锁实例，保留锁以维持互斥机制

    @filter.command("猜卡面帮助")
    async def show_guess_card_help(self, event: AstrMessageEvent):
        """显示猜卡插件帮助"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return
        help_text = (
            "✨ PJSK猜卡面指南 ✨\n\n"
            "基础指令\n"
            "猜卡面 - 随机猜一张卡，看看你能不能认出来！\n"
            "猜卡面切换日服题库/猜卡面切换国服题库 - 切换题库服务器（下一局生效）\n\n"
            "数据统计\n"
            "猜卡面排行榜 - 查看猜卡总分排行榜，看看谁是猜卡大师！\n"
            "猜卡面分数 - 查看自己的猜卡数据统计，了解自己的进步~\n"
            "猜卡面绑定 QQ号 - QQ官方机器人绑定到你的QQ号，需发送“确认”\n"
            "猜卡面自定义名称 - 设置你的个性化ID（不带参数可清除）\n\n"
        )
        yield event.plain_result(help_text)


    @filter.command("猜卡面分数", alias={"pjsk猜卡面分数", "猜卡分数", "猜卡面个人分数"})
    async def show_user_score(self, event: AstrMessageEvent):
        """显示玩家自己的猜卡积分和统计数据"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return
        raw_user_id = str(event.get_sender_id())
        user_id, platform_name = self._get_account_identity(event)
        if self._is_event_user_blacklisted(event, user_id):
            yield event.plain_result("抱歉，你已被禁止使用猜卡功能 😔")
            return
        user_name = event.get_sender_name()
        display_name = self._get_display_name(user_id, user_name)
        platform_display_name = {
            OFFICIAL_PLATFORM_NAME: "QQ官方机器人",
            DEFAULT_PLATFORM_NAME: "普通QQ",
        }.get(self._get_event_platform_name(event), self._get_event_platform_name(event))
        identity_lines = [
            f"👤 用户ID: {raw_user_id}",
            f"🌐 平台: {platform_display_name}（{self._get_event_platform_name(event)}）",
        ]
        if self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME and user_id == raw_user_id:
            identity_lines.extend([
                "当前官方机器人账号尚未绑定QQ号。",
                "如需将官方机分数迁移到普通QQ账号，请发送：猜卡面绑定 QQ号",
            ])
        elif user_id != raw_user_id:
            identity_lines.append(f"🔗 统计账号: {user_id}")
        identity_text = "\n".join(identity_lines)
        
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT score, attempts, correct_attempts, last_play_date, daily_plays "
                "FROM user_stats WHERE user_id = ? AND platform_name = ?",
                (user_id, platform_name),
            )
            user_data = cursor.fetchone()
            
        if not user_data:
            yield event.plain_result(
                f"{identity_text}\n\n"
                f"{user_name}，你还没有参与过猜卡游戏哦！快来一起玩呀~ 🎮"
            )
            return
            
        score, attempts, correct_attempts, last_play_date, daily_plays = user_data
        accuracy = (correct_attempts * 100 / attempts) if attempts > 0 else 0
        
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM user_stats WHERE score > ?", (score,))
            rank = cursor.fetchone()[0] + 1
        
        # 计算今日剩余可猜次数
        daily_limit = self.config.get("daily_play_limit", 10)
        today = time.strftime("%Y-%m-%d")
        remaining_plays = "无限次数"
        if daily_limit != -1:
            if last_play_date == today:
                remaining = daily_limit - daily_plays
                remaining_plays = f"{remaining}次" if remaining > 0 else "0次"
            else:
                remaining_plays = f"{daily_limit}次"
        
        stats_text = (
            f"{identity_text}\n"
            f"✨ {display_name} 的猜卡数据 ✨\n"
            f"🏆 总分: {score} 分\n"
            f"🎯 正确率: {accuracy:.1f}%\n"
            f"🎮 游戏次数: {attempts} 次\n"
            f"✅ 答对次数: {correct_attempts} 次\n"
            f"🏅 当前排名: 第 {rank} 名\n"
            f"📅 今日剩余: {remaining_plays}\n"
        )
        
        yield event.plain_result(stats_text)


    @filter.command("猜卡面自定义名称", alias={"自定义名称", "猜卡自定义名称"})
    async def set_custom_name(self, event: AstrMessageEvent):
        """设置玩家自定义ID"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return

        sender_id, platform_name = self._get_account_identity(event)
        if self._is_event_user_blacklisted(event, sender_id):
            yield event.plain_result("抱歉，你已被禁止使用猜卡功能 😔")
            return
        # 解析命令参数，获取自定义名称
        parts = event.message_str.strip().split(maxsplit=1)
        custom_name = parts[1].strip() if len(parts) > 1 else None
        
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            
            if custom_name:
                # 设置自定义名称
                cursor.execute(
                    "SELECT user_id FROM user_stats WHERE user_id = ? AND platform_name = ?",
                    (sender_id, platform_name),
                )
                if cursor.fetchone():
                    cursor.execute(
                        "UPDATE user_stats SET custom_name = ? WHERE user_id = ? AND platform_name = ?",
                        (custom_name, sender_id, platform_name),
                    )
                else:
                    # 如果用户没有记录，创建一个新记录
                    today = time.strftime("%Y-%m-%d")
                    sender_name = event.get_sender_name()
                    cursor.execute(
                        "INSERT INTO user_stats (user_id, user_name, custom_name, platform_name, last_play_date, daily_plays) VALUES (?, ?, ?, ?, ?, ?)",
                        (sender_id, sender_name, custom_name, platform_name, today, 0)
                    )
                conn.commit()
                yield event.plain_result(f"好的！你的猜卡面自定义名称已设置为：{custom_name} ✨")
            else:
                # 清除自定义名称
                cursor.execute(
                    "SELECT user_id FROM user_stats WHERE user_id = ? AND platform_name = ?",
                    (sender_id, platform_name),
                )
                if cursor.fetchone():
                    cursor.execute(
                        "UPDATE user_stats SET custom_name = NULL WHERE user_id = ? AND platform_name = ?",
                        (sender_id, platform_name),
                    )
                    conn.commit()
                    yield event.plain_result("好的！你的自定义名称已清除，将显示QQ名称 ✨")
                else:
                    yield event.plain_result("你还没有参与过猜卡游戏，暂无自定义名称哦~ 🎮")

    @filter.command("重置猜卡面次数", alias={"resetgl"})
    async def reset_guess_limit(self, event: AstrMessageEvent):
        """重置用户猜卡次数（仅限管理员）"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return

        sender_id = event.get_sender_id()
        super_users = self.config.get("super_users", [])

        if str(sender_id) not in super_users:
            yield event.plain_result("哎呀，这个指令只有管理员才能使用哦~ 😊")
            return

        # 从消息中解析出可能的目标用户ID
        parts = event.message_str.strip().split()
        target_id = sender_id # 默认为自己
        if len(parts) > 1 and parts[1].isdigit():
            target_id = parts[1]
        
        target_id_str = str(target_id)

        if self._reset_user_limit(target_id_str):
            if target_id_str == str(sender_id):
                yield event.plain_result("好的！你的猜卡次数已经重置啦~ 可以继续玩了哦！✨")
            else:
                yield event.plain_result(f"好的！用户 {target_id_str} 的猜卡次数已经重置啦~ ✨")
        else:
            yield event.plain_result(f"哎呀，没有找到用户 {target_id_str} 的游戏记录呢~ 是不是ID输入错了呀？")


    @filter.command("猜卡面排行榜", alias={"猜卡排行榜", "本地猜卡排行榜"})
    async def show_ranking(self, event: AstrMessageEvent):
        """显示猜卡排行榜"""
        if not self._is_group_allowed(event):
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                yield event.plain_result(reject_msg)
            return

        self._cleanup_output_dir()

        ranking_count = self.config.get("ranking_display_count", 10)
        
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT s.user_id, s.user_name, s.custom_name, s.score, s.attempts, "
                "s.correct_attempts, CASE WHEN s.platform_name = ? AND b.official_user_id IS NULL "
                "THEN 1 ELSE 0 END AS is_unbound_official "
                "FROM user_stats AS s LEFT JOIN account_bindings AS b "
                "ON b.official_platform = s.platform_name AND b.official_user_id = s.user_id "
                "WHERE b.official_user_id IS NULL OR s.platform_name != ? "
                "ORDER BY s.score DESC LIMIT ?",
                (OFFICIAL_PLATFORM_NAME, OFFICIAL_PLATFORM_NAME, ranking_count),
            )
            rows = cursor.fetchall()

        if not rows:
            yield event.plain_result("还没有人参与过猜卡游戏呢~ 快来成为第一个玩家吧！✨")
            return

        try:
            img_path = await asyncio.to_thread(self._render_ranking_image, rows, ranking_count)
            if img_path:
                yield event.image_result(str(img_path))
            else:
                yield event.plain_result("生成排行榜图片时出错，请联系管理员。")
        except Exception as e:
            logger.error(f"使用Pillow生成排行榜图片失败: {e}", exc_info=True)
            yield event.plain_result("生成排行榜图片时出错，请联系管理员。")

    def _render_ranking_image(self, rows: list, ranking_count: int = 10) -> Optional[str]:
        """渲染排行榜图片（同步方法），支持动态高度调整"""
        try:
            width = 850
            # 动态计算高度：基础高度 + 每个排名项的高度
            base_height = 250
            item_height = 70
            height = base_height + len(rows) * item_height

            bg_color_start = (230, 240, 255)
            bg_color_end = (200, 210, 240)
            img = Image.new("RGB", (width, height), bg_color_start)
            draw_bg = ImageDraw.Draw(img)
            for y in range(height):
                r = int(bg_color_start[0] + (bg_color_end[0] - bg_color_start[0]) * y / height)
                g = int(bg_color_start[1] + (bg_color_end[1] - bg_color_start[1]) * y / height)
                b = int(bg_color_start[2] + (bg_color_end[2] - bg_color_start[2]) * y / height)
                draw_bg.line([(0, y), (width, y)], fill=(r, g, b))
            


            if img.mode != 'RGBA':
                img = img.convert('RGBA')

            white_overlay = Image.new("RGBA", img.size, (255, 255, 255, 100))
            img = Image.alpha_composite(img, white_overlay)

            title_text = "PJSK猜卡面排行榜"
            font_color = (30, 30, 50)
            shadow_color = (180, 180, 190, 128)
            header_color = (80, 90, 120)
            score_color = (235, 120, 20)
            accuracy_color = (0, 128, 128)
            
            title_font = self.title_font
            header_font = self.header_font
            body_font = self.body_font
            id_font = self.id_font
            medal_font = self.medal_font

            with Pilmoji(img) as pilmoji:
                center_x, title_y = int(width / 2), 80
                pilmoji.text((center_x + 2, title_y + 2), title_text, font=title_font, fill=shadow_color, anchor="mm", emoji_position_offset=(0, 6))
                pilmoji.text((center_x, title_y), title_text, font=title_font, fill=font_color, anchor="mm", emoji_position_offset=(0, 6))

                headers = ["排名", "玩家", "总分", "正确率", "总次数"]
                col_positions_header = [40, 150, 500, 610, 720]
                title_height = pilmoji.getsize(title_text, font=title_font)[1]
                current_y = title_y + int(title_height / 2) + 45
                for header in headers:
                    pilmoji.text((col_positions_header.pop(0), current_y), header, font=header_font, fill=header_color)

                current_y += 55

                rank_icons = ["🥇", "🥈", "🥉"]
                for i, row in enumerate(rows):
                    user_id, user_name, custom_name, score, attempts, correct_attempts = str(row[0]), row[1], row[2], str(row[3]), str(row[4]), row[5]
                    is_unbound_official = bool(row[6])
                    # 优先使用自定义名称，如果没有则使用QQ名称，黑名单用户显示统一标识
                    display_name = self._get_display_name(user_id, custom_name if custom_name else user_name)
                    accuracy = f"{(correct_attempts * 100 / int(attempts) if int(attempts) > 0 else 0):.1f}%"
                    
                    rank = i + 1
                    col_positions = [40, 150, 500, 610, 720]
                    rank_num_align_x = 130

                    pilmoji.text((rank_num_align_x, current_y), str(rank), font=body_font, fill=font_color, anchor="ra")

                    if i < 3:
                        pilmoji.text((col_positions[0], current_y - 30), rank_icons[i], font=medal_font, fill=font_color)
                    
                    name_x = col_positions[1]
                    if is_unbound_official:
                        badge_text = "未绑定QQ"
                        badge_text_width = pilmoji.getsize(badge_text, font=id_font)[0]
                        badge_width = badge_text_width + 16
                        badge_height = 26
                        badge_y = current_y + 3
                        draw = ImageDraw.Draw(img)
                        draw.rounded_rectangle(
                            [name_x, badge_y, name_x + badge_width, badge_y + badge_height],
                            radius=8,
                            fill=(115, 125, 150, 230),
                        )
                        pilmoji.text(
                            (name_x + 8, badge_y + 4),
                            badge_text,
                            font=id_font,
                            fill=(255, 255, 255, 255),
                        )
                        name_x += badge_width + 10

                    max_name_width = col_positions[2] - name_x - 20
                    if body_font.getbbox(display_name)[2] > max_name_width:
                        while body_font.getbbox(display_name + "...")[2] > max_name_width and len(display_name) > 0:
                            display_name = display_name[:-1]
                        display_name += "..."
                    
                    pilmoji.text((name_x, current_y), display_name, font=body_font, fill=font_color)
                    id_text = f"{user_name} ID: {user_id}"
                    max_id_width = col_positions[2] - col_positions[1] - 20
                    if id_font.getbbox(id_text)[2] > max_id_width:
                        while id_font.getbbox(id_text + "...")[2] > max_id_width and len(id_text) > 0:
                            id_text = id_text[:-1]
                        id_text += "..."
                    # ID 小字固定从玩家列左侧开始，不能跟随未绑定徽章后的名称位置右移。
                    pilmoji.text((col_positions[1], current_y + 32), id_text, font=id_font, fill=header_color)
                    pilmoji.text((col_positions[2], current_y), score, font=body_font, fill=score_color)
                    pilmoji.text((col_positions[3], current_y), accuracy, font=body_font, fill=accuracy_color)
                    pilmoji.text((col_positions[4], current_y), attempts, font=body_font, fill=font_color)

                    separator_y = current_y + 60
                    if i < len(rows) - 1:
                        draw = ImageDraw.Draw(img)
                        draw.line([(30, separator_y), (width - 30, separator_y)], fill=(200, 200, 210, 128), width=1)
                    
                    current_y += 70

                footer_text = f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                footer_y = height - 25
                pilmoji.text((center_x, footer_y), footer_text, font=id_font, fill=header_color, anchor="ms")

            os.makedirs(self.output_dir, exist_ok=True)
            img_path = self.output_dir / f"ranking_{time.time_ns()}.png"
            img.save(img_path)
            return str(img_path)

        except Exception as e:
            logger.error(f"渲染排行榜图片失败: {e}", exc_info=True)
            return None
            
    # --- 数据更新与检查 ---
    def _record_game_start(
        self,
        user_id: str,
        user_name: str,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ):
        """记录一次游戏开始，增加该用户的每日游戏次数"""
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            today = time.strftime("%Y-%m-%d")

            cursor.execute(
                "SELECT last_play_date, daily_plays FROM user_stats "
                "WHERE user_id = ? AND platform_name = ?",
                (user_id, platform_name),
            )
            user_data = cursor.fetchone()

            if user_data:
                last_play_date, daily_plays = user_data
                if last_play_date == today:
                    new_daily_plays = daily_plays + 1
                else:
                    new_daily_plays = 1
                
                cursor.execute(
                    "UPDATE user_stats SET user_name = ?, last_play_date = ?, daily_plays = ? "
                    "WHERE user_id = ? AND platform_name = ?",
                    (user_name, today, new_daily_plays, user_id, platform_name)
                )
            else:
                cursor.execute(
                    "INSERT INTO user_stats (user_id, user_name, platform_name, last_play_date, daily_plays) VALUES (?, ?, ?, ?, ?)",
                    (user_id, user_name, platform_name, today, 1)
                )
            conn.commit()

    def _update_stats(
        self,
        user_id: str,
        user_name: str,
        score: int,
        correct: bool,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ):
        """更新用户的得分和总尝试次数统计"""
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT score, attempts, correct_attempts FROM user_stats "
                "WHERE user_id = ? AND platform_name = ?",
                (user_id, platform_name),
            )
            user_data = cursor.fetchone()

            if user_data:
                new_score = user_data[0] + score
                new_attempts = user_data[1] + 1
                new_correct = user_data[2] + (1 if correct else 0)

                cursor.execute(
                    """
                    UPDATE user_stats 
                    SET score = ?, attempts = ?, correct_attempts = ?, user_name = ?
                    WHERE user_id = ? AND platform_name = ?
                    """,
                    (new_score, new_attempts, new_correct, user_name, user_id, platform_name),
                )
            else:
                today = time.strftime("%Y-%m-%d")
                cursor.execute(
                    "INSERT INTO user_stats (user_id, user_name, platform_name, score, attempts, correct_attempts, last_play_date, daily_plays) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, user_name, platform_name, score, 1, 1 if correct else 0, today, 0),
                )
            conn.commit()

    def _can_play(self, user_id: str, platform_name: str = DEFAULT_PLATFORM_NAME) -> bool:
        """检查用户今天是否还能玩，支持-1表示无限制"""
        daily_limit = self.config.get("daily_play_limit", 10)
        # 如果设置为-1，表示无限制
        if daily_limit == -1:
            return True
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            today = time.strftime("%Y-%m-%d")
            cursor.execute(
                "SELECT daily_plays, last_play_date FROM user_stats "
                "WHERE user_id = ? AND platform_name = ?",
                (user_id, platform_name),
            )
            user_data = cursor.fetchone()
            if user_data and user_data[1] == today:
                return user_data[0] < daily_limit
            return True

    def _reset_user_limit(self, user_id: str, platform_name: str = DEFAULT_PLATFORM_NAME) -> bool:
        """重置指定用户的每日游戏次数"""
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM user_stats WHERE user_id = ? AND platform_name = ?",
                (user_id, platform_name),
            )
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE user_stats SET daily_plays = 0 WHERE user_id = ? AND platform_name = ?",
                    (user_id, platform_name),
                )
                conn.commit()
                return True
            return False

    async def terminate(self):
        """插件卸载或停用时调用"""
        logger.info("正在关闭猜卡插件的后台任务...")

        # 取消并等待所有后台任务完成
        if self._background_tasks:
            tasks = list(self._background_tasks)
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.warning(f"等待任务完成时出错: {e}")
            self._background_tasks.clear()

        # 停止卡牌题库自动同步服务
        try:
            await self.master_data.terminate()
        except Exception as e:
            logger.warning(f"停止题库同步服务时出错: {e}")

        # 关闭 aiohttp session
        if self.http_session and not self.http_session.closed:
            try:
                await self.http_session.close()
                logger.info("aiohttp session已关闭。")
            except Exception as e:
                logger.error(f"关闭 aiohttp session 时出错: {e}")

        logger.info("猜卡面插件已终止。")
