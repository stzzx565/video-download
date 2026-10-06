# -*- coding: utf-8 -*-
"""
无水印视频下载器 — 桌面界面版
基于开源项目 Douyin_TikTok_Download_API(Evil0ctal,Apache-2.0)封装
支持:抖音 / 快手 / 小红书 / 哔哩哔哩 / 微信视频号 / TikTok 的解析下载
(视频号需一次性浏览器登录腾讯元宝授权;微博因登录态限制暂不支持)
仅供学习交流使用,请勿用于违法用途。

命令行模式:
  无水印视频下载器.pyw --selftest   运行自检(解析+下载)
  无水印视频下载器.pyw <链接>...    命令行解析链接后退出
"""

import sys
import threading
import time
import traceback
import os

import downloader_core
from downloader_core import (Core, Cancelled, ParseError, WxNeedLoginError,
                             extract_urls, APP_DIR, recover_proxy_leak)

DEFAULT_SAVE_DIR = os.path.join(APP_DIR, "下载")
SELFTEST_BILI_URL = "https://www.bilibili.com/video/BV1GJ411x7h7"
SELFTEST_KS_URL = "https://v.kuaishou.com/JPZ3huAa"
SELFTEST_XHS_URL = ("https://www.xiaohongshu.com/explore/"
                    "6a7adfb900000000260341f8"
                    "?xsec_token=ABQYGj284PsxGhhgO8RmnTGKCCm9ip7udkJCkIYF-WJPY="
                    "&xsec_source=")


# ---------------------------------------------------------------------------
# 命令行入口(无界面)
# ---------------------------------------------------------------------------
def run_headless(argv):
    # 打包为窗口程序后没有控制台,print 目标为 None,重定向到日志文件
    if sys.stdout is None:
        sys.stdout = open(os.path.join(APP_DIR, "headless_print.log"), "w",
                          encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    sys.stdout.reconfigure(encoding="utf-8")
    if "--selftest" in argv:
        return run_selftest()
    urls = extract_urls(" ".join(argv))
    if not urls:
        print("用法:无水印视频下载器.pyw <视频链接> [--proxy http://127.0.0.1:7890]")
        return 1
    proxy = ""
    if "--proxy" in argv:
        i = argv.index("--proxy")
        if i + 1 < len(argv):
            proxy = argv[i + 1]
    print(f"共提取到 {len(urls)} 条链接,开始解析...")
    items = downloader_core.run_parse_batch(urls, proxy=proxy,
                                            on_each=lambda it: print_item(it))
    ok, fail = downloader_core.run_download_batch(
        items, DEFAULT_SAVE_DIR, proxy=proxy,
        on_event=lambda msg: print(msg))
    print(f"完成:成功 {ok} 个,失败 {fail} 个。保存目录:{DEFAULT_SAVE_DIR}")
    return 0


def print_item(it):
    if it.get("error"):
        print(f"[失败] {it['url']}\n       {it['error']}")
    else:
        print(f"[成功] {it['platform']} {it['type']} | {it['author']} | "
              f"{it['title'][:40]} | 直链数:{len(it['files'])}")


def run_selftest():
    log_path = os.path.join(APP_DIR, "selftest_result.txt")
    lines = []

    def log(msg):
        lines.append(str(msg))
        print(msg)

    log(f"自检开始 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Python: {sys.version}")
    log(f"打包模式: {'是' if getattr(sys, 'frozen', False) else '否'}")
    try:
        items = downloader_core.run_parse_batch(
            ["https://v.douyin.com/L4FJNR3/", SELFTEST_KS_URL,
             SELFTEST_XHS_URL, SELFTEST_BILI_URL])
        for it in items:
            if it.get("error"):
                log(f"解析失败 [{it['platform']}]:{it['error']}")
            else:
                log(f"解析成功 [{it['platform']}] {it['type']} | "
                    f"{it['author']} | {it['title'][:40]}")
        downloadable = [it for it in items if not it.get("error")]
        if downloadable:
            out_dir = os.path.join(APP_DIR, "selftest_out")
            ok, fail = downloader_core.run_download_batch(
                downloadable, out_dir, on_event=lambda m: log(m))
            log(f"下载完成:成功 {ok} 个,失败 {fail} 个")
    except Exception as exc:
        log(f"自检异常:{exc}")
        log(traceback.format_exc())
    log("自检结束")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return 0


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
def run_gui(smoke: bool = False):
    import queue
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # 启动自愈:历史版本若异常退出导致系统代理残留,此处自动还原
    try:
        recover_proxy_leak()
    except Exception:
        pass

    try:  # 高分屏下文字更清晰
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    class App:
        def __init__(self, root):
            self.root = root
            self.core = None
            self._busy = False
            self._cancel = threading.Event()
            self._results = {}      # iid -> item dict
            self._seq = 0
            # 工作线程 → 主线程 的界面更新队列(root.after 不能跨线程调用)
            self._ui_queue = queue.Queue()
            root.after(50, self._drain_ui_queue)

            root.title("无水印视频下载器 — 抖音 / 快手 / 小红书 / 哔哩哔哩 / "
                       "视频号 / TikTok")
            root.geometry("920x700")
            root.minsize(780, 560)

            style = ttk.Style()
            try:
                style.theme_use("vista")
            except Exception:
                pass
            default_font = ("Microsoft YaHei UI", 10)
            root.option_add("*Font", default_font)
            style.configure("Treeview", rowheight=26, font=default_font)
            style.configure("Treeview.Heading",
                            font=("Microsoft YaHei UI", 10, "bold"))

            self._build_widgets()

        # ---------------------------------------------------------- 界面搭建
        def _build_widgets(self):
            pad = {"padx": 8, "pady": 4}

            top = ttk.LabelFrame(self.root,
                                 text=" 第一步:粘贴分享链接或口令(每行一条,可批量) ")
            top.pack(fill="x", **pad)
            self.url_text = tk.Text(top, height=5, wrap="word", undo=True,
                                    font=("Microsoft YaHei UI", 10))
            self.url_text.pack(fill="x", padx=6, pady=6)
            self.url_text.insert(
                "1.0",
                "例如:\n"
                "7.43 pda:/ 让你在几秒钟之内记住我  "
                "https://v.douyin.com/L5pbfdP/ "
                "复制此链接,打开Dou音搜索,直接观看视频!\n"
                "https://v.kuaishou.com/JPZ3huAa "
                "复制此链接,打开【快手App】直接观看!\n"
                "https://weixin.qq.com/sph/Axv548mzBF "
                "视频号分享链接(首次下载需按提示完成一次授权)\n"
                "https://www.bilibili.com/video/BV1GJ411x7h7")

            btns = ttk.Frame(self.root)
            btns.pack(fill="x", **pad)
            self.btn_parse = ttk.Button(btns, text="🔍 解析链接",
                                        command=self.on_parse)
            self.btn_parse.pack(side="left", padx=(8, 4))
            self.btn_dl_sel = ttk.Button(
                btns, text="⬇ 下载选中", command=lambda: self.on_download(True),
                state="disabled")
            self.btn_dl_sel.pack(side="left", padx=4)
            self.btn_dl_all = ttk.Button(
                btns, text="⬇ 下载全部", command=lambda: self.on_download(False),
                state="disabled")
            self.btn_dl_all.pack(side="left", padx=4)
            self.btn_cancel = ttk.Button(btns, text="✖ 取消",
                                         command=self.on_cancel,
                                         state="disabled")
            self.btn_cancel.pack(side="left", padx=4)
            self.btn_clear = ttk.Button(btns, text="清空结果",
                                        command=self.on_clear)
            self.btn_clear.pack(side="right", padx=(4, 8))

            dirf = ttk.LabelFrame(self.root,
                                  text=" 第二步:选择保存位置(可选设置) ")
            dirf.pack(fill="x", **pad)
            ttk.Label(dirf, text="保存目录:").grid(row=0, column=0, sticky="w",
                                                    padx=6, pady=4)
            self.dir_var = tk.StringVar(value=DEFAULT_SAVE_DIR)
            ttk.Entry(dirf, textvariable=self.dir_var).grid(
                row=0, column=1, sticky="we", padx=4)
            ttk.Button(dirf, text="浏览...", command=self.on_browse).grid(
                row=0, column=2, padx=4)
            ttk.Button(dirf, text="打开目录",
                       command=self.on_open_dir).grid(row=0, column=3, padx=4)
            ttk.Label(dirf, text="代理(可选,如 http://127.0.0.1:7890,"
                                 "TikTok 需要):").grid(row=1, column=0,
                                                        sticky="w", padx=6)
            self.proxy_var = tk.StringVar()
            ttk.Entry(dirf, textvariable=self.proxy_var).grid(
                row=1, column=1, sticky="we", padx=4)
            ttk.Label(dirf, text="(留空 = 直连)").grid(row=1, column=2,
                                                        sticky="w")
            ttk.Button(dirf, text="视频号说明",
                       command=self.on_show_wx_help).grid(row=1, column=3,
                                                          padx=4)
            dirf.columnconfigure(1, weight=1)

            mid = ttk.LabelFrame(self.root,
                                 text=" 第三步:解析结果(双击结果行可复制无水印直链) ")
            mid.pack(fill="both", expand=True, **pad)
            cols = ("no", "platform", "type", "author", "title", "status")
            self.tree = ttk.Treeview(mid, columns=cols, show="headings",
                                     selectmode="extended")
            headers = [("no", "序号", 50, "center"),
                       ("platform", "平台", 80, "center"),
                       ("type", "类型", 90, "center"),
                       ("author", "作者", 130, "w"),
                       ("title", "标题", 380, "w"),
                       ("status", "状态", 110, "center")]
            for cid, text, width, anchor in headers:
                self.tree.heading(cid, text=text)
                self.tree.column(cid, width=width, anchor=anchor)
            vsb = ttk.Scrollbar(mid, orient="vertical",
                                command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
            vsb.grid(row=0, column=1, sticky="ns", pady=6)
            mid.rowconfigure(0, weight=1)
            mid.columnconfigure(0, weight=1)
            self.tree.tag_configure("error", foreground="#c0392b")
            self.tree.tag_configure("done", foreground="#1e8449")
            self.tree.bind("<Double-1>", self.on_copy_link)

            bottom = ttk.Frame(self.root)
            bottom.pack(fill="x", **pad)
            self.progress = ttk.Progressbar(bottom, mode="determinate",
                                            maximum=100)
            self.progress.pack(fill="x", padx=8)
            self.status_var = tk.StringVar(
                value="就绪。粘贴链接后点击【解析链接】。")
            ttk.Label(bottom, textvariable=self.status_var,
                      anchor="w").pack(fill="x", padx=8, pady=(2, 0))

            logf = ttk.LabelFrame(self.root, text=" 运行日志 ")
            logf.pack(fill="both", expand=False, padx=8, pady=(0, 8))
            self.log_text = tk.Text(logf, height=6, wrap="word",
                                    state="disabled",
                                    font=("Consolas", 9),
                                    background="#f7f7f7")
            log_sb = ttk.Scrollbar(logf, orient="vertical",
                                   command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=log_sb.set)
            self.log_text.pack(side="left", fill="both", expand=True,
                               padx=(6, 0), pady=6)
            log_sb.pack(side="right", fill="y", pady=6)

        # ---------------------------------------------------------- 工具方法
        def ui(self, fn, *args):
            """把界面更新调度回主线程(线程安全队列)"""
            self._ui_queue.put(lambda: self._safe(fn, *args))

        def _safe(self, fn, *args):
            try:
                fn(*args)
            except Exception:
                pass

        def _drain_ui_queue(self):
            try:
                while True:
                    fn = self._ui_queue.get_nowait()
                    fn()
            except Exception:
                pass
            self.root.after(50, self._drain_ui_queue)

        def log(self, msg):
            def _do():
                stamp = time.strftime("%H:%M:%S")
                self.log_text.configure(state="normal")
                self.log_text.insert("end", f"[{stamp}] {msg}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            self.ui(_do)

        def set_status(self, text):
            self.ui(lambda: self.status_var.set(text))

        def set_progress(self, percent):
            self.ui(lambda: self.progress.configure(
                value=max(0, min(100, percent))))

        def set_busy(self, busy, note=""):
            self._busy = busy
            state = "disabled" if busy else "normal"
            for btn in (self.btn_parse, self.btn_dl_sel, self.btn_dl_all,
                        self.btn_clear):
                self.ui(lambda b=btn, s=state: b.configure(state=s))
            self.ui(lambda: self.btn_cancel.configure(
                state="normal" if busy else "disabled"))
            if note:
                self.set_status(note)

        # ---------------------------------------------------------- 解析
        def on_parse(self):
            if self._busy:
                return
            urls = extract_urls(self.url_text.get("1.0", "end"))
            if not urls:
                messagebox.showwarning("提示", "请先粘贴包含视频链接的文本。")
                return
            self._cancel.clear()
            self.core = Core(self.proxy_var.get())
            self.core._status_hook = lambda m: self.log("[视频号] " + str(m))
            self.set_busy(True, f"正在解析 {len(urls)} 条链接...")
            self.log(f"开始解析 {len(urls)} 条链接")
            threading.Thread(target=self._parse_worker, args=(urls,),
                             daemon=True).start()

        def _parse_worker(self, urls):
            import asyncio

            async def run():
                for i, url in enumerate(urls, start=1):
                    if self._cancel.is_set():
                        self.log("解析已取消")
                        break
                    try:
                        item = await self.core.parse(url)
                    except WxNeedLoginError:
                        # 视频号首次下载:引导完成一次元宝扫码授权后重试
                        self.log("[视频号] 需要一次元宝扫码授权")
                        ok = self._run_wx_login_flow()
                        if not ok:
                            self.log("[视频号] 已取消授权,跳过该链接")
                            item = {"url": url, "platform": "视频号",
                                    "type": "", "author": "", "title": "",
                                    "aweme_id": "", "files": [],
                                    "error": "已取消视频号授权"}
                        else:
                            item = await self.core.parse(url)
                    except ParseError as exc:
                        item = {"url": url, "platform": "未知", "type": "",
                                "author": "", "title": "", "aweme_id": "",
                                "files": [], "error": str(exc)}
                    except Cancelled:
                        break
                    except Exception as exc:
                        item = {"url": url, "platform": "未知", "type": "",
                                "author": "", "title": "", "aweme_id": "",
                                "files": [],
                                "error": f"{type(exc).__name__}: {exc}"}
                    self.ui(self._add_row, item)
                    self.set_status(f"解析进度 {i}/{len(urls)}")
                self.ui(self._parse_done, self._cancel.is_set())

            try:
                asyncio.run(run())
            except Exception as exc:
                self.log(f"解析线程异常:{exc}")
                self.ui(self._parse_done, False)

        def _run_wx_login_flow(self):
            """打开元宝登录窗口,等用户扫码并确认,返回是否授权成功"""
            import wx_login
            result = {"ok": False}
            ev = threading.Event()
            try:
                login = wx_login.LoginSession(
                    on_status=lambda m: self.log("[视频号] " + m))
                login.start()
            except Exception as exc:
                self.log(f"[视频号] 登录窗口启动失败:{exc}")
                return False

            def show_dialog():
                dlg = tk.Toplevel(self.root)
                dlg.title("视频号授权")
                dlg.geometry("420x200")
                dlg.grab_set()
                ttk.Label(dlg, text=(
                    "在弹出的浏览器窗口中:\n"
                    "1. 用微信扫码登录腾讯元宝\n"
                    "2. 看到元宝页面后,点击下方按钮"
                ), padding=12).pack()
                status_lbl = ttk.Label(dlg, text="", foreground="#555")
                status_lbl.pack()

                def on_grab_done():
                    if result["ok"]:
                        status_lbl.configure(text="登录态已保存。")
                        dlg.after(600, lambda: (ev.set(), dlg.destroy()))
                    else:
                        status_lbl.configure(
                            text=result.get("error", "读取失败,请重试"))

                def confirm():
                    status_lbl.configure(text="正在读取登录态…")
                    dlg.update_idletasks()

                    def do_grab():
                        try:
                            login.grab()
                            result["ok"] = True
                        except Exception as e:
                            result["error"] = f"读取失败:{e}"
                        self.root.after(0, on_grab_done)

                    threading.Thread(target=do_grab, daemon=True).start()

                def cancel():
                    result["ok"] = False
                    ev.set()
                    dlg.destroy()

                ttk.Button(dlg, text="✔ 我已完成扫码登录",
                           command=confirm).pack(pady=(2, 2), fill="x",
                                                 padx=12)
                ttk.Button(dlg, text="取消", command=cancel).pack(
                    fill="x", padx=12)

            self.ui(show_dialog)
            ev.wait(timeout=300)
            try:
                login.close()
            except Exception:
                pass
            return result["ok"]

        def _add_row(self, item):
            self._seq += 1
            iid = f"r{self._seq}"
            self._results[iid] = item
            if item.get("error"):
                values = (self._seq, item.get("platform", ""), "—", "—",
                          item.get("url", ""), "失败")
                self.tree.insert("", "end", iid=iid, values=values,
                                 tags=("error",))
                self.log(f"[{self._seq}] 解析失败:{item['error']}")
            else:
                title = item["title"] or item["aweme_id"]
                values = (self._seq, item["platform"], item["type"],
                          item["author"] or "—", title, "已解析")
                self.tree.insert("", "end", iid=iid, values=values)
                self.log(f"[{self._seq}] 解析成功:{item['platform']} "
                         f"{item['type']} | {item['author']} | {title[:40]}")
            self.tree.see(iid)

        def _parse_done(self, cancelled):
            self.set_busy(False)
            has_ok = any(not it.get("error") for it in self._results.values())
            for btn in (self.btn_dl_sel, self.btn_dl_all):
                self.ui(lambda b=btn, ok=has_ok and not cancelled:
                        b.configure(state="normal" if ok else "disabled"))
            if cancelled:
                self.set_status("解析已取消。")
            else:
                self.set_status(f"解析完成,共 {len(self._results)} 条。"
                                "选择条目后点击【下载选中】或【下载全部】。")

        # ---------------------------------------------------------- 下载
        def on_download(self, selected_only):
            if self._busy:
                return
            if selected_only:
                iids = self.tree.selection()
                items = [self._results[i] for i in iids if i in self._results]
            else:
                items = list(self._results.values())
            items = [it for it in items if not it.get("error")]
            if not items:
                messagebox.showinfo(
                    "提示", "没有可下载的结果(请先解析,或选择有效条目)。")
                return
            save_dir = self.dir_var.get().strip() or DEFAULT_SAVE_DIR
            self._cancel.clear()
            self.set_busy(True, f"准备下载 {len(items)} 个条目...")
            self.log(f"开始下载 {len(items)} 个条目 → {save_dir}")
            self.set_progress(0)
            threading.Thread(target=self._download_worker,
                             args=(items, save_dir), daemon=True).start()

        def _download_worker(self, items, save_dir):
            import asyncio
            total = len(items)
            done = {"n": 0}

            async def run():
                for idx, item in enumerate(items, start=1):
                    if self._cancel.is_set():
                        self.log("下载已取消")
                        break
                    iid = self._iid_of(item)

                    def on_file_start(filename, seq, seq_total, item=item):
                        self.set_status(f"({done['n'] + 1}/{total}) "
                                        f"正在下载:{filename}")
                        self.log(f"[{item.get('aweme_id', '')}] "
                                 f"下载 {seq}/{seq_total}:{filename}")

                    def on_progress(received, total_bytes):
                        if total_bytes > 0:
                            self.set_progress(
                                (done["n"] + received / total_bytes)
                                / total * 100)

                    try:
                        if iid:
                            self.ui(self._set_row_status, iid, "下载中...")
                        paths = await self.core.download_item(
                            item, save_dir, idx,
                            on_file_start=on_file_start,
                            on_progress=on_progress,
                            cancel=self._cancel)
                        done["n"] += 1
                        if iid:
                            self.ui(self._set_row_status, iid, "完成 ✔", True)
                        self.log(f"[{idx}] 完成,共保存 {len(paths)} 个文件")
                    except Cancelled:
                        if iid:
                            self.ui(self._set_row_status, iid, "已取消")
                        break
                    except Exception as exc:
                        done["n"] += 1
                        if iid:
                            self.ui(self._set_row_status, iid, "失败",
                                    error=True)
                        self.log(f"[{idx}] 下载失败:{exc}")
                    self.set_progress(done["n"] / total * 100)
                self.ui(self._download_done, done["n"], total,
                        self._cancel.is_set())

            try:
                asyncio.run(run())
            except Exception as exc:
                self.log(f"下载线程异常:{exc}")
                self.ui(self._download_done, done["n"], total, False)

        def _iid_of(self, item):
            for iid, it in self._results.items():
                if it is item:
                    return iid
            return None

        def _set_row_status(self, iid, text, done=False, error=False):
            tags = []
            if error:
                tags.append("error")
            elif done:
                tags.append("done")
            self.tree.item(iid,
                           values=self.tree.item(iid, "values")[:5] + (text,),
                           tags=tags)

        def _download_done(self, ok_count, total, cancelled):
            self.set_busy(False)
            if cancelled:
                self.set_status(f"下载已取消(完成 {ok_count}/{total})。")
            else:
                self.set_status(f"下载完成:成功 {ok_count}/{total}。"
                                f"保存目录:{self.dir_var.get()}")
                self.log(f"全部任务结束:成功 {ok_count}/{total}")

        # ---------------------------------------------------------- 其他操作
        def on_cancel(self):
            self._cancel.set()
            self.set_status("正在取消...")

        def on_clear(self):
            if self._busy:
                return
            for iid in self.tree.get_children():
                self.tree.delete(iid)
            self._results.clear()
            self._seq = 0
            self.set_progress(0)
            self.set_status("已清空结果。")

        def on_browse(self):
            path = filedialog.askdirectory(
                initialdir=self.dir_var.get() or APP_DIR)
            if path:
                self.dir_var.set(os.path.normpath(path))

        def on_open_dir(self):
            path = self.dir_var.get().strip() or DEFAULT_SAVE_DIR
            os.makedirs(path, exist_ok=True)
            os.startfile(path)

        def on_show_wx_help(self):
            messagebox.showinfo(
                "视频号下载说明",
                "视频号支持粘贴分享链接直接下载:\n\n"
                "在微信里打开视频号 → 分享 → 复制链接,得到类似\n"
                "https://weixin.qq.com/sph/xxxx 的链接,粘贴进文本框即可。\n\n"
                "首次下载需要授权一次:\n"
                "程序会引导你用浏览器登录腾讯元宝(yuanbao.tencent.com,\n"
                "微信扫码即可),之后程序自动读取浏览器登录态,无需手动\n"
                "复制任何 Cookie。登录一次长期可用。\n\n"
                "注:在线解析服务部署在境外 Cloudflare,若提示连接超时,\n"
                "说明需要代理;程序会自动切换到国内直连路线(需上述登录)。"
            )

        def on_copy_link(self, _event):
            sel = self.tree.selection()
            if not sel:
                return
            item = self._results.get(sel[0])
            if not item or item.get("error"):
                return
            links = [e[1] if e[0] != "bili"
                     else "(B站分P,点击下载后自动获取)"
                     for e in item.get("files", [])]
            text = "\n".join(str(l) for l in links) or item.get("url", "")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_status("已复制无水印直链到剪贴板。")

        def on_close(self):
            """退出前清理后台资源并还原可能的系统代理残留"""
            try:
                recover_proxy_leak()
            except Exception:
                pass
            root.destroy()

    root = tk.Tk()
    root.protocol("WM_DELETE_WINDOW", lambda: app.on_close())
    app = App(root)
    if smoke:
        _run_smoke(app, root)
        return
    root.mainloop()


def _run_smoke(app, root):
    """冒烟模式:不进入主循环,自动填入链接→解析→检查结果→退出"""
    app.url_text.delete("1.0", "end")
    app.url_text.insert("1.0",
                        "https://v.douyin.com/L4FJNR3/\n"
                        "https://v.kuaishou.com/JPZ3huAa\n"
                        + SELFTEST_XHS_URL + "\n"
                        "https://www.bilibili.com/video/BV1GJ411x7h7")
    print("[smoke] 窗口与控件构建完成,开始自动解析测试")
    app.on_parse()
    deadline = time.time() + 180
    while time.time() < deadline:
        root.update()
        if not app._busy:
            break
        time.sleep(0.2)
    rows = app.tree.get_children()
    ok = 0
    for iid in rows:
        vals = app.tree.item(iid, "values")
        print(f"[smoke] 行:{vals}")
        if vals[5] == "已解析":
            ok += 1
    print(f"[smoke] 结果 {ok}/{len(rows)} 行解析成功")
    print("[smoke] RESULT:", "SUCCESS" if ok == len(rows) and rows else "FAIL")
    root.destroy()


def main():
    argv = sys.argv[1:]
    if "--smoke" in argv:
        run_gui(smoke=True)
        return
    if "--selftest" in argv or any(a.startswith("http") for a in argv):
        sys.exit(run_headless(argv))
    run_gui()


if __name__ == "__main__":
    main()
