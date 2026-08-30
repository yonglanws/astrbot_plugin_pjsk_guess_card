"""卡牌题库数据服务（学习 PJSK Wordle 的 master 自动同步机制）。

数据来源与职责：
- 日服卡牌题库: https://github.com/Team-Haruki/haruki-sekai-master   (master/cards.json)
- 国服卡牌题库: https://github.com/Team-Haruki/haruki-sekai-sc-master (master/cards.json)
- 题库版本号取 versions/current_version.json 的 dataVersion（如 6.8.0.12）。

拉取策略：一律优先 GitHub Contents API。cards.json 超过 1MB 时 base64 通道拿不到内容，
自动改用同一 API 的 raw 媒体类型（支持 1~100MB 文件，日服 cards.json 约 34MB）；
GitHub API 整体不可用时回退 jsDelivr CDN（仅支持 20MB 以下文件）。

为节省流量：每次周期检查先拉取很小的 current_version.json，若 dataVersion 没有变化
（新卡牌只会随游戏数据版本更新上架），则跳过大文件重新下载。

所有文件持久化在 plugin_data 目录，每 24 小时自动检查一次；
基于原始数据构建派生题库 derived.json（仅 rarity_3/rarity_4 的可猜卡牌），
游戏运行时只读派生题库。角色数据与服务器无关，仍使用插件自带 characters.json。
"""

import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path

import aiohttp

try:  # 在 AstrBot 内使用其 logger，脱离 AstrBot（单测）时退回标准 logging
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_pjsk_guess_card")

SERVER_JP = "jp"
SERVER_SC = "sc"
SERVER_LABELS = {SERVER_JP: "日服", SERVER_SC: "国服"}

_SERVER_FILES = {
    SERVER_JP: [
        ("master/cards.json", "cards.json"),
        ("versions/current_version.json", "current_version.json"),
    ],
    SERVER_SC: [
        ("master/cards.json", "cards.json"),
        ("versions/current_version.json", "current_version.json"),
    ],
}

_GITHUB_REPOS = {
    SERVER_JP: "Team-Haruki/haruki-sekai-master",
    SERVER_SC: "Team-Haruki/haruki-sekai-sc-master",
}
_GITHUB_BRANCH = "main"

_JSDELIVR_TPL = "https://cdn.jsdelivr.net/gh/{repo}@{branch}/{path}"

# 派生题库构建规则版本：规则变更时 +1，启动时用本地原始文件离线重建
DERIVED_RULE = 1

# 参与猜卡的卡牌稀有度（与游戏玩法一致：仅 ⭐⭐⭐ / ⭐⭐⭐⭐）
_PLAYABLE_RARITIES = ("rarity_3", "rarity_4")


class MasterDataService:
    """卡牌题库下载、缓存、派生与查询。"""

    def __init__(
        self,
        data_dir: Path,
        update_interval_hours: int = 24,
        request_timeout: int = 120,
    ):
        self.data_dir = Path(data_dir)
        self.card_dir = self.data_dir / "carddata"
        self.card_dir.mkdir(parents=True, exist_ok=True)
        self.update_interval_hours = max(1, int(update_interval_hours))
        self.request_timeout = request_timeout

        self.meta_path = self.card_dir / "meta.json"
        self.meta: dict = {}

        self.cards: dict[str, list[dict]] = {SERVER_JP: [], SERVER_SC: []}

        self._fetch_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._update_task: asyncio.Task | None = None
        # 题库更新后的回调（由插件主流程注入）
        self.on_cards_updated = None

    # ---------- 路径辅助 ----------

    def _server_dir(self, server: str) -> Path:
        return self.card_dir / server

    @property
    def derived_path(self) -> dict[str, Path]:
        return {s: self._server_dir(s) / "derived.json" for s in (SERVER_JP, SERVER_SC)}

    # ---------- 生命周期 ----------

    async def start(self):
        """启动：加载本地缓存，缺失则立即拉取，并启动 24h 周期更新任务。"""
        self.card_dir.mkdir(parents=True, exist_ok=True)
        self._load_meta()

        loaded_any = False
        for server in (SERVER_JP, SERVER_SC):
            if not self._load_derived(server):
                continue
            loaded_any = True
            # 派生规则升级时，用本地缓存的原始文件离线重建
            info = self.meta.setdefault("servers", {}).get(server, {})
            if info.get("derived_rule") != DERIVED_RULE:
                if not self._rebuild_from_local(server):
                    # 本地原始文件不全，清除时间戳以便周期任务重新拉取
                    info.pop("updated_at", None)
                    self._save_meta()
        if loaded_any:
            logger.info("[PJSK猜卡面] 本地卡牌题库加载完成。")

        self._update_task = asyncio.create_task(self._update_loop())

    async def terminate(self):
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except (asyncio.CancelledError, Exception):
                pass
            self._update_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---------- 对外查询接口 ----------

    def get_cards(self, server: str) -> list[dict]:
        return self.cards.get(server, [])

    def get_card_count(self, server: str) -> int:
        return len(self.cards.get(server, []))

    def get_version(self, server: str) -> str:
        """题库版本号：优先游戏数据版本 dataVersion（如 6.8.0.12），否则更新日期。"""
        info = self.meta.get("servers", {}).get(server, {})
        version = info.get("version")
        if version:
            return str(version)
        updated = info.get("updated_at")
        if updated:
            return datetime.fromtimestamp(updated).strftime("%Y%m%d")
        return "unknown"

    def is_ready(self, server: str) -> bool:
        return bool(self.cards.get(server))

    # ---------- 周期更新 ----------

    async def _update_loop(self):
        """每 30 分钟检查一次，超过更新间隔（默认 24h）的数据源自动重新拉取。"""
        try:
            await self.refresh_if_stale()
        except Exception as e:
            logger.error(f"[PJSK猜卡面] 首次题库检查失败: {e}", exc_info=True)
        while True:
            await asyncio.sleep(1800)
            try:
                await self.refresh_if_stale()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[PJSK猜卡面] 周期题库更新失败: {e}", exc_info=True)

    async def refresh_if_stale(self, force: bool = False):
        """按 24h 间隔刷新所有数据源；force=True 时无视间隔与版本守卫强制刷新。"""
        async with self._fetch_lock:
            now = time.time()
            interval = self.update_interval_hours * 3600
            stale_servers = [
                server
                for server in (SERVER_JP, SERVER_SC)
                if force
                or not self.cards.get(server)
                or now - self.meta.get("servers", {}).get(server, {}).get("updated_at", 0)
                >= interval
            ]

            updated = False
            for server in stale_servers:
                try:
                    if await self._update_server(server, force=force):
                        updated = True
                except Exception as e:
                    logger.error(
                        f"[PJSK猜卡面] {SERVER_LABELS[server]}题库更新失败: {e}",
                        exc_info=True,
                    )

            if updated:
                self._save_meta()
                self._notify_updated()

    def _notify_updated(self):
        if self.on_cards_updated:
            try:
                result = self.on_cards_updated()
                if asyncio.iscoroutine(result):
                    asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                try:
                    result.close()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[PJSK猜卡面] 题库更新回调执行失败: {e}")

    # ---------- 单服务器题库更新 ----------

    async def _update_server(self, server: str, force: bool = False) -> bool:
        """更新单个服务器的卡牌题库，返回题库是否有变化。"""
        repo = _GITHUB_REPOS[server]

        # 先拉取很小的版本文件做版本守卫：dataVersion 未变则跳过大文件下载
        version_data, _ = await self._fetch_remote_json(
            repo, "versions/current_version.json", small=True
        )
        data_version = str((version_data or {}).get("dataVersion") or "").strip()

        info = self.meta.setdefault("servers", {}).setdefault(server, {})
        if (
            not force
            and data_version
            and info.get("data_version") == data_version
            and self.cards.get(server)
            and info.get("derived_rule") == DERIVED_RULE
        ):
            if info.get("updated_at", 0) < time.time() - 300:
                info["updated_at"] = time.time()
                self._save_meta()
            return False

        parsed: dict = {}
        for remote_path, local_name in _SERVER_FILES[server]:
            if local_name == "current_version.json" and version_data is not None:
                parsed[local_name] = version_data
                continue
            data, _ = await self._fetch_remote_json(repo, remote_path, small=False)
            if data is None:
                raise RuntimeError(f"拉取 {repo}/{remote_path} 失败（所有通道）")
            parsed[local_name] = data
            await asyncio.sleep(0.3)  # 轻微限速，尊重匿名 API 配额

        self._store_server(server, parsed, data_version, via="github")
        return True

    def _rebuild_from_local(self, server: str) -> bool:
        """用本地缓存的原始 JSON 离线重建派生题库（用于派生规则升级）。"""
        sdir = self._server_dir(server)
        parsed: dict = {}
        for _, local_name in _SERVER_FILES[server]:
            f = sdir / local_name
            if not f.exists():
                logger.warning(
                    f"[PJSK猜卡面] 本地缺少 {SERVER_LABELS[server]}/{local_name}，跳过离线重建"
                )
                return False
            try:
                parsed[local_name] = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[PJSK猜卡面] 本地文件 {local_name} 解析失败: {e}")
                return False
        try:
            info = self.meta.get("servers", {}).get(server, {})
            self._store_server(
                server,
                parsed,
                str(info.get("data_version") or ""),
                via="local-rebuild",
            )
            return True
        except Exception as e:
            logger.warning(f"[PJSK猜卡面] {SERVER_LABELS[server]}离线重建失败: {e}")
            return False

    @staticmethod
    def _atomic_write_json(path: Path, data, *, indent: int | None = None) -> None:
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8"
        )
        temp_path.replace(path)

    def _store_server(self, server: str, parsed: dict, data_version: str, via: str):
        """落盘原始文件并重建派生题库。"""
        sdir = self._server_dir(server)
        sdir.mkdir(parents=True, exist_ok=True)
        for name, data in parsed.items():
            self._atomic_write_json(sdir / name, data)

        cards = self._build_derived(parsed)
        if not cards:
            raise RuntimeError(f"{SERVER_LABELS[server]}卡牌题库解析结果为空")

        self.cards[server] = cards
        self._atomic_write_json(self.derived_path[server], cards)

        display_version = data_version or datetime.now().strftime("%Y%m%d")
        servers = self.meta.setdefault("servers", {})
        prev = servers.get(server, {})
        servers[server] = {
            "version": display_version,
            "data_version": data_version or None,
            "updated_at": time.time(),
            "via": via,
            "count": len(cards),
            "derived_rule": DERIVED_RULE,
        }
        # 版本守卫依赖 data_version，落盘 meta 交由调用方统一保存
        self._save_meta()
        logger.info(
            f"[PJSK猜卡面] {SERVER_LABELS[server]}卡牌题库已更新: "
            f"{len(cards)} 张, 版本 {display_version} (via {via})"
        )
        _ = prev

    # ---------- 派生题库构建 ----------

    @staticmethod
    def _build_derived(parsed: dict) -> list[dict]:
        cards = parsed.get("cards.json", [])
        result: list[dict] = []
        for c in cards:
            if c.get("cardRarityType") not in _PLAYABLE_RARITIES:
                continue
            card_id = c.get("id")
            bundle = c.get("assetbundleName")
            if card_id is None or not bundle or not c.get("characterId"):
                continue
            result.append(
                {
                    "id": card_id,
                    "characterId": c.get("characterId"),
                    "cardRarityType": c.get("cardRarityType"),
                    "assetbundleName": bundle,
                }
            )
        result.sort(key=lambda x: x["id"])
        return result

    # ---------- 本地加载 ----------

    def _load_meta(self):
        if not self.meta_path.exists():
            self.meta = {}
            return
        try:
            self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[PJSK猜卡面] meta.json 解析失败，将重建: {e}")
            self.meta = {}

    def _save_meta(self):
        self.card_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(self.meta_path, self.meta, indent=1)

    def _load_derived(self, server: str) -> bool:
        path = self.derived_path[server]
        if not path.exists():
            return False
        try:
            cards = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cards, list) or not cards:
                return False
            self.cards[server] = cards
            return True
        except Exception as e:
            logger.warning(f"[PJSK猜卡面] 加载 {SERVER_LABELS[server]}派生题库失败: {e}")
            return False

    # ---------- 网络层 ----------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                headers={
                    "User-Agent": "astrbot_plugin_pjsk_guess_card (+https://github.com/AstrBotDevs/AstrBot)"
                },
            )
        return self._session

    async def _fetch_url(self, url: str) -> bytes:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _fetch_remote_json(
        self, repo: str, remote_path: str, small: bool
    ) -> tuple[object | None, str | None]:
        """优先 GitHub Contents API 拉取 JSON，返回 (解析结果, blob sha)。

        small=True（1MB 以下文件）走 base64 通道；small=False（大文件）先按 base64
        探测，拿不到内容时自动改用同一 API 的 raw 媒体类型（支持 1~100MB）。
        GitHub API 不可用时回退 jsDelivr（仅 20MB 以下文件可回退）。
        解析失败或全部通道失败时返回 (None, None)，由调用方决定是否致命。
        """
        api_url = f"https://api.github.com/repos/{repo}/contents/{remote_path}?ref={_GITHUB_BRANCH}"
        session = await self._get_session()

        # 1) base64 通道（同时拿到 blob sha）
        try:
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    content = payload.get("content")
                    encoding = payload.get("encoding", "base64")
                    sha = payload.get("sha")
                    if content and encoding == "base64":
                        return json.loads(base64.b64decode(content)), sha
                    if small:
                        logger.warning(
                            f"[PJSK猜卡面] GitHub API 未返回 {repo}/{remote_path} 的内容，回退 jsDelivr"
                        )
                    # 大文件 content 为空（超过 1MB），进入 raw 通道
                elif resp.status == 403:
                    logger.warning(
                        "[PJSK猜卡面] GitHub API 限流(403)。"
                        "提示: 匿名限额 60 次/小时/IP，可等待配额恢复。"
                    )
                else:
                    logger.warning(
                        f"[PJSK猜卡面] GitHub API 返回 {resp.status}: {repo}/{remote_path}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[PJSK猜卡面] GitHub API 拉取失败({repo}/{remote_path}): {e}")

        # 2) raw 媒体类型通道（1MB ~ 100MB 文件）
        if not small:
            try:
                async with session.get(api_url, headers={"Accept": "application/vnd.github.raw"}) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return json.loads(data.decode("utf-8")), None
                    logger.warning(
                        f"[PJSK猜卡面] GitHub API raw 通道返回 {resp.status}: {repo}/{remote_path}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"[PJSK猜卡面] GitHub API raw 通道拉取失败({repo}/{remote_path}): {e}"
                )

        # 3) jsDelivr 回退（超过 20MB 的文件 jsDelivr 会拒绝，属预期失败）
        jsd_url = _JSDELIVR_TPL.format(repo=repo, branch=_GITHUB_BRANCH, path=remote_path)
        try:
            data = await self._fetch_url(jsd_url)
            return json.loads(data.decode("utf-8")), None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[PJSK猜卡面] jsDelivr 回退失败({repo}/{remote_path}): {e}")
            return None, None
