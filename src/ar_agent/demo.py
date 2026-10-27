"""演示剧本 —— 一条命令跑完整条故事线。

把完整流程固化成一个脚本，每一步先说明要验证什么，再运行并展示结果。

13 幕，大约 5-8 分钟，覆盖：分级 → 自动跟进 → 48h 缓冲 → 人工接管
→ 关键词强制升级 → 高风险零外发 → 熔断 → 周报 → 风险边界自检。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, BLUE, GREY = ("\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[90m")

ROOT: Path = Path(".")
PAUSE = True
STEP = 0


def _run(*args: str) -> None:
    """跑一条 CLI 命令，输出直通终端（保留颜色）。

    先 flush：子进程直接写终端 fd，而本进程的 print 在管道下是块缓冲的。
    不 flush 的话，旁白会全部堆到最后才出现，演示节奏就散了。
    """
    sys.stdout.flush()
    subprocess.run([sys.executable, str(ROOT / "ar.py"), *args], check=False)
    sys.stdout.flush()


def scene(title: str, why: str) -> None:
    global STEP
    STEP += 1
    print(f"\n{GREY}{'═' * 88}{RESET}")
    print(f"{BOLD}第 {STEP} 幕　{title}{RESET}")
    print(f"{DIM}{why}{RESET}")
    print(f"{GREY}{'─' * 88}{RESET}")
    sys.stdout.flush()
    if PAUSE:
        try:
            input(f"{GREY}　按回车继续…{RESET}")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)


def note(text: str) -> None:
    print(f"\n{BLUE}▸{RESET} {text}")


def show_outbox_one(match_invoice: str, label: str) -> None:
    """从 outbox 里挑出一条消息，原样打印出来。"""
    files = sorted((ROOT / "data" / "outbox").glob("*.json"))
    for f in reversed(files):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("invoice_id") == match_invoice:
            out = d["audience"] == "customer"
            color = RED if out else BLUE
            print(f"\n{color}【{label}】{RESET} {d['channel']} → {BOLD}{d['to']}{RESET}"
                  f"　{DIM}模板 {d['template_id']}{RESET}")
            if d.get("subject"):
                print(f"{BOLD}主题：{d['subject']}{RESET}")
            print(f"{GREY}{'┈' * 84}{RESET}")
            print(d["body"])
            print(f"{GREY}{'┈' * 84}{RESET}")
            sys.stdout.flush()
            return
    print(f"{DIM}（未找到 {match_invoice} 的消息）{RESET}")


def run_demo(root: Path, pause: bool = True) -> None:
    global ROOT, PAUSE, STEP
    ROOT, PAUSE, STEP = Path(root), pause, 0

    anchor = "2026-08-25"

    print(f"\n{BOLD}应收账款催收 Agent · 演示{RESET}")
    print(f"{DIM}虚拟场景：B2B 贸易企业，300+ 企业客户，账期 30-60 天，财务 2 人 + 销售 20 人。")
    print(f"演示锚点日 {anchor}，全程 dry-run —— 不会向任何真实客户发送消息。{RESET}")

    # ---------------------------------------------------------------- #
    scene("初始化：30 笔模拟账款",
          "这 30 笔不是随机造的，是按决策矩阵的每一格 + 每一条策略规则刻意设计的，"
          "所以待会儿任何一条你都可以指着问「这条为什么这么判」。")
    _run("seed", "--today", anchor)

    # ---------------------------------------------------------------- #
    scene("分级引擎：30 笔账款，三档风险",
          "先看判定结果，不产生任何动作。注意最后一段「被 L4 策略层强制升级」的账款 —— "
          "那是分级引擎本来判得更轻、被运行时策略层改严了的。")
    _run("matrix", "--date", anchor)

    # ---------------------------------------------------------------- #
    scene("第一次每日扫描",
          "轻度自动发 WhatsApp；中度先 Slack 通知客户经理并进入 48 小时缓冲期；"
          "高风险只通知内部，不对客户发任何东西。")
    _run("scan", "--date", f"{anchor} 09:00")

    # ---------------------------------------------------------------- #
    scene("Agent 到底对客户说了什么",
          "话术是预设模板渲染的，Agent 没有自由文本入口。注意语气：专业、克制、"
          "信息完整（金额/到期日/付款链接），并且明确给出「已付请忽略」的出口。")
    show_outbox_one("INV-2001", "对客户 · 轻度首次提醒")
    note("Agent 不可能写出「我们同意贵司分期」这类话 —— "
         f"渲染函数 {BOLD}render(template_id, variables){RESET} 根本没有 body 参数。")

    # ---------------------------------------------------------------- #
    scene("中度：先给人优先权，而不是抢在人前面",
          "这是整套设计里最能体现「人机协作而非机器接管」的一条：Agent 先通知客户经理，"
          "明确告诉他有 48 小时优先处理权，以及不响应会发生什么。")
    show_outbox_one("INV-2010", "对内 · Slack 通知客户经理")

    # ---------------------------------------------------------------- #
    scene("高风险：误判的代价是「财务多看一眼」，不是「客户被误伤」",
          "高风险分级只触发内部通知，不触发任何对外动作。这是刻意做的非对称风险控制："
          "宁可误报增加人工审查，不可漏报导致自动化误伤客户关系。")
    show_outbox_one("INV-2019", "对内 · 高风险告警（26 万 / 逾期 40 天）")
    note(f"注意加粗那句「{BOLD}系统未向该客户发送任何消息{RESET}」—— "
         "让财务一眼就能确认客户没被打扰过。")

    # ---------------------------------------------------------------- #
    scene("客户经理接管：Agent 立即停手",
          "接管做成显式命令，而不是「在 Slack 回一句我来处理」。"
          "聊天记录是上下文，不是授权 —— 把它当授权用，等于给任何能在那个频道说话的人开了个口子。")
    _run("takeover", "INV-2014", "--by", "李娜", "--note", "客户答应本周五付款，我自己跟进")

    # ---------------------------------------------------------------- #
    scene("48 小时后：客户经理没响应的，Agent 补位",
          "补位发的是「正式但克制」的对账函，不是加压话术。"
          "同时把「客户经理未响应」这个事实记进周报 —— 这本身是个管理信号。")
    _run("scan", "--date", "2026-08-27 10:00")
    note("被接管的 INV-2014 在列表里是「暂不动作」—— Agent 认接管，不抢人的活。")

    # ---------------------------------------------------------------- #
    scene("关键规则：客户一提「分期」，无条件转人工",
          "现在模拟一个轻度账款的客户回复。这条规则是纯关键词匹配，不经过 LLM —— "
          "它不判断诉求是否合理，命中即升级。")
    _run("reply", "INV-2005", "--text",
         "你好，这个月资金有点紧，我们能不能分三期把这笔款付清？",
         "--at", "2026-08-27 15:00")
    note("下一次扫描时，这条账款会从 🟢 轻度 直接跳到 🔴 高风险，并停止一切自动化。")

    # ---------------------------------------------------------------- #
    scene("验证：轻度 → 高风险，且不再对客户发任何东西",
          "分级引擎按天数金额本来判它轻度，运行时策略层覆盖了这个判断。"
          "这就是「复杂任务逻辑不能被绕过」的落地方式：判定权在规则层，不在模型。")
    _run("scan", "--date", "2026-08-28 09:00")
    note("再往后推几天：INV-2026 到这时才刚过付款日 —— 它是系统上线后"
         "新逾期的第一笔，用它才能量出真实的首次触达时效。")
    _run("scan", "--date", "2026-08-31 09:00")

    # ---------------------------------------------------------------- #
    scene("给 CFO 的周报",
          "CFO 的诉求是「不用追着财务要数据」。注意第三段护栏指标 —— "
          "把反向指标放进 CFO 每周都看的报告里，它才真正约束决策，"
          "藏在监控后台就没人当回事。")
    _run("report", "--date", "2026-08-31", "--send")

    # ---------------------------------------------------------------- #
    scene("风险边界自检：不是承诺，是当场验证",
          "文档里写「我们有 7 层防线」谁都会写。下面每一条都是"
          "故意做一件不该被允许的事，然后断言它被拦住了。")
    _run("verify")

    # ---------------------------------------------------------------- #
    scene("可视化面板",
          "财务和 CFO 真正会问的四个问题：现在有多少钱在外面、"
          "每条为什么这么判、Agent 对客户说了什么、有没有为了效率牺牲客户关系。")
    print(f"\n  面板地址：{BOLD}http://127.0.0.1:8787/{RESET}")
    print(f"  {DIM}启动命令：python ar.py dashboard{RESET}")
    print(f"\n{GREY}{'═' * 88}{RESET}")
    print(f"{GREEN}{BOLD}演示结束{RESET}　{DIM}全程 dry-run，"
          f"所有消息都在 data/outbox/，没有发给任何真实客户。{RESET}")
    print(f"{DIM}生成时间 {datetime.now():%Y-%m-%d %H:%M}{RESET}\n")
