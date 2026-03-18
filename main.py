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

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from datetime import datetime
from pilmoji import Pilmoji
from urllib.parse import urlparse

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
PLUGIN_AUTHOR = "慵懒午睡"
PLUGIN_DESCRIPTION = "PJSK猜卡面插件"
PLUGIN_VERSION = "1.2.0" 
PLUGIN_REPO_URL = "https://github.com/yonglanws/astrbot_plugin_pjsk_guess_card"


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

# --- 卡牌数据加载 ---
def load_card_data(resources_dir: Path) -> tuple[list[dict] | None, dict | None]:
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

        # 使用 context 初始化共享的游戏会话状态
        if not hasattr(self.context, "active_game_sessions"):
            self.context.active_game_sessions = set()
        
        # 添加会话初始化锁，防止并发创建多个 aiohttp ClientSession
        self.session_lock = asyncio.Lock()

        if not self.guess_cards or not self.characters_map:
            logger.error("插件初始化失败，缺少必要的卡牌数据文件。插件功能将受限。")
        
        if not aiohttp:
            logger.warning("`aiohttp` 模块未安装，远程图片功能将受限或性能较差。建议安装: pip install aiohttp")

        self._cleanup_task = None
        self._background_tasks: set = set()

        self._load_fonts()
        self._build_valid_answers_set()

        self._cleanup_output_dir()
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task())
        self._track_task(self._cleanup_task)

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

    async def _get_session(self) -> 'aiohttp.ClientSession' | None:
        """延迟初始化并获取 aiohttp session"""
        if not aiohttp:
            return None
        if self.http_session is None or self.http_session.closed:
            async with self.session_lock:
                # 再次检查，防止在等待锁的过程中已经被其他协程创建
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

        ping_url = None
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
            if ping_url:
                logger.warning(f"Stats ping to {ping_url} failed: {e}")
            else:
                logger.warning(f"Stats ping failed: {e}")

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

    def _get_resource_path_or_url(self, relative_path: str) -> Path | str | None:
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

    async def _open_image(self, image_source: Path | str) -> Image.Image | None:
        """打开一个资源图片，无论是本地路径还是远程URL。"""
        if not image_source:
            return None
        
        try:
            if isinstance(image_source, str) and image_source.startswith(('http://', 'https://')):
                session = await self._get_session()
                if not session:
                    logger.error("无法获取远程图片: `aiohttp` 模块未安装。")
                    return None
                
                # 设置下载超时和最大文件大小
                max_size = 10 * 1024 * 1024  # 10MB
                
                async with session.get(image_source, timeout=10) as response:
                    response.raise_for_status() # Will raise an error for non-200 status
                    
                    # 检查文件大小
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) > max_size:
                        logger.error(f"远程图片过大: {content_length} bytes，超过限制 {max_size} bytes")
                        return None
                    
                    # 分块读取，防止内存溢出
                    image_data = b''
                    async for chunk in response.content.iter_chunked(8192):
                        image_data += chunk
                        if len(image_data) > max_size:
                            logger.error(f"远程图片下载超过大小限制 {max_size} bytes")
                            return None
                    
                    return Image.open(io.BytesIO(image_data))
            else:
                return Image.open(image_source)
        except Exception as e:
            logger.error(f"无法打开图片资源 {image_source}: {e}", exc_info=True)
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



    def _cleanup_output_dir(self, max_age_seconds: int = 3600):
        """清理旧的排行榜图片和模糊处理的图片"""
        if not self.output_dir.exists():
            return
            
        now = time.time()
        try:
            for filename in os.listdir(self.output_dir):
                file_path = self.output_dir / filename
                # 确保只删除本插件生成的 png 和 jpg 图片 (包括排行榜、优化后的答案图和模糊处理的图片)
                if file_path.is_file() and (
                    filename.startswith("ranking_") or 
                    filename.startswith("answer_") or
                    filename.startswith("blurred_")
                ) and (filename.endswith(".png") or filename.endswith(".jpg")):
                    file_mtime = file_path.stat().st_mtime
                    if (now - file_mtime) > max_age_seconds:
                        os.remove(file_path)
                        logger.info(f"已清理旧图片: {filename}")
        except Exception as e:
            logger.error(f"清理图片时出错: {e}")
    
    def _apply_gaussian_blur_sync(self, image_source: Path | str) -> str | None:
        """对图片应用高斯模糊处理（同步版本）"""
        try:
            if isinstance(image_source, str) and image_source.startswith(('http://', 'https://')):
                return None
            
            with Image.open(image_source) as img:
                if not img:
                    return None
                
                blur_radius = 25
                blurred_img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
                
                os.makedirs(self.output_dir, exist_ok=True)
                img_path = self.output_dir / f"blurred_{time.time_ns()}.png"
                blurred_img.save(img_path)
                
                return str(img_path)
        except Exception as e:
            logger.error(f"应用高斯模糊失败: {e}")
            return None

    async def _apply_gaussian_blur(self, image_source: Path | str) -> str | None:
        """对图片应用高斯模糊处理（异步包装）"""
        if isinstance(image_source, str) and image_source.startswith(('http://', 'https://')):
            img = await self._open_image(image_source)
            if not img:
                return None
            blur_radius = 25
            blurred_img = await asyncio.to_thread(img.filter, ImageFilter.GaussianBlur(radius=blur_radius))
            os.makedirs(self.output_dir, exist_ok=True)
            img_path = self.output_dir / f"blurred_{time.time_ns()}.png"
            await asyncio.to_thread(blurred_img.save, img_path)
            return str(img_path)
        else:
            return await asyncio.to_thread(self._apply_gaussian_blur_sync, image_source)

    # --- 游戏逻辑 ---
    def start_new_game(self) -> dict | None:
        """准备一轮新游戏，加入花前/花后逻辑"""
        if not self.guess_cards or not self.characters_map:
            logger.error("无法开始游戏，因为卡牌数据未成功加载。")
            return None

        card_pool = self.guess_cards
        card = random.choice(card_pool)
        card_type = random.choice(["normal", "after_training"])
        
        # 使用原始卡面图片作为问题图片
        answer_image_filename = f"card_{card_type}.png"

        # 当使用本地资源时，检查图片是否存在
        if self.config.get("use_local_resources", True):
            answer_image_path = self.resources_dir / "member" / card["assetbundleName"] / answer_image_filename
            if not answer_image_path.exists():
                logger.error(f"卡面图片未找到: {answer_image_path}")
                return None

        character = self.characters_map.get(card["characterId"])
        if not character:
            logger.error(f"未找到ID为 {card['characterId']} 的角色")
            return None

        # 统一分数计算，每次操作增加1分
        base_score = 1
        
        show_rarity_hint = random.choice([True, False])
        show_training_hint = random.choice([True, False])

        # 获取卡面图片路径
        
        return {
            "card": card,
            "card_state": card_type,
            "card_image_source": self._get_resource_path_or_url(f'member/{card["assetbundleName"]}/{answer_image_filename}'),
            "character": character,
            "score": base_score,
            "show_rarity_hint": show_rarity_hint,
            "show_training_hint": show_training_hint,
        }

    # --- 指令处理 ---
    @filter.command("pjsk猜卡面", alias={"猜卡", "gc","猜卡面"})
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
                yield event.plain_result("当前已经有一个游戏在进行中啦~ 等它结束后再来玩吧！")
                return

            cooldown = self.config.get("game_cooldown_seconds", 60)
            last_end_time = self.last_game_end_time.get(session_id, 0)
            time_since_last_game = time.time() - last_end_time

            if time_since_last_game < cooldown:
                remaining_time = cooldown - time_since_last_game
                time_display = f"{remaining_time:.3f}" if remaining_time < 1 else str(int(remaining_time))
                yield event.plain_result(f"让我们休息一下吧！{time_display}秒后再来玩哦~ 😊")
                return

            if not self._can_play(event.get_sender_id()):
                yield event.plain_result(f"今天的游戏次数已经用完啦~ 明天再来玩吧！每天最多可以玩{self.config.get('daily_play_limit', 10)}次哦~ ✨")
                return
            
            # 标记游戏会话为活动状态，然后释放锁
            self.context.active_game_sessions.add(session_id)

        try:
            # 记录游戏开始，并增加该用户的每日游戏次数
            self._record_game_start(event.get_sender_id(), event.get_sender_name())

            # --- 新增：发送统计信标 ---
            self._track_task(asyncio.create_task(self._send_stats_ping("guess_card")))

            game_data = self.start_new_game()
            if not game_data:
                yield event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误，请联系管理员。")
                return

            # 对卡面图片应用高斯模糊处理
            card_image_source = game_data.get("card_image_source")
            blurred_image_path = await self._apply_gaussian_blur(card_image_source)
            
            if not blurred_image_path:
                yield event.plain_result("哎呀，处理图片时遇到了一点小问题呢~ 游戏暂时中断了，稍后再试试吧！")
                return

            # 在后台日志中输出答案，方便测试
            logger.info(f"[猜卡插件] 新游戏开始. 答案: {game_data['character']['fullNameChinese']}")
                
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
            
            intro_text = f"嗨嗨！来玩猜卡游戏吧！\n请在{timeout_seconds}秒内发送角色名称缩写进行回答哦\n"
            
            hint_text = "\n".join(hints) + "\n" if hints else ""
            
            msg_chain: list = [Comp.Plain(intro_text + hint_text)]

            try:
                # 发送模糊处理后的图片
                if blurred_image_path:
                    msg_chain.append(Comp.Image(file=blurred_image_path))
                yield event.chain_result(msg_chain)
            except Exception as e:
                logger.error(f"......发送图片失败: {e}. Check if the file path is correct and accessible.")
                yield event.plain_result("......发送问题图片时出错，游戏中断。")
                return
            
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
                answer_name = re.sub(r"^[!！]", "", answer_text).lower()

                if answer_name:
                    # 验证输入是否为有效的角色名称或别名
                    if answer_name not in self.valid_answers:
                        # 输入不是有效的角色名称或别名，忽略该输入
                        return
                        
                    guess_attempts_count += 1
                    try:
                        correct_name_abbr = game_data["character"]["name"].lower()
                        correct_name_chinese = game_data["character"]["fullNameChinese"].lower()
                        aliases = game_data["character"].get("aliases", [])
                        
                        # 检查是否匹配任何一个可能的答案（缩写、中文名称、别名）
                        is_correct = answer_name == correct_name_abbr or answer_name == correct_name_chinese
                        
                        # 检查是否匹配任何一个别名
                        if not is_correct:
                            for alias in aliases:
                                if answer_name == alias.lower():
                                    is_correct = True
                                    break

                        if is_correct:
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
            correct_name = game_data['character']['fullNameChinese']

            text_msg = []
            if winner_info:
                text_msg.append(Comp.Plain(f"{winner_info['name']}答对了呢!获得了{winner_info['score']}分！继续加油哦~\n正确答案是: {correct_name}"))
            elif game_ended_by_attempts:
                text_msg.append(Comp.Plain(f"哎呀，本轮猜测次数已经用完了呢~ 没关系，下次一定可以的！\n"))
                text_msg.append(Comp.Plain(f"正确答案是: {correct_name}\n"))
            elif game_ended_by_timeout:
                text_msg.append(Comp.Plain("时间到啦~ 大家有没有猜出来呢？\n"))
                text_msg.append(Comp.Plain(f"正确答案是: {correct_name}\n"))
            
            if text_msg:
                yield event.chain_result(text_msg)

            # 使用原始卡面图片作为答案图片
            card_image_source = game_data.get("card_image_source")
            image_msg = []
            if card_image_source:
                # 处理远程图片，确保发送本地文件
                if isinstance(card_image_source, str) and card_image_source.startswith(('http://', 'https://')):
                    # 下载远程图片到本地
                    try:
                        img = await self._open_image(card_image_source)
                        if img:
                            os.makedirs(self.output_dir, exist_ok=True)
                            img_path = self.output_dir / f"answer_{time.time_ns()}.png"
                            await asyncio.to_thread(img.save, img_path)
                            image_msg.append(Comp.Image(file=str(img_path)))
                    except Exception as e:
                        logger.error(f"下载远程答案图片失败: {e}")
                else:
                    # 本地图片直接发送
                    image_msg.append(Comp.Image(file=str(card_image_source)))
            
            if image_msg:
                yield event.chain_result(image_msg)

        finally:
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)
            # 不再删除锁实例，保留锁以维持互斥机制


    @filter.command("猜卡面帮助")
    async def show_guess_card_help(self, event: AstrMessageEvent):
        """显示猜卡插件帮助"""
        if not self._is_group_allowed(event):
            return
        help_text = (
            "✨ PJSK猜卡面指南 ✨\n\n"
            "基础指令\n"
            "pjsk猜卡面 - 随机猜一张卡，看看你能不能认出来！\n\n"
            "数据统计\n"
            "猜卡面排行榜 - 查看猜卡总分排行榜，看看谁是猜卡大师！\n"
            "猜卡面分数 - 查看自己的猜卡数据统计，了解自己的进步~\n\n"
        )
        yield event.plain_result(help_text)


    @filter.command("猜卡面分数", alias={"gcscore", "猜卡分数"})
    async def show_user_score(self, event: AstrMessageEvent):
        """显示玩家自己的猜卡积分和统计数据"""
        if not self._is_group_allowed(event):
            return
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        with closing(self.get_conn()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT score, attempts, correct_attempts, last_play_date, daily_plays FROM user_stats WHERE user_id = ?", (user_id,))
            user_data = cursor.fetchone()
            
        if not user_data:
            yield event.plain_result(f"{user_name}，你还没有参与过猜卡游戏哦！快来一起玩呀~ 🎮")
            return
            
        score, attempts, correct_attempts, _, _ = user_data
        accuracy = (correct_attempts * 100 / attempts) if attempts > 0 else 0
        
        with closing(self.get_conn()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM user_stats WHERE score > ?", (score,))
            rank = cursor.fetchone()[0] + 1
        
        stats_text = (
            f"✨ {user_name} 的猜卡数据 ✨\n"
            f"🏆 总分: {score} 分\n"
            f"🎯 正确率: {accuracy:.1f}%\n"
            f"🎮 游戏次数: {attempts} 次\n"
            f"✅ 答对次数: {correct_attempts} 次\n"
            f"🏅 当前排名: 第 {rank} 名\n"
        )
        
        yield event.plain_result(stats_text)


    @filter.command("重置猜卡面次数", alias={"resetgl"})
    async def reset_guess_limit(self, event: AstrMessageEvent):
        """重置用户猜卡次数（仅限管理员）"""
        if not self._is_group_allowed(event):
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


    @filter.command("猜卡面排行榜", alias={"gcrank", "gctop"})
    async def show_ranking(self, event: AstrMessageEvent):
        """显示猜卡排行榜"""
        if not self._is_group_allowed(event):
            return

        self._cleanup_output_dir()

        with closing(self.get_conn()) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, user_name, score, attempts, correct_attempts FROM user_stats ORDER BY score DESC LIMIT 10"
            )
            rows = cursor.fetchall()

        if not rows:
            yield event.plain_result("还没有人参与过猜卡游戏呢~ 快来成为第一个玩家吧！✨")
            return

        try:
            img_path = await asyncio.to_thread(self._render_ranking_image, rows)
            if img_path:
                yield event.image_result(str(img_path))
            else:
                yield event.plain_result("生成排行榜图片时出错，请联系管理员。")
        except Exception as e:
            logger.error(f"使用Pillow生成排行榜图片失败: {e}", exc_info=True)
            yield event.plain_result("生成排行榜图片时出错，请联系管理员。")

    def _render_ranking_image(self, rows: list) -> str | None:
        """渲染排行榜图片（同步方法）"""
        try:
            width, height = 650, 950

            bg_color_start = (230, 240, 255)
            bg_color_end = (200, 210, 240)
            img = Image.new("RGB", (width, height), bg_color_start)
            draw_bg = ImageDraw.Draw(img)
            for y in range(height):
                r = int(bg_color_start[0] + (bg_color_end[0] - bg_color_start[0]) * y / height)
                g = int(bg_color_start[1] + (bg_color_end[1] - bg_color_start[1]) * y / height)
                b = int(bg_color_start[2] + (bg_color_end[2] - bg_color_start[2]) * y / height)
                draw_bg.line([(0, y), (width, y)], fill=(r, g, b))
            
            background_path = self.resources_dir / "ranking_bg.png"
            if background_path.exists():
                try:
                    with Image.open(background_path) as custom_bg:
                        custom_bg = custom_bg.convert("RGBA")
                        custom_bg = custom_bg.resize((width, height), LANCZOS)
                        custom_bg.putalpha(128)
                        img = img.convert("RGBA")
                        img = Image.alpha_composite(img, custom_bg)
                except Exception as e:
                    logger.warning(f"加载或混合自定义背景图片失败: {e}. 将仅使用默认背景。")

            if img.mode != 'RGBA':
                img = img.convert('RGBA')

            white_overlay = Image.new("RGBA", img.size, (255, 255, 255, 100))
            img = Image.alpha_composite(img, white_overlay)

            title_text = "PJSK猜卡排行榜"
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
                col_positions_header = [40, 120, 320, 450, 560]
                title_height = pilmoji.getsize(title_text, font=title_font)[1]
                current_y = title_y + int(title_height / 2) + 45
                for header in headers:
                    pilmoji.text((col_positions_header.pop(0), current_y), header, font=header_font, fill=header_color)

                current_y += 55

                rank_icons = ["🥇", "🥈", "🥉"]
                for i, row in enumerate(rows):
                    user_id, user_name, score, attempts, correct_attempts = str(row[0]), row[1], str(row[2]), str(row[3]), row[4]
                    accuracy = f"{(correct_attempts * 100 / int(attempts) if int(attempts) > 0 else 0):.1f}%"
                    
                    rank = i + 1
                    col_positions = [40, 120, 320, 450, 560]
                    rank_num_align_x = 100

                    pilmoji.text((rank_num_align_x, current_y), str(rank), font=body_font, fill=font_color, anchor="ra")

                    if i < 3:
                        pilmoji.text((col_positions[0], current_y - 30), rank_icons[i], font=medal_font, fill=font_color)
                    
                    max_name_width = col_positions[2] - col_positions[1] - 20
                    if body_font.getbbox(user_name)[2] > max_name_width:
                        while body_font.getbbox(user_name + "...")[2] > max_name_width and len(user_name) > 0:
                            user_name = user_name[:-1]
                        user_name += "..."
                    
                    pilmoji.text((col_positions[1], current_y), user_name, font=body_font, fill=font_color)
                    pilmoji.text((col_positions[1], current_y + 32), f"ID: {user_id}", font=id_font, fill=header_color)
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
    def _record_game_start(self, user_id: str, user_name: str):
        """记录一次游戏开始，增加该用户的每日游戏次数"""
        with closing(self.get_conn()) as conn:
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
                cursor.execute(
                    "INSERT INTO user_stats (user_id, user_name, last_play_date, daily_plays) VALUES (?, ?, ?, ?)",
                    (user_id, user_name, today, 1)
                )
            conn.commit()

    def _update_stats(self, user_id: str, user_name: str, score: int, correct: bool):
        """更新用户的得分和总尝试次数统计"""
        with closing(self.get_conn()) as conn:
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
                today = time.strftime("%Y-%m-%d")
                cursor.execute(
                    "INSERT INTO user_stats (user_id, user_name, score, attempts, correct_attempts, last_play_date, daily_plays) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, user_name, score, 1, 1 if correct else 0, today, 0),
                )
            conn.commit()

    def _can_play(self, user_id: str) -> bool:
        """检查用户今天是否还能玩"""
        daily_limit = self.config.get("daily_play_limit", 10)
        with closing(self.get_conn()) as conn:
            cursor = conn.cursor()
            today = time.strftime("%Y-%m-%d")
            cursor.execute("SELECT daily_plays, last_play_date FROM user_stats WHERE user_id = ?", (user_id,))
            user_data = cursor.fetchone()
            if user_data and user_data[1] == today:
                return user_data[0] < daily_limit
            return True

    def _reset_user_limit(self, user_id: str) -> bool:
        """重置指定用户的每日游戏次数"""
        with closing(self.get_conn()) as conn:
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
        
        # 关闭 aiohttp session
        if self.http_session and not self.http_session.closed:
            try:
                await self.http_session.close()
                logger.info("aiohttp session已关闭。")
            except Exception as e:
                logger.error(f"关闭 aiohttp session 时出错: {e}")
        
        logger.info("猜卡插件已终止。")
