#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键把 dist/BTSPK.exe 发布为 GitHub Release。

用法：
    python release.py                       # 版本号从最新 tag 自动 +1（补丁位）
    python release.py v1.1.0                # 指定版本号
    python release.py v1.1.0 --notes notes.md   # 用文件作为发布说明
    python release.py v1.1.0 --draft        # 存为草稿，先不公开
    python release.py v1.1.0 --tag-only     # 只打 tag，不发 Release

前置：
    - 已执行 `python -m PyInstaller --noconfirm --clean build_exe.spec`
      生成 dist/BTSPK.exe
    - git 凭据管理器里存有 GitHub 账号（git push 能通即可，无需 gh CLI）

凭据通过 `git credential fill` 实时读取，不落盘、不写进任何文件。
注意：默认**直连**不走本地代理 —— 本地代理转发大文件上传会返回 502。
      若你的网络必须走代理，加 --no-proxy。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OWNER = "faithxie"
REPO = "Bluetooch_Mic_Speacker"
EXE = os.path.join(ROOT, "dist", "BTSPK.exe")
SHOT = "docs/screenshot-main.png"


# ---------------------------------------------------------------- 凭据 / HTTP

def get_creds():
    """从 git 凭据管理器取 GitHub 用户名与 token。"""
    r = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, cwd=ROOT,
    )
    if r.returncode != 0:
        sys.exit("git credential fill 失败：" + r.stderr.strip())
    vals = dict(l.split("=", 1) for l in r.stdout.strip().splitlines() if "=" in l)
    if not vals.get("password"):
        sys.exit("凭据管理器里没有 github.com 的密码/token")
    return vals.get("username"), vals["password"]


def make_opener(direct=True):
    """direct=True 走直连（本地代理会拦截大文件上传，返回 502）。"""
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


OPENER = None


def api(method, path, token, data=None, raw=None,
        ctype="application/json", timeout=180):
    """调 GitHub API。path 可以是 /repos/... 或完整 URL。"""
    url = path if path.startswith("http") else "https://api.github.com" + path
    body = raw if raw is not None else (
        json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "BTSPK-release")
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(txt) if txt.strip() else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt}


def retry(fn, tries=5, label="", delay=3):
    """返回 (status, data)；彻底失败返回 (0, 异常)，保证调用方可解包。"""
    last = None
    for i in range(1, tries + 1):
        try:
            r = fn()
            if isinstance(r, tuple) and r[0] and r[0] >= 400:
                last = r
                print(f"  [{label}] 第 {i} 次：HTTP {r[0]} {str(r[1])[:160]}")
                if r[0] < 500 and r[0] != 429:
                    return r          # 4xx 是真实错误，不重试
            else:
                return r
        except Exception as e:
            last = e
            print(f"  [{label}] 第 {i} 次：{type(e).__name__}: {str(e)[:160]}")
        time.sleep(delay * i)
    return last if isinstance(last, tuple) else (0, last)


# ---------------------------------------------------------------- git 辅助

def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def latest_tag():
    _, out, _ = git("tag", "-l", "v*", "--sort=-v:refname")
    return out.splitlines()[0] if out.strip() else None


def bump(tag):
    """v1.0.0 -> v1.0.1；无 tag 时从 v1.0.0 开始。"""
    if not tag:
        return "v1.0.0"
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", tag)
    if not m:
        sys.exit(f"最新 tag {tag} 不符合 vX.Y.Z 格式，请手动指定版本号")
    a, b, c = (int(x) for x in m.groups())
    return f"v{a}.{b}.{c + 1}"


# ---------------------------------------------------------------- 发布说明

def default_body(tag, sha256, size_mb, date):
    shot_url = (f"https://raw.githubusercontent.com/{OWNER}/{REPO}"
                f"/master/{SHOT}")
    return f"""## 蓝牙音频路由 BTSPK {tag}

把你的蓝牙耳机变成一支无线麦克风 —— 声音实时从电脑音响 / HDMI 播出。

### 下载

| 文件 | 说明 |
|------|------|
| **BTSPK.exe** | Windows 免安装单文件版，双击即用（{size_mb:.1f} MB） |

> 无需安装 Python 或任何依赖，PortAudio / WASAPI 已打包在内。

### 功能

- 🎧 **蓝牙耳机麦克风 → 扬声器 / HDMI 实时路由**，端到端延迟可监控
- 🛡️ **四道防啸叫防线**：自适应陷波 + 整体移频 + 智能门限 + 多判据检测
- 🔊 **深色主题界面**：信号链路双卡片、渐变电平表（绿→黄→红 + 峰值保持）、延迟/缓冲/丢帧指标
- 🔄 **跟随系统默认设备**：Windows 里切换输入输出后自动生效，可手动刷新重载 PortAudio
- 💾 设备偏好记忆（`%APPDATA%\\BTSPK\\.device_prefs.json`），日志落在 `%APPDATA%\\BTSPK\\BTSPK.log`

![界面]({shot_url})

### 使用步骤

1. 系统设置里连上蓝牙耳机，并把**输入**设备选为它
2. 双击 `BTSPK.exe` 启动
3. 确认「输入 · 麦克风」是蓝牙耳机，「输出 · 扬声器/HDMI」是房间音响
4. 点 ▶ **开始路由**，对着耳机说话即可

> 💡 降低啸叫最有效的办法是物理层面：让麦克风远离扬声器、降低扬声器音量。软件算法只是兜底。

### 系统要求

- Windows 10 / 11（x64）
- 蓝牙耳机（支持 HFP 免提协议）或任意麦克风
- 输出端：扬声器 / 音响 / HDMI 显示器 / 电视

### 校验

```
SHA256  BTSPK.exe
{sha256}
```

---

**完整文档见 [README](https://github.com/{OWNER}/{REPO}#readme)** · 构建于 {date}
"""


# ---------------------------------------------------------------- 主流程

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="发布 BTSPK.exe 为 GitHub Release")
    ap.add_argument("version", nargs="?", help="版本号，如 v1.1.0（默认自动 +1）")
    ap.add_argument("--notes", help="自定义发布说明文件（Markdown）")
    ap.add_argument("--title", help="Release 标题")
    ap.add_argument("--draft", action="store_true", help="存为草稿")
    ap.add_argument("--prerelease", action="store_true", help="标记为预发布")
    ap.add_argument("--tag-only", action="store_true", help="只打 tag，不发 Release")
    ap.add_argument("--no-proxy", action="store_true",
                    help="走本地代理（默认直连，代理会拦大文件上传）")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    args = ap.parse_args()

    global OPENER
    OPENER = make_opener(direct=not args.no_proxy)

    if not os.path.exists(EXE):
        sys.exit(f"找不到 {EXE}\n请先构建：\n"
                 f"  python -m PyInstaller --noconfirm --clean build_exe.spec")

    ver = args.version or bump(latest_tag())
    if not re.match(r"^v\d+\.\d+\.\d+", ver):
        sys.exit(f"版本号格式应为 vX.Y.Z，收到：{ver}")

    user, token = get_creds()
    print(f"账号 {user} · 版本 {ver}")

    st, me = retry(lambda: api("GET", "/user", token), label="鉴权")
    if st != 200:
        sys.exit("token 无效或网络不通：" + str(me)[:300])

    rc, head, _ = git("rev-parse", "HEAD")
    if rc != 0:
        sys.exit("无法读取 git HEAD")

    _, dirty, _ = git("status", "--porcelain")
    if dirty:
        print("⚠️  工作区有未提交的改动，Release 会指向最后一次提交：")
        for line in dirty.splitlines()[:10]:
            print("   ", line)
        if not args.yes and input("继续？[y/N] ").strip().lower() != "y":
            sys.exit("已取消")

    # ---- 1) tag
    st, existing = retry(
        lambda: api("GET", f"/repos/{OWNER}/{REPO}/git/ref/tags/{ver}", token),
        tries=2, label="查 tag")
    if st == 200:
        sha = existing.get("object", {}).get("sha", "")
        if sha and sha != head:
            sys.exit(f"tag {ver} 已存在且指向 {sha[:8]}，与当前 HEAD {head[:8]} 不同，"
                     f"请换个版本号")
        print(f"tag {ver} 已存在（{sha[:8]}）")
    else:
        st, res = retry(lambda: api("POST", f"/repos/{OWNER}/{REPO}/git/refs", token,
                                    {"ref": f"refs/tags/{ver}", "sha": head}),
                        label="建 tag")
        if st not in (200, 201):
            sys.exit("创建 tag 失败：" + str(res)[:400])
        print(f"tag {ver} -> {head[:8]}")
        git("push", "origin", f"refs/tags/{ver}")

    if args.tag_only:
        print("仅打 tag，跳过 Release。完成。")
        return

    # ---- 2) Release
    st, rel = retry(
        lambda: api("GET", f"/repos/{OWNER}/{REPO}/releases/tags/{ver}", token),
        tries=2, label="查 release")
    if st == 200:
        print("Release 已存在，复用 id =", rel["id"])
    else:
        if args.notes:
            with open(args.notes, encoding="utf-8") as f:
                body = f.read()
        else:
            body = default_body(ver, sha256_of(EXE),
                                os.path.getsize(EXE) / 1048576,
                                time.strftime("%Y-%m-%d"))
        st, rel = retry(lambda: api("POST", f"/repos/{OWNER}/{REPO}/releases", token, {
            "tag_name": ver,
            "target_commitish": head,
            "name": args.title or ver,
            "body": body,
            "draft": args.draft,
            "prerelease": args.prerelease,
        }), label="建 release")
        if st not in (200, 201):
            sys.exit("创建 Release 失败：" + str(rel)[:400])
        print("Release 创建成功 id =", rel["id"])

    rel_id, upload_url = rel["id"], rel["upload_url"].split("{")[0]

    # ---- 3) 附件
    st, assets = retry(
        lambda: api("GET", f"/repos/{OWNER}/{REPO}/releases/{rel_id}/assets", token),
        label="列附件")
    for a in (assets if isinstance(assets, list) else []):
        if a.get("name") == "BTSPK.exe":
            print("删除同名旧附件 id =", a["id"])
            retry(lambda: api("DELETE",
                              f"/repos/{OWNER}/{REPO}/releases/assets/{a['id']}", token),
                  label="删附件")

    size = os.path.getsize(EXE)
    print(f"上传 BTSPK.exe（{size / 1048576:.1f} MB）…")
    with open(EXE, "rb") as f:
        payload = f.read()
    st, asset = retry(lambda: api("POST", upload_url + "?name=BTSPK.exe", token,
                                  raw=payload, ctype="application/octet-stream",
                                  timeout=1800), tries=3, delay=5, label="上传")
    if st not in (200, 201):
        sys.exit("上传失败：" + str(asset)[:400])

    print("\n✅ 发布完成")
    print("   Release :", rel["html_url"])
    print("   附件    :", asset.get("name"), f"{asset.get('size', 0) / 1048576:.1f} MB")
    print("   下载直链:", asset.get("browser_download_url"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n已中断")
