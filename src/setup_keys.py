#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_keys.py — 通用 API key 录入工具 (支持多交易所、多账户)
"""
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SECRETS_DIR = os.path.join(ROOT, "secrets")
ENV_PATH = os.path.join(SECRETS_DIR, ".env")

def _mask(s: str) -> str:
    if not s:
        return "<空>"
    return s[:4] + "****" + s[-4:] if len(s) > 8 else "****"

def _write_env(pairs: list[tuple[str, str]]):
    os.makedirs(SECRETS_DIR, exist_ok=True)
    
    existing_env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing_env[k.strip()] = v.strip()

    for k, v in pairs:
        existing_env[k] = v

    lines = [f"{k}={v}" for k, v in existing_env.items()]
    
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    
    try:
        os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    
    if os.name == "nt":
        try:
            os.system(f'icacls "{ENV_PATH}" /inheritance:r /grant:r "%USERNAME%":(R,W) >nul 2>&1')
        except Exception:
            pass

def main():
    print("=" * 60)
    print("GateRiskWatcher — 通用 API key 录入工具")
    print("=" * 60)
    print("说明：支持多交易所录入。")
    print("      录入后会自动合并到现有的 .env 文件中。")
    print()

    all_pairs: list[tuple[str, str]] = []

    while True:
        print("--- 选择交易所 ---")
        print("1. Gate.io")
        print("2. OKX")
        print("3. Binance")
        print("4. 自定义名称 (Custom)")
        print("0. 完成录入，退出")

        choice = input("请选择 (0-4): ").strip()

        if choice == '0':
            break

        if choice == '1':
            prefix = "GATE"
            has_extra_auth = False
        elif choice == '2':
            prefix = "OKX"
            has_extra_auth = True
        elif choice == '3':
            prefix = "BINANCE"
            has_extra_auth = False
        elif choice == '4':
            prefix = input("请输入自定义交易所名称 (例如 MYEX): ").strip().upper()
            if not prefix:
                print("名称不能为空，返回交易所选择。")
                continue
            has_extra_auth = input("该交易所是否需要额外认证参数（如密码短语）？(y/N): ").strip().lower() == "y"
        else:
            print("无效选择，返回交易所选择。")
            continue

        print(f"\n当前模式: [{prefix}] 交易所录入")
        print("=" * 60)

        new_pairs: list[tuple[str, str]] = []
        idx = 1

        while True:
            suffix = "" if idx == 1 else f"_{idx}"
            print(f"--- 账户 {idx} ({prefix}_{suffix}) ---")

            key = input(f"  {prefix}_API_KEY{suffix} (直接回车结束当前交易所录入): ").strip()

            if not key:
                if idx == 1:
                    print("  未检测到任何 Key 输入，返回交易所选择。")
                else:
                    print("  录入结束，返回交易所选择。")
                break

            secret = input(f"  {prefix}_API_SECRET{suffix}: ").strip()
            if not secret:
                print("  secret 不能为空，跳过该账户。")
                continue

            extra_pairs: list[tuple[str, str]] = []
            if has_extra_auth:
                extra_key = input("  额外认证参数的.env变量名（例如 OKX_PASSPHRASE），留空跳过: ").strip()
                if extra_key:
                    print(f"    → 接下来请输入 {extra_key} 的真实值（例如您的 OKX 密码短语）。直接回车将跳过此参数，不会写入 .env。")
                    extra_val = input(f"  {extra_key}: ").strip()
                    if extra_val:
                        extra_pairs.append((extra_key, extra_val))

            new_pairs.append((f"{prefix}_API_KEY{suffix}", key))
            new_pairs.append((f"{prefix}_API_SECRET{suffix}", secret))
            if extra_pairs:
                new_pairs.extend(extra_pairs)

            idx += 1
            more = input("  还要为该交易所添加更多账户吗？(y/N): ").strip().lower()
            if more != "y":
                break

        if new_pairs:
            _write_env(new_pairs)
            all_pairs.extend(new_pairs)
            print("\n" + "=" * 60)
            print(f"✅ [{prefix}] 录入成功，已更新至: {ENV_PATH}")
            print("=" * 60)
            again = input("按回车继续录入其他交易所，输入 q 退出: ").strip().lower()
            if again == 'q':
                break
        else:
            print(f"  [{prefix}] 未检测到任何 Key 输入，返回交易所选择。")

    if not all_pairs:
        print("没有新数据需要写入，退出。")
        sys.exit(0)

    print("\n" + "=" * 60)
    print(f"✅ 全部录入完成，共计 {len(all_pairs)} 项，已更新至: {ENV_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
