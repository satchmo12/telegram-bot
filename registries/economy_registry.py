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

    if is_feature_enabled(app, "lottery_betting"):
        register_buy_lottery_handlers(app)
    if is_feature_enabled(app, "economy_info"):
        register_economy_handlers(app)
    if is_feature_enabled(app, "economy_bank"):
        register_economy_bank_handlers(app)
    if is_feature_enabled(app, "market_price"):
        register_price_handlers(app)

    # 公司与股票系统
    if is_feature_enabled(app, "company_business"):
        register_business_handlers(app)
    if is_feature_enabled(app, "company_ipo"):
        register_company_ipo_handlers(app)
    if is_feature_enabled(app, "company_recruit"):
        register_recruit_handlers(app)
