ANIMAL_CONFIG = {
    "牛": {
        "name": "牛",
        "cost": 500,
        "produce": "milk",
        "product_name": "牛奶",
        "interval": 6 * 3600,
        "grow_time": 12 * 3600,  # 12小时才能出栏
        "butcher_product": "牛肉",  # ←新增
        "max_yield": 10,  # 产奶一次最多 3 份
        "sell_price": 100,
    },
    "鸡": {
        "name": "鸡",
        "cost": 200,
        "produce": "egg",
        "product_name": "鸡蛋",
        "interval": 3 * 3600,
        "grow_time": 3 * 3600,  # 3小时才能出栏
        "butcher_product": "鸡肉",  # ←新增
        "max_yield": 10,  # 产奶一次最多 3 份
        "sell_price": 40,
    },
    "羊": {
        "name": "羊",
        "cost": 300,
        "produce": "wool",
        "product_name": "羊毛",
        "interval": 12 * 3600,
        "grow_time": 6 * 3600,  # 12小时才能出栏
        "butcher_product": "羊肉",  # ←新增
        "sell_price": 120,
        "max_yield": 10,  # 产奶一次最多 3 份
    },
    "猪": {
        "name": "猪",
        "cost": 400,
        "produce": "pork",
        "product_name": "猪肉",
        "interval": None,
        "grow_time": 6 * 3600,  # 6小时才能屠宰
        "butcher_product": "猪肉",
        "sell_price": 80,
        "max_yield": 10,  # 产奶一次最多 3 份
    },
}


ANIMAL_PRODUCT_CONFIG = {
    "牛奶": {},
    "牛肉": {},
    "鸡肉": {},
    "鸡蛋": {},
    "羊毛": {},
    "羊肉": {},
    "猪肉": {},
}
