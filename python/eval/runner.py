"""
评测运行器 —— 回答"这次改动是变好了还是变差了"。

    python -m eval.runner                        # 跑一遍，出一份报告
    python -m eval.runner --baseline <旧报告>     # 与旧报告对比

    为什么需要它
    ────────────
    没有评测，"优化"就只是感觉。本项目的每一次改动（关闭推理、修候选集 bug、
    加工具层）都能验证"没坏"，但没法回答"好了多少"。

    这个运行器提供两样东西：
      ① 可重复的通过率 —— 同一份用例、同一套断言，每次给出同一个数
      ② 前后对比 —— --baseline 打印差值，这就是简历上那个数字的来源

    输出的每一份报告都带【身份信息】
    ────────────────────────────────
    git SHA / 模型名 / 价格表指纹 / Python 版本。
    因为模型换了、价格表改了，所有数字都会变 ——
    不带身份信息的对比是假的。

    ⚠️ 结果必须写到 python/ 【外面】
    ────────────────────────────
    uvicorn --reload 会盯着工作目录树里的 *.py，而且结果文件带时间戳、
    每次不同。让它们进版本库或触发重启都没有意义。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

PYTHON_DIR = Path(__file__).resolve().parent.parent
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

DEFAULT_API = "http://127.0.0.1:8000"
DEFAULT_OUT = PYTHON_DIR.parent / "eval_results"


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PYTHON_DIR, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


# ── 断言 ────────────────────────────────────────────────────
#
# 全部是【确定性】断言 —— 不看模型说了什么好不好，只看契约有没有被满足。
# 理由：文案 Agent 跑在 temperature=0.9 上，是系统里输出最噪的一环，
# 用主观标准做门禁会产生大量假报警，让评测失去意义。
# 主观质量另有一条【非门禁】的 judge（见 judge.py）。

def check_case(case: dict[str, Any], body: dict[str, Any]) -> list[str]:
    """返回失败原因列表。空列表 = 通过。"""
    a = case.get("assert", {})
    req = case.get("request", {})
    failures: list[str] = []

    products = body.get("products", [])
    copies = body.get("marketing_copies", [])
    agents = body.get("agent_results", {})
    harness = body.get("harness") or {}
    usage = harness.get("usage") or {}

    num_items = req.get("num_items", 10)

    # 1. 商品数量契约
    min_products = a.get("min_products")
    if min_products is not None and len(products) < min_products:
        failures.append(f"商品数不足: 期望 >= {min_products}，实际 {len(products)}")
    if len(products) > num_items:
        failures.append(f"商品数超过请求量: num_items={num_items}，实际 {len(products)}")

    # 2. 商品 ID 必须是目录里真实存在的
    if a.get("known_product_ids"):
        from agents.product_rec_agent import MOCK_PRODUCTS

        known = {p.product_id for p in MOCK_PRODUCTS}
        unknown = {p["product_id"] for p in products} - known
        if unknown:
            failures.append(f"返回了不存在的商品 ID: {sorted(unknown)}")

    # 3. 文案必须逐商品对应
    if a.get("copies_cover_products"):
        want = {p["product_id"] for p in products}
        got = {c.get("product_id") for c in copies}
        if want != got:
            missing = want - got
            extra = got - want
            if missing:
                failures.append(f"这些商品没有文案: {sorted(missing)}")
            if extra:
                failures.append(f"文案对应了不存在的商品: {sorted(extra)}")

    # 4. 违禁词 —— 直接复用业务代码里的清单，不重新声明一份
    #    （重复声明一份 = 将来两处规则会不一致，而合规是硬要求）
    if a.get("no_forbidden_words"):
        from agents.marketing_copy_agent import FORBIDDEN_WORDS

        hits = [
            (c.get("product_id"), w)
            for c in copies
            for w in FORBIDDEN_WORDS
            if w in (c.get("copy") or "")
        ]
        if hits:
            failures.append(f"文案含违禁词: {hits}")

    # 5. 文案长度（空文案/超长文案都是故障信号）
    for c in copies:
        text = c.get("copy") or ""
        if not text.strip():
            failures.append(f"{c.get('product_id')} 的文案为空")
        elif len(text) > 200:
            failures.append(f"{c.get('product_id')} 的文案过长({len(text)}字)")

    # 6. 指定 Agent 必须成功
    for name in a.get("require_agents_success", []):
        r = agents.get(name)
        if not r:
            failures.append(f"响应里缺少 {name} 的结果")
        elif not r.get("success"):
            failures.append(f"{name} 未成功: {r.get('error')}")

    # 7. 延迟
    max_latency = a.get("max_latency_ms")
    if max_latency and body.get("total_latency_ms", 0) > max_latency:
        failures.append(
            f"延迟超限: {body['total_latency_ms']:.0f}ms > {max_latency}ms"
        )

    # 8. 成本（价格未知时跳过 —— 不能拿一个 None 当 0 用）
    max_cost = a.get("max_cost_usd")
    if max_cost is not None and usage.get("cost_known"):
        cost = usage.get("cost_usd") or 0.0
        if cost > max_cost:
            failures.append(f"成本超限: ${cost:.6f} > ${max_cost}")

    return failures


# ── 执行 ────────────────────────────────────────────────────

def run_case(case: dict[str, Any], api: str, timeout_s: float) -> dict[str, Any]:
    body = json.dumps(case["request"], ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/api/v1/recommend", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read())
        wall_ms = (time.perf_counter() - t0) * 1000
        failures = check_case(case, payload)
        return {
            "id": case["id"],
            "note": case.get("note", ""),
            "passed": not failures,
            "failures": failures,
            "http_status": 200,
            "wall_ms": round(wall_ms, 1),
            "total_latency_ms": payload.get("total_latency_ms"),
            "product_count": len(payload.get("products", [])),
            "copy_count": len(payload.get("marketing_copies", [])),
            # 留一份文案样本，供【非门禁】的 LLM 裁判使用（见 judge.py）。
            # 只留前 2 条，避免报告体积膨胀。
            "copies_sample": payload.get("marketing_copies", [])[:2],
            "usage": (payload.get("harness") or {}).get("usage"),
        }
    except urllib.error.HTTPError as exc:
        return {
            "id": case["id"], "note": case.get("note", ""), "passed": False,
            "failures": [f"HTTP {exc.code}: {exc.read()[:200]!r}"],
            "http_status": exc.code,
            "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as exc:
        return {
            "id": case["id"], "note": case.get("note", ""), "passed": False,
            "failures": [f"{type(exc).__name__}: {exc}"],
            "http_status": None,
            "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


def summarise(runs: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in runs if r.get("passed")]
    lat = [r["total_latency_ms"] for r in runs if r.get("total_latency_ms")]
    costs = [
        r["usage"]["cost_usd"] for r in runs
        if (r.get("usage") or {}).get("cost_known") and r["usage"].get("cost_usd") is not None
    ]
    tokens = [
        r["usage"]["output_tokens"] for r in runs if (r.get("usage") or {}).get("output_tokens")
    ]

    def pct(vals: list[float], p: float) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
        return round(s[k], 1)

    return {
        "cases": len(runs),
        "passed": len(ok),
        "pass_rate": round(len(ok) / len(runs), 3) if runs else 0.0,
        "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
        "latency_p95_ms": pct(lat, 95),
        "cost_mean_usd": round(statistics.mean(costs), 6) if costs else None,
        "cost_known_cases": len(costs),
        "output_tokens_p50": round(statistics.median(tokens), 1) if tokens else None,
    }


def build_report(runs: list[dict[str, Any]], api: str) -> dict[str, Any]:
    """报告头里带上【身份信息】—— 否则前后对比是假的。"""
    from config import get_settings
    from harness.deps import get_pricing

    settings = get_settings()
    return {
        "meta": {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "git_sha": git_sha(),
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "pricing_fingerprint": get_pricing().fingerprint(),
            "thinking_disabled": settings.llm_disable_thinking,
            "thinking_exempt": settings.llm_thinking_exempt_agents,
            "mcp_wms_enabled": settings.mcp_wms_enabled,
            "python": sys.version.split()[0],
            "api": api,
        },
        "summary": summarise(runs),
        "runs": runs,
    }


# ── 对比 ────────────────────────────────────────────────────

def print_delta(current: dict[str, Any], baseline_path: Path) -> None:
    """
    打印与旧报告的差值。**这一行就是简历上那个数字的来源。**

    先检查身份信息是否可比 —— 模型或价格表变了的对比没有意义。
    """
    try:
        old = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"\n[WARN] 读不了基线报告 {baseline_path}: {exc}")
        return

    om, nm = old.get("meta", {}), current["meta"]
    print("\n" + "=" * 66)
    print(f"与基线对比：{baseline_path.name}")
    print("=" * 66)

    warnings = []
    if om.get("model") != nm.get("model"):
        warnings.append(f"模型不同: {om.get('model')} -> {nm.get('model')}")
    if om.get("pricing_fingerprint") != nm.get("pricing_fingerprint"):
        warnings.append("价格表不同 —— 成本对比不可比")
    if om.get("thinking_exempt") != nm.get("thinking_exempt"):
        warnings.append(
            f"推理豁免名单不同: {om.get('thinking_exempt')!r} -> {nm.get('thinking_exempt')!r}"
        )
    if warnings:
        print("⚠️  可比性告警：")
        for w in warnings:
            print(f"    - {w}")
        print()

    os_, ns = old.get("summary", {}), current["summary"]
    rows = [
        ("通过率", "pass_rate", lambda v: f"{v:.0%}" if isinstance(v, float) else v, "up"),
        ("延迟 p50 (ms)", "latency_p50_ms", lambda v: f"{v:.0f}" if v else "n/a", "down"),
        ("延迟 p95 (ms)", "latency_p95_ms", lambda v: f"{v:.0f}" if v else "n/a", "down"),
        ("平均成本 ($)", "cost_mean_usd", lambda v: f"{v:.6f}" if v else "n/a", "down"),
        ("输出 token p50", "output_tokens_p50", lambda v: f"{v:.0f}" if v else "n/a", "down"),
    ]
    for label, key, fmt, direction in rows:
        o, n = os_.get(key), ns.get(key)
        if o is None or n is None:
            print(f"  {label:<18} {fmt(o) if o is not None else 'n/a':>10} -> {fmt(n) if n is not None else 'n/a':>10}")
            continue
        arrow = ""
        if o != n:
            better = (n > o) if direction == "up" else (n < o)
            arrow = "  ↑改善" if better else "  ↓变差"
        print(f"  {label:<18} {fmt(o):>10} -> {fmt(n):>10}{arrow}")


def main() -> int:
    parser = argparse.ArgumentParser(description="跑评测集并出报告")
    parser.add_argument("--cases", default=str(Path(__file__).parent / "cases.jsonl"))
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--baseline", default=None, help="旧报告路径，用于打印差值")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试用）")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    if args.limit:
        cases = cases[: args.limit]

    # 先探活 —— 服务没起来的话跑一堆连接失败毫无意义
    try:
        urllib.request.urlopen(f"{args.api}/health", timeout=5)
    except Exception as exc:
        print(f"[FAIL] 服务不可达 {args.api}/health: {exc}")
        print("       先启动：cd python && .venv/Scripts/python.exe -m uvicorn main:app --port 8000")
        return 2

    print(f"跑 {len(cases)} 条用例 -> {args.api}")
    t0 = time.perf_counter()
    runs = []
    for i, case in enumerate(cases, 1):
        r = run_case(case, args.api, args.timeout)
        runs.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        extra = "" if r["passed"] else f"  <- {r['failures'][0][:60]}"
        print(f"  [{i:>2}/{len(cases)}] {r['id']:<10} {mark}"
              f"  {r.get('total_latency_ms') or 0:>7.0f}ms"
              f"  商品{r.get('product_count', 0)}{extra}")
    elapsed = time.perf_counter() - t0

    report = build_report(runs, args.api)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M")
    out_path = out_dir / f"{stamp}-{report['meta']['git_sha']}.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    s = report["summary"]
    print("\n" + "=" * 66)
    print(f"通过 {s['passed']}/{s['cases']}  ({s['pass_rate']:.0%})   总耗时 {elapsed:.0f}s")
    print(f"  延迟 p50/p95 : {s['latency_p50_ms']} / {s['latency_p95_ms']} ms")
    print(f"  平均成本     : ${s['cost_mean_usd']}" if s["cost_mean_usd"] is not None
          else "  平均成本     : n/a（价格未知）")
    print(f"  输出 token   : {s['output_tokens_p50']}")
    print(f"\n报告已写入 {out_path}")

    if args.baseline:
        print_delta(report, Path(args.baseline))
    else:
        print("\n提示：下次跑时加 --baseline <本次报告路径> 可打印差值。")

    return 0 if s["passed"] == s["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
