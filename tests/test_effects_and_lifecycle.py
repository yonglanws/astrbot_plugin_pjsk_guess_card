import importlib
import sys
import types
from pathlib import Path


PLUGIN_DIR = Path(__file__).parents[1]


def _install_astrbot_stubs():
    logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    api.AstrBotConfig = dict
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(command=lambda *args, **kwargs: lambda func: func)
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path("."))
    star.register = lambda *args, **kwargs: lambda cls: cls
    components = types.ModuleType("astrbot.api.message_components")
    waiter = types.ModuleType("astrbot.core.utils.session_waiter")
    waiter.SessionController = object
    waiter.SessionFilter = object
    waiter.session_waiter = lambda *args, **kwargs: lambda func: func
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.message_components": components,
            "astrbot.core.utils.session_waiter": waiter,
        }
    )


def _load_main_module():
    _install_astrbot_stubs()
    sys.path.insert(0, str(PLUGIN_DIR))
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def test_all_disabled_effects_produce_a_safe_no_effect_round():
    module = _load_main_module()
    config = {"effects": {name: {"enabled": False} for name in module.ImageEffectProcessor.EFFECT_NAMES}}

    processor = module.ImageEffectProcessor(config)

    assert processor.random_effect() == "none"
    assert processor.random_effect_combination() == (["none"], "无效果")
