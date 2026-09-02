import ast
import operator as op
import math

import re

from telegram import Update
from telegram.ext import ContextTypes

# 允许的运算
_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
    ast.FloorDiv: op.floordiv,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}


def _calculate_node(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("不支持的数字类型")

    if isinstance(node, ast.Num):
        return node.n

    if isinstance(node, ast.BinOp):
        operator = _OPERATORS.get(type(node.op))
        if operator is None:
            raise ValueError("不支持的运算符")

        left = _calculate_node(node.left)
        right = _calculate_node(node.right)

        # 防止指数计算过大
        if isinstance(node.op, ast.Pow):
            if abs(right) > 100:
                raise ValueError("指数不能超过 100")

        return operator(left, right)

    if isinstance(node, ast.UnaryOp):
        operator = _OPERATORS.get(type(node.op))
        if operator is None:
            raise ValueError("不支持的运算符")

        value = _calculate_node(node.operand)
        return operator(value)

    raise ValueError("表达式格式不正确")


def calculate(expression: str):
    expression = expression.strip()

    if not expression:
        raise ValueError("请输入计算式")

    # 限制长度
    if len(expression) > 100:
        raise ValueError("计算式太长")

    tree = ast.parse(expression, mode="eval")

    result = _calculate_node(tree.body)

    if isinstance(result, float):
        if not math.isfinite(result):
            raise ValueError("计算结果无效")

        # 整数结果不要显示 .0
        if result.is_integer():
            return int(result)

        return round(result, 10)

    return result

async def calculator_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    msg = update.effective_message

    if not msg or not msg.text:
        return

    text = msg.text.strip()

    # 太长的普通聊天直接忽略
    if len(text) > 100:
        return

    # 只允许包含数学表达式相关字符
    # 数字、运算符、小数点、括号、空格
    if not re.fullmatch(
        r"[0-9+\-*/%^().\s]+",
        text
    ):
        return

    # 至少要有数字
    if not re.search(r"\d", text):
        return

    try:
        # 支持用户习惯输入 2^10
        expression = text.replace("^", "**")

        result = calculate(expression)

    except Exception:
        # 不是合法计算式，直接忽略
        return

    await msg.reply_text(
        f"🧮 {text} = {result}"
    )