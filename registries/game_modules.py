from farm.animals_game import register_animals_game_handlers
from items.bag_game import register_bag_game_handlers
from farm.crafting_game import register_crafting_game_handlers
from farm.farm_game import register_farm_game_handlers
from farm.farm_manager import register_farm_manager_handlers
from farm.farm_orders import register_farm_order_handlers
from farm.garden_game import register_garden_game_handlers
from farm.shop_game import register_shop_game_handlers
from slave.marry_system import register_marry_handlers
from slave.pet_game import register_pet_handlers
from slave.slave_game import register_slave_handlers


def register_roleplay_handlers(app):
    register_slave_handlers(app)
    register_pet_handlers(app)
    register_marry_handlers(app)


def register_farm_ecosystem_handlers(app):
    register_farm_game_handlers(app)
    register_animals_game_handlers(app)
    register_crafting_game_handlers(app)
    register_shop_game_handlers(app)
    register_farm_manager_handlers(app)
    register_farm_order_handlers(app)
    register_garden_game_handlers(app)

    # 庄园产出与背包联动
    register_bag_game_handlers(app)


def register_game_handlers(app):
    register_roleplay_handlers(app)
    register_farm_ecosystem_handlers(app)
