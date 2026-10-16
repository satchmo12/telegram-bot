
import platform
import random
import time
from pathlib import Path

import pyautogui
import pyperclip

MESSAGE_FILE = Path(__file__).with_name("messages.txt")


def load_messages():
    if not MESSAGE_FILE.exists():
        raise FileNotFoundError(
            f"找不到消息文件：{MESSAGE_FILE}"
        )

    messages = [
        line.strip()
        for line in MESSAGE_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]

    if not messages:
        raise ValueError("messages.txt 中没有可发送的内容")

    return messages


def paste_from_clipboard():
    """Paste with an explicit modifier hold so macOS receives Command+V."""
    modifier = "command" if platform.system() == "Darwin" else "ctrl"
    try:
        # On macOS, hotkey() can occasionally deliver the V before the Command
        # modifier has been registered by the target app. Holding it explicitly
        # prevents Telegram from receiving a literal "v" instead of a paste.
        pyautogui.keyDown(modifier)
        time.sleep(0.15)
        pyautogui.press("v")
        time.sleep(0.15)
    finally:
        pyautogui.keyUp(modifier)



def main():
    messages = load_messages()

    print("循环发送脚本已启动。")
    print("按 Ctrl+C 可以停止脚本。")

    try:
        while True:
            message = random.choice(messages)
            print(f"\n本次选择的消息：{message}")

            # 每轮随机等待 20～40 秒
            wait_seconds = random.uniform(10, 20)
            print(f"将在 {wait_seconds:.1f} 秒后继续...")
            time.sleep(wait_seconds)

            # 复制消息并检查剪贴板
            pyperclip.copy(message)
            time.sleep(0.2)

            if pyperclip.paste() != message:
                print("剪贴板写入失败，本轮跳过。")
                continue

            # 模拟粘贴
            paste_from_clipboard()
            time.sleep(1)

            print("已尝试粘贴，请确认消息出现在正确的 Telegram 输入框中。")

            # 暂时不自动按 Enter，避免误发送
            input("确认内容和目标群正确后，按 Enter 继续；输入 q 退出：")
            # 这里只是交互确认，不会自动发送

    except KeyboardInterrupt:
        print("\n脚本已停止。")


if __name__ == "__main__":
    main()
