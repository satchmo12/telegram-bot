from farm.crop_config import CROP_CONFIG
from farm.farm_game import get_growth_stage


def water_crop(land, crop):
    if not land.get("watered") and get_growth_stage(land, crop) == "苗期":
        land["watered"] = True
        land["planted_time"] -= CROP_CONFIG[crop]["grow_time"] * 0.1
        return f"💧浇水 -> {crop}"
    return None

def fertilize_crop(land, crop):
    if not land.get("fertilized") and get_growth_stage(land, crop) == "花期":
        land["fertilized"] = True
        land["planted_time"] -= CROP_CONFIG[crop]["grow_time"] * 0.1
        return f"🌿施肥 -> {crop}"
    return None

def spray_crop(land, crop):
    if not land.get("sprayed") and get_growth_stage(land, crop) == "果期":
        land["sprayed"] = True
        return f"🐛杀虫 -> {crop}（增产）"
    return None

def harvest_crop(land, crop, now_ts, user_auto_crop=None):
    num = land.get("yield_left", 10)
    if not land.get("sprayed"):
        num -= 2

    planted_time = land.get("planted_time", 0)
    grow_time = CROP_CONFIG[crop]["grow_time"]

    if now_ts - planted_time >= grow_time:
        harvested = {crop: num}

        # 收获后自动种植
        if user_auto_crop:
            land.update({
                "crop": user_auto_crop,
                "planted_time": now_ts,
                "watered": False,
                "fertilized": False,
                "sprayed": False,
                "yield_left": CROP_CONFIG.get(user_auto_crop, {}).get("max_yield", 10)
            })
            return harvested, f"🌱 自动种植 -> {user_auto_crop}"
        else:
            return harvested, None

    return None, None
