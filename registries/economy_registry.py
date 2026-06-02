
from info.economy import register_economy_handlers
from market.price import register_price_handlers
from feature_flags import is_feature_enabled


def register_economy_handlers_group(app):
    if not is_feature_enabled(app, "economy"):
        return
    register_economy_handlers(app)
    register_price_handlers(app)
 