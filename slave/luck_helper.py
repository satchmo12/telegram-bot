import random

def calculate_success(luck: int, base_rate: float, max_luck_bonus: float = 0.3) -> tuple[bool, float]:
    """
    计算是否成功，返回(success: bool, final_success_rate: float)
    
    :param luck: 用户幸运值（0~100）
    :param base_rate: 行为基础成功率（0~1）
    :param max_luck_bonus: 幸运值最大能提供多少额外成功率（默认30%）
    :return: 是否成功, 
    # 最终成功率百分比
    """
    luck = max(0, min(100, luck))  # 限制在 0~100
    bonus = (luck / 100) * max_luck_bonus
    final_rate = min(1.0, base_rate + bonus)
    success = random.random() < final_rate
    # , round(final_rate * 100, 2)
    return success
