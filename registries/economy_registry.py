from lottery.betting import register_buy_lottery_handlers
from company.business import register_business_handlers
from company.company_ipo import register_company_ipo_handlers
from company.company_recruit import register_recruit_handlers
from info.economy import register_economy_handlers
from info.economy_bank import register_economy_bank_handlers
from market.price import register_price_handlers
from feature_flags import is_feature_enabled


def register_economy_handlers_group(app):
    if not is_feature_enabled(app, "economy"):
        return

    register_buy_lottery_handlers(app)
    register_economy_handlers(app)
    register_economy_bank_handlers(app)
    register_price_handlers(app)
    register_business_handlers(app)
    register_company_ipo_handlers(app)
    register_recruit_handlers(app)
