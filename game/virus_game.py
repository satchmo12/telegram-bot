import random
import time
from typing import Optional

from utils import CONFIG_FILE, GAME_FILE, load_json, save_json


# ============================================================
# 默认配置
# ============================================================

DEFAULT_CONFIG = {
    "enabled": True,

    # 游戏默认持续时间，单位：分钟
    "duration": 30,

    # 开始后至少多少个活跃玩家才爆发
    "min_active_players": 5,

    # 初始感染者需要成功传播多少人才能痊愈
    "spread_required": 2,

    # 被多少个不同感染源感染后直接死亡
    "death_threshold": 3,

    # 初始感染者如果多久没有传播，自动更换
    "first_spread_timeout": 120,

    # 解药
    "antidote": {
        "enabled": True,

        # 两次神秘咒语之间的随机间隔，单位：秒
        "min_interval": 300,
        "max_interval": 3000,

        # 神秘咒语出现后持续多久
        "duration": 120,

        # 后台配置的神秘咒语
        "items": [
            "我是小狗汪汪汪",
            "好痒啊",
            "用力啊",
            "啊啊啊",
            "我是疯子",
            "快点蹂躏我",
            "好舒服好满足"
        ]
    }
}


# ============================================================
# 游戏管理器
# ============================================================

class VirusGameManager:

    def __init__(self):

        self.games = load_json(GAME_FILE)
        self.config = load_json(CONFIG_FILE)

        if not isinstance(self.games, dict):
            self.games = {}

        if not isinstance(self.config, dict) or not self.config:
            self.config = DEFAULT_CONFIG.copy()

        self._tasks = {}

    # ========================================================
    # 保存
    # ========================================================

    def save(self):

        save_json(
            GAME_FILE,
            self.games
        )

        save_json(
            CONFIG_FILE,
            self.config
        )

    # ========================================================
    # 获取游戏
    # ========================================================

    def get_game(
        self,
        chat_id: int
    ):

        return self.games.get(
            str(chat_id)
        )

    # ========================================================
    # 是否正在游戏
    # ========================================================

    def is_running(
        self,
        chat_id: int
    ) -> bool:

        game = self.get_game(chat_id)

        return bool(
            game
            and game.get("running", False)
        )

    # ========================================================
    # 初始化玩家
    # ========================================================

    def _new_player(
        self,
        user
    ):

        return {
            "user_id": user.id,
            "name": user.full_name,

            # healthy / infected / dead
            "status": "healthy",

            # 累计统计
            "infection_count": 0,
            "spread_count": 0,
            "death_count": 0,

            # 成功使用解药次数
            "antidote_discover_count": 0,

            # 当前这一轮被哪些不同感染源感染
            "infected_by": [],

            # 当前感染者已经感染过哪些目标
            "infected_targets": [],

            # 当前这一轮痊愈进度
            "recovery_progress": 0,

            # 当前需要传播多少人才能痊愈
            "recovery_required": 0,
        }

    # ========================================================
    # 开始游戏
    # ========================================================

    def start_game(
        self,
        chat_id: int,
        duration_minutes: Optional[int] = None
    ):

        chat_key = str(chat_id)

        if self.is_running(chat_id):

            return (
                False,
                "当前已经有一场病毒游戏正在进行。"
            )

        duration = (
            duration_minutes
            or self.config.get(
                "duration",
                DEFAULT_CONFIG["duration"]
            )
        )

        now = time.time()

        game = {

            "chat_id": chat_id,

            "running": True,

            # waiting：
            # 游戏已经开始，但病毒还没有爆发
            #
            # running：
            # 已经产生初始感染者
            "phase": "waiting",

            "start_time": now,

            "end_time": (
                now + duration * 60
            ),

            # 游戏开始后发言的人
            "active_players": {},

            # 所有参加过游戏的人
            "players": {},

            # 传播记录
            "spread_records": [],

            # source -> target
            "infection_links": {},

            # 初始感染者
            "initial_infected": None,

            "stats": {
                "total_infections": 0,
                "total_spreads": 0,
                "total_deaths": 0,
                "total_antidotes": 0,
                "total_antidote_uses": 0
            },

            # =================================================
            # 当前神秘咒语
            # =================================================

            "antidote": {
                "active": False,
                "text": None,

                # 保留兼容旧数据
                "message_id": None,

                "expire_time": 0
            },

            "last_antidote_time": 0,

            # =================================================
            # 非常重要：
            #
            # 游戏开始时不产生咒语
            #
            # 等病毒真正爆发后，
            # 再设置这个时间。
            # =================================================

            "next_antidote_time": 0
        }

        self.games[chat_key] = game

        self.save()

        return (
            True,
            game
        )

    # ========================================================
    # 停止游戏
    # ========================================================

    def stop_game(
        self,
        chat_id: int
    ):

        game = self.get_game(chat_id)

        if not game:
            return None

        if not game.get("running", False):
            return None

        game["running"] = False

        # 解药立即失效
        game["antidote"] = {
            "active": False,
            "text": None,
            "message_id": None,
            "expire_time": 0
        }

        self.save()

        return game

    # ========================================================
    # 注册活跃玩家
    # ========================================================

    def register_active_player(
        self,
        chat_id: int,
        user
    ):

        game = self.get_game(chat_id)

        if not game:
            return

        if not game.get("running", False):
            return

        if time.time() < game.get(
            "start_time",
            0
        ):
            return

        user_id = str(user.id)

        if user_id not in game["active_players"]:

            game["active_players"][user_id] = {
                "user_id": user.id,
                "name": user.full_name
            }

        if user_id not in game["players"]:

            game["players"][user_id] = (
                self._new_player(user)
            )

        self.save()

    # ========================================================
    # 获取活跃玩家
    # ========================================================

    def get_active_players(
        self,
        chat_id: int
    ):

        game = self.get_game(chat_id)

        if not game:
            return []

        return list(
            game.get(
                "active_players",
                {}
            ).values()
        )

    # ========================================================
    # 爆发病毒
    # ========================================================

    def try_start_infection(
        self,
        chat_id: int
    ):

        game = self.get_game(chat_id)

        if not game:
            return None

        if not game.get("running", False):
            return None

        if game.get("phase") != "waiting":
            return None

        active = self.get_active_players(
            chat_id
        )

        minimum = int(
            self.config.get(
                "min_active_players",
                DEFAULT_CONFIG[
                    "min_active_players"
                ]
            )
        )

        if len(active) < minimum:
            return None

        selected = random.choice(active)

        user_id = str(
            selected["user_id"]
        )

        player = game["players"][user_id]

        # ====================================================
        # 设置初始感染者
        # ====================================================

        player["status"] = "infected"

        player["recovery_progress"] = 0

        player["recovery_required"] = int(
            self.config.get(
                "spread_required",
                DEFAULT_CONFIG[
                    "spread_required"
                ]
            )
        )

        player["infected_by"] = []

        player["infected_targets"] = []

        game["initial_infected"] = (
            selected["user_id"]
        )

        game["phase"] = "running"

        # ====================================================
        # ★ 病毒正式爆发
        #
        # 从这里开始计算第一次神秘咒语时间
        # ====================================================

        now = time.time()

        antidote_config = self.config.get(
            "antidote",
            DEFAULT_CONFIG["antidote"]
        )

        min_interval = int(
            antidote_config.get(
                "min_interval",
                DEFAULT_CONFIG[
                    "antidote"
                ]["min_interval"]
            )
        )

        max_interval = int(
            antidote_config.get(
                "max_interval",
                DEFAULT_CONFIG[
                    "antidote"
                ]["max_interval"]
            )
        )

        if max_interval < min_interval:
            max_interval = min_interval

        game["next_antidote_time"] = (
            now + random.randint(
                min_interval,
                max_interval
            )
        )

        self.save()

        return selected

    # ========================================================
    # 当前感染关系
    # source -> target
    # ========================================================

    def _get_infection_links(
        self,
        game,
        source_id: int
    ):

        links = game.setdefault(
            "infection_links",
            {}
        )

        return links.setdefault(
            str(source_id),
            []
        )

    # ========================================================
    # 判断 source 是否已经感染过 target
    # ========================================================

    def already_infected_target(
        self,
        chat_id: int,
        source_id: int,
        target_id: int
    ):

        game = self.get_game(chat_id)

        if not game:
            return False

        links = game.get(
            "infection_links",
            {}
        )

        source_links = links.get(
            str(source_id),
            []
        )

        return str(target_id) in [
            str(x)
            for x in source_links
        ]

    # ========================================================
    # 建立感染关系
    # ========================================================

    def _add_infection_link(
        self,
        game,
        source_id: int,
        target_id: int
    ):

        links = game.setdefault(
            "infection_links",
            {}
        )

        source_key = str(
            source_id
        )

        source_links = links.setdefault(
            source_key,
            []
        )

        if str(target_id) not in [
            str(x)
            for x in source_links
        ]:

            source_links.append(
                target_id
            )

    # ========================================================
    # 目标恢复健康后
    # 清除所有旧感染关系
    # ========================================================

    def _clear_target_infection_links(
        self,
        game,
        target_id: int
    ):

        target_key = str(
            target_id
        )

        links = game.setdefault(
            "infection_links",
            {}
        )

        for source_key in list(
            links.keys()
        ):

            source_links = links.get(
                source_key,
                []
            )

            links[source_key] = [
                x
                for x in source_links
                if str(x) != target_key
            ]

            if not links[source_key]:

                links.pop(
                    source_key,
                    None
                )

    # ========================================================
    # 传播病毒
    # ========================================================

    def spread(
        self,
        chat_id: int,
        source_id: int,
        target_user,
        message_id: int
    ):

        game = self.get_game(chat_id)

        if not game or not game.get(
            "running",
            False
        ):

            return {
                "success": False,
                "reason": "game_not_running"
            }

        if game.get("phase") != "running":

            return {
                "success": False,
                "reason": "not_started"
            }

        source_key = str(source_id)

        target_key = str(
            target_user.id
        )

        source = game["players"].get(
            source_key
        )

        if not source:

            return {
                "success": False,
                "reason": "source_not_player"
            }

        # ====================================================
        # 传播者必须感染
        # ====================================================

        if source.get("status") != "infected":

            return {
                "success": False,
                "reason": "source_not_infected"
            }

        # ====================================================
        # 不能感染自己
        # ====================================================

        if source_id == target_user.id:

            return {
                "success": False,
                "reason": "self"
            }

        # ====================================================
        # 获取目标
        # ====================================================

        target = game["players"].get(
            target_key
        )

        if not target:

            target = self._new_player(
                target_user
            )

            game["players"][target_key] = target

        # ====================================================
        # 死亡玩家不能感染
        # ====================================================

        if target.get("status") == "dead":

            return {
                "success": False,
                "reason": "target_dead"
            }

        # ====================================================
        # 目标仍然感染
        #
        # 检查这个感染源是否已经感染过
        # ====================================================

        if target.get("status") == "infected":

            if self.already_infected_target(
                chat_id,
                source_id,
                target_user.id
            ):

                return {
                    "success": False,
                    "reason": "already_target"
                }

        # ====================================================
        # 目标健康
        #
        # 可以重新感染
        # ====================================================

        if target.get("status") == "healthy":

            self._clear_target_infection_links(
                game,
                target_user.id
            )

        infected_by = target.setdefault(
            "infected_by",
            []
        )

        # ====================================================
        # 同一个感染源不能重复计数
        # ====================================================

        if source_id in infected_by:

            return {
                "success": False,
                "reason": "already_infected_by_source"
            }

        # ====================================================
        # 建立感染关系
        # ====================================================

        self._add_infection_link(
            game,
            source_id,
            target_user.id
        )

        source_targets = source.setdefault(
            "infected_targets",
            []
        )

        if target_user.id not in source_targets:

            source_targets.append(
                target_user.id
            )

        # ====================================================
        # 记录目标感染来源
        # ====================================================

        infected_by.append(
            source_id
        )

        target["infection_count"] += 1

        game["stats"][
            "total_infections"
        ] += 1

        game["spread_records"].append({
            "time": time.time(),
            "source": source_id,
            "target": target_user.id,
            "message_id": message_id
        })

        # ====================================================
        # 判断死亡
        # ====================================================

        death_threshold = int(
            self.config.get(
                "death_threshold",
                DEFAULT_CONFIG[
                    "death_threshold"
                ]
            )
        )

        died = False

        if len(infected_by) >= death_threshold:

            target["status"] = "dead"

            target["recovery_progress"] = 0

            target["recovery_required"] = 0

            game["stats"][
                "total_deaths"
            ] += 1

            # 所有感染源获得死亡贡献
            for source_user_id in infected_by:

                source_player = (
                    game["players"].get(
                        str(source_user_id)
                    )
                )

                if source_player:

                    source_player[
                        "death_count"
                    ] += 1

            self._clear_target_infection_links(
                game,
                target_user.id
            )

            target["infected_by"] = []

            died = True

        else:

            # =================================================
            # 目标继续感染
            # =================================================

            target["status"] = "infected"

            # 第一次感染
            if len(infected_by) == 1:

                target[
                    "recovery_progress"
                ] = 0

                target[
                    "recovery_required"
                ] = int(
                    self.config.get(
                        "spread_required",
                        DEFAULT_CONFIG[
                            "spread_required"
                        ]
                    )
                )

            # 第二个不同感染源
            elif len(infected_by) == 2:

                target[
                    "recovery_required"
                ] += 1

        # ====================================================
        # ★ 有效传播
        #
        # 目标必须是一个活着的成员。
        #
        # 第三个感染源导致死亡：
        # 这次仍然算成功感染，
        # 因此传播者仍然 +1。
        # ====================================================

        source["spread_count"] += 1

        source[
            "recovery_progress"
        ] = (
            source.get(
                "recovery_progress",
                0
            ) + 1
        )

        game["stats"][
            "total_spreads"
        ] += 1

        # ====================================================
        # 传播者是否痊愈
        # ====================================================

        recovered = False

        if not died:

            required = int(
                source.get(
                    "recovery_required",
                    self.config.get(
                        "spread_required",
                        DEFAULT_CONFIG[
                            "spread_required"
                        ]
                    )
                )
            )

            progress = int(
                source.get(
                    "recovery_progress",
                    0
                )
            )

            if (
                required > 0
                and progress >= required
            ):

                source["status"] = "healthy"

                source[
                    "recovery_progress"
                ] = 0

                source[
                    "recovery_required"
                ] = 0

                source[
                    "infected_by"
                ] = []

                recovered = True

        self.save()

        return {
            "success": True,

            "target": target,

            "died": died,

            "recovered": recovered,

            "source": source,

            "target_infection_sources": len(
                target.get(
                    "infected_by",
                    []
                )
            ),

            "target_recovery_progress": target.get(
                "recovery_progress",
                0
            ),

            "target_recovery_required": target.get(
                "recovery_required",
                0
            ),

            "recovery_progress": source.get(
                "recovery_progress",
                0
            ),

            "recovery_required": source.get(
                "recovery_required",
                0
            )
        }

    # ========================================================
    # 感染人数
    # ========================================================

    def get_infected_count(
        self,
        chat_id: int
    ) -> int:

        game = self.get_game(chat_id)

        if not game:
            return 0

        return sum(
            1
            for player in game.get(
                "players",
                {}
            ).values()
            if player.get(
                "status"
            ) == "infected"
        )

    # ========================================================
    # 是否应该结束游戏
    # ========================================================

    def should_end_game(
        self,
        chat_id: int
    ) -> bool:

        game = self.get_game(chat_id)

        if not game:
            return False

        if not game.get(
            "running",
            False
        ):
            return False

        if game.get(
            "phase"
        ) != "running":

            return False

        return (
            self.get_infected_count(
                chat_id
            ) == 0
        )

    # ========================================================
    # 生成神秘咒语
    # ========================================================

    def check_antidote_trigger(
        self,
        chat_id: int
    ):

        game = self.get_game(chat_id)

        if not game:
            return None

        # 游戏必须运行
        if not game.get(
            "running",
            False
        ):
            return None

        # ====================================================
        # 病毒还没有爆发
        #
        # waiting 阶段绝对不能产生咒语
        # ====================================================

        if game.get(
            "phase"
        ) != "running":

            return None

        config = self.config.get(
            "antidote",
            DEFAULT_CONFIG["antidote"]
        )

        if not config.get(
            "enabled",
            True
        ):
            return None

        now = time.time()

        antidote = game.setdefault(
            "antidote",
            {
                "active": False,
                "text": None,
                "message_id": None,
                "expire_time": 0
            }
        )

        # ====================================================
        # 当前咒语还有效
        # ====================================================

        if antidote.get(
            "active",
            False
        ):

            if now >= antidote.get(
                "expire_time",
                0
            ):

                antidote["active"] = False

                antidote["text"] = None

                antidote[
                    "message_id"
                ] = None

                antidote[
                    "expire_time"
                ] = 0

                self.save()

            else:

                return None

        # ====================================================
        # 还没到下一次生成时间
        # ====================================================

        if now < game.get(
            "next_antidote_time",
            0
        ):

            return None

        items = config.get(
            "items",
            []
        )

        if not items:
            return None

        # ====================================================
        # 随机选择咒语
        # ====================================================

        text = random.choice(items)

        duration = int(
            config.get(
                "duration",
                DEFAULT_CONFIG[
                    "antidote"
                ]["duration"]
            )
        )

        min_interval = int(
            config.get(
                "min_interval",
                DEFAULT_CONFIG[
                    "antidote"
                ]["min_interval"]
            )
        )

        max_interval = int(
            config.get(
                "max_interval",
                DEFAULT_CONFIG[
                    "antidote"
                ]["max_interval"]
            )
        )

        if max_interval < min_interval:
            max_interval = min_interval

        # ====================================================
        # 激活解药
        # ====================================================

        game["antidote"] = {

            "active": True,

            "text": text,

            "message_id": None,

            "expire_time": (
                now + duration
            )
        }

        game[
            "last_antidote_time"
        ] = now

        # ====================================================
        # 设置下一次神秘咒语
        # ====================================================

        game[
            "next_antidote_time"
        ] = (
            now + random.randint(
                min_interval,
                max_interval
            )
        )

        game["stats"][
            "total_antidotes"
        ] += 1

        self.save()

        return game["antidote"]

    # ========================================================
    # 兼容旧代码
    # ========================================================

    def set_antidote_message(
        self,
        chat_id: int,
        message_id: int
    ):

        game = self.get_game(
            chat_id
        )

        if not game:
            return

        antidote = game.get(
            "antidote"
        )

        if not antidote:
            return

        if not antidote.get(
            "active"
        ):
            return

        antidote[
            "message_id"
        ] = message_id

        self.save()

    # ========================================================
    # ★ 直接发送咒语解毒
    # ========================================================

    def use_antidote(
        self,
        chat_id: int,
        user_id: int,
        text: str
    ):

        game = self.get_game(
            chat_id
        )

        if not game:

            return {
                "success": False,
                "reason": "game_not_running"
            }

        if not game.get(
            "running",
            False
        ):

            return {
                "success": False,
                "reason": "game_not_running"
            }

        antidote = game.get(
            "antidote",
            {}
        )

        # ====================================================
        # 当前没有解药
        # ====================================================

        if not antidote.get(
            "active",
            False
        ):

            return {
                "success": False,
                "reason": "no_antidote"
            }

        now = time.time()

        # ====================================================
        # 过期
        # ====================================================

        if now >= antidote.get(
            "expire_time",
            0
        ):

            antidote[
                "active"
            ] = False

            antidote[
                "text"
            ] = None

            antidote[
                "message_id"
            ] = None

            antidote[
                "expire_time"
            ] = 0

            self.save()

            return {
                "success": False,
                "reason": "expired"
            }

        # ====================================================
        # 检查咒语
        # ====================================================

        user_text = (
            text or ""
        ).strip()

        correct_text = (
            antidote.get(
                "text"
            ) or ""
        ).strip()

        if not correct_text:

            return {
                "success": False,
                "reason": "no_antidote"
            }

        if user_text != correct_text:

            return {
                "success": False,
                "reason": "wrong_spell"
            }

        # ====================================================
        # 获取玩家
        # ====================================================

        player = game.get(
            "players",
            {}
        ).get(
            str(user_id)
        )

        if not player:

            return {
                "success": False,
                "reason": "not_player"
            }

        # ====================================================
        # 只有感染者可以解毒
        # ====================================================

        if player.get(
            "status"
        ) != "infected":

            return {
                "success": False,
                "reason": "not_infected"
            }

        # ====================================================
        # 解毒
        # ====================================================

        player[
            "status"
        ] = "healthy"

        player[
            "recovery_progress"
        ] = 0

        player[
            "recovery_required"
        ] = 0

        player[
            "infected_by"
        ] = []

        # ====================================================
        # 玩家恢复健康
        #
        # 解除所有 source -> target 关系
        # ====================================================

        self._clear_target_infection_links(
            game,
            user_id
        )

        # ====================================================
        # 解药只能使用一次
        # ====================================================

        antidote[
            "active"
        ] = False

        antidote[
            "text"
        ] = None

        antidote[
            "message_id"
        ] = None

        antidote[
            "expire_time"
        ] = 0

        game["stats"][
            "total_antidote_uses"
        ] += 1

        player[
            "antidote_discover_count"
        ] += 1

        self.save()

        return {
            "success": True
        }

    # ========================================================
    # 状态
    # ========================================================

    def status_text(
        self,
        chat_id: int
    ):

        game = self.get_game(
            chat_id
        )

        if not game:

            return (
                "当前没有病毒游戏。"
            )

        if not game.get(
            "running",
            False
        ):

            return (
                "当前病毒游戏已经结束。"
            )

        active_count = len(
            game.get(
                "active_players",
                {}
            )
        )

        infected = 0
        dead = 0

        for player in game.get(
            "players",
            {}
        ).values():

            if player.get(
                "status"
            ) == "infected":

                infected += 1

            elif player.get(
                "status"
            ) == "dead":

                dead += 1

        phase = game.get(
            "phase"
        )

        if phase == "waiting":

            return (
                "🦠 病毒正在潜伏……\n\n"
                f"👥 当前活跃玩家："
                f"{active_count}\n"
                f"🎯 最少需要："
                f"{self.config.get('min_active_players', 5)}人\n\n"
                "请继续聊天，"
                "病毒会在活跃玩家足够后随机爆发。"
            )

        antidote = game.get(
            "antidote",
            {}
        )

        if antidote.get(
            "active"
        ):

            antidote_text = (
                "🧪 神秘咒语已出现"
            )

        else:

            antidote_text = (
                "⏳ 等待下一次神秘咒语"
            )

        return (
            "🦠 病毒扩散进行中\n\n"
            f"👥 活跃玩家："
            f"{active_count}\n"
            f"🦠 当前感染者："
            f"{infected}\n"
            f"💀 阵亡人数："
            f"{dead}\n"
            f"🔄 总传播次数："
            f"{game['stats']['total_spreads']}\n"
            f"🧬 总感染次数："
            f"{game['stats']['total_infections']}\n"
            f"💊 解药出现："
            f"{game['stats']['total_antidotes']}次\n"
            f"💊 解药使用："
            f"{game['stats']['total_antidote_uses']}次\n\n"
            f"{antidote_text}"
        )

    # ========================================================
    # 结算
    # ========================================================

    def build_result(
        self,
        chat_id: int
    ):

        game = self.get_game(
            chat_id
        )

        if not game:
            return None

        players = list(
            game.get(
                "players",
                {}
            ).values()
        )

        def score(player):

            return (
                player.get(
                    "spread_count",
                    0
                )
                + player.get(
                    "infection_count",
                    0
                ) * 2
                + player.get(
                    "death_count",
                    0
                ) * 10
            )

        players.sort(
            key=score,
            reverse=True
        )

        return {
            "stats": game.get(
                "stats",
                {}
            ),
            "players": players[:10]
        }

    # ========================================================
    # 删除游戏
    # ========================================================

    def remove_game(
        self,
        chat_id: int
    ):

        self.games.pop(
            str(chat_id),
            None
        )

        task = self._tasks.pop(
            str(chat_id),
            None
        )

        if task:

            try:
                task.cancel()
            except Exception:
                pass

        self.save()


# ============================================================
# 全局管理器
# ============================================================

virus_manager = VirusGameManager()