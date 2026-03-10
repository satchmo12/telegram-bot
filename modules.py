from game_niuniu import register_niuniu_handlers
from registries.economy_registry import register_economy_handlers_group
from registries.entertainment_registry import register_entertainment_handlers
from registries.group_registry import register_group_handlers
from registries.simulation_registry import register_simulation_handlers
from feature_flags import is_feature_enabled


def register_all_handlers(app):
    
    register_economy_handlers_group(app)
    register_entertainment_handlers(app)
    register_simulation_handlers(app)
    register_group_handlers(app)
    
    if is_feature_enabled(app, "niuniu"):
        register_niuniu_handlers(app)
