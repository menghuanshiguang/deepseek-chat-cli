#!/usr/bin/env python3
"""
dsc - DeepSeek C端对话CLI (纯文本对话版,参考 dsv 设计)

用法:
  dsc "你好"
  dsc "用一句话介绍你自己"
  echo "你好" | dsc
  dsc --login <手机号>
  dsc --verify <短信验证码>
  dsc --logout
  dsc --keep "保留本次会话不删除"

原理:
  驱动浏览器打开 chat.deepseek.com,新建对话并发送纯文本提示词,
  等待网页端流式回答完成后输出文本,默认立即删除该会话。
  PoW 由浏览器前端自动计算,无需破解。登录态存 ~/.dsv_token。

输出:
  纯文本到 stdout(DeepSeek 的回答),日志到 stderr。
"""
import sys, os, subprocess, json, time, urllib.request, shutil

# ---------- 配置 ----------
TOKEN_FILE = os.path.expanduser("~/.dsv_token")
BASE = "https://chat.deepseek.com"
if sys.platform == "win32":
    # Windows: 用 Playwright 驱动系统 Edge (单文件, dsc.py 自身作子进程入口), 零浏览器下载
    B = [sys.executable, os.path.abspath(__file__), "--win-browser"]
elif shutil.which("minis-browser-use"):
    # iSH / OpenMinis: 使用系统自带 minis-browser-use
    B = ["minis-browser-use"]
else:
    # Linux/GitHub Actions: 使用 Playwright 驱动 Chromium (单文件子进程模式)
    B = [sys.executable, os.path.abspath(__file__), "--linux-browser"]

def log(*a):
    print(*a, file=sys.stderr)

def run(args, timeout=120):
    """执行 minis-browser-use 命令,返回 JSON"""
    try:
        r = subprocess.run(B + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = r.stdout.strip()
        if not out:
            return {"error": r.stderr[-500:]}
        return json.loads(out)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}

def js(script):
    """执行页面 JS,返回纯净值"""
    r = run(["execute_js", "--script", script])
    if isinstance(r, dict) and "error" in r:
        log("[dsc] 浏览器驱动错误:", r["error"])
    # 递归提取 text
    def extract(v):
        if isinstance(v, dict):
            if "text" in v:
                return clean(v["text"])
            # data 字段继续往下
            for k in ("data", "value", "result"):
                if k in v:
                    return extract(v[k])
            return v
        return v
    def clean(s):
        if isinstance(s, str):
            # 去掉 minis-browser-use 附加的 "\n  tab_id: N" 尾巴
            import re
            s = re.sub(r"\n\s*tab_id:\s*\d+", "", s).strip()
        return s
    return extract(r)

def get_token():
    """读取持久化 token,无效则提示登录"""
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        t = f.read().strip()
    # 验证有效性
    try:
        req = urllib.request.Request(BASE + "/api/v0/users/current",
            headers={"Authorization": "Bearer " + t, "accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
            if d.get("data", {}).get("biz_code") == 0:
                return t
    except Exception as e:
        log("[dsc] token验证异常:", e)
    return None

def ensure_login():
    """确保浏览器处于登录态,注入token"""
    tok = get_token()
    if not tok:
        # 由 main 统一处理(exit 4 + 登录指引, 符合 SKILL.md 契约)
        return None
    # 打开首页
    run(["navigate", "--url", BASE + "/"])
    time.sleep(1.0)
    # 注入 token 到 localStorage 并刷新
    js("localStorage.setItem('userToken', JSON.stringify({value:%s, __version:'0'})); location.reload();" % json.dumps(tok))
    time.sleep(1.5)
    return tok

def open_new_chat():
    """开新对话(每次对话独立会话, 便于用后即删)"""
    # 若当前已是空白新对话(无历史消息)则直接复用, 省一次navigate
    cur = js("""
    var marks=document.querySelectorAll('.ds-markdown');
    return String(marks.length === 0);
    """)
    if str(cur).strip().lower() == "true":
        return
    # 否则导航到根路径(即新对话)
    run(["navigate", "--url", BASE + "/"])
    time.sleep(1.5)
    # 尝试点"开启新对话"按钮
    js("""
    var nodes=[...document.querySelectorAll('div,span,button,[role=button]')];
    var t=null;
    for(var i=0;i<nodes.length;i++){var e=nodes[i];var x=(e.innerText||'').trim();if(x==='开启新对话'){t=e;break;}}
    if(!t) return 'no_btn';
    t.click(); return 'clicked';
    """)
    time.sleep(1.0)  # 等新对话渲染

def wait_for(js_script, timeout=30, interval=1.0, desc="条件"):
    """轮询执行 JS 直到返回 'true'(网页端状态就绪),超时返回 False"""
    start = time.time()
    while time.time() - start < timeout:
        r = js(js_script)
        if os.environ.get("DSV_DEBUG"):
            log(f"[dsc] wait[{desc}] -> {str(r).strip()!r}")
        if str(r).strip().lower() == "true":
            return True
        time.sleep(interval)
    log(f"[dsc] 等待超时({timeout}s): {desc}")
    return False

def send_and_wait(prompt, timeout=None):
    """输入问题,发送(等发送成功),轮询回答直到稳定"""
    if timeout is None:
        timeout = int(os.environ.get("DSC_TIMEOUT", "300"))
    # 记录发送前最后一条回答的内容作基线(数量+文本, 兼容欢迎语占用 .ds-markdown 的场景)
    # 必须在发送前采样: 发送后采样会与快速回复竞态(回复2秒完成时基线直接采到完成态, 轮询永不触发)
    try:
        base_info = js("""
        var m=document.querySelectorAll('.ds-markdown');
        return JSON.stringify({n:m.length, last:m.length? m[m.length-1].innerText : ''});
        """)
        import json as _j2
        _bi = _j2.loads(str(base_info).strip())
        base_n = int(_bi.get("n", 0))
        base_txt = _bi.get("last", "")
        log(f"[dsc] baseline(发送前): n={base_n} len={len(base_txt)}")
    except Exception:
        base_n, base_txt = 0, ""
        log("[dsc] baseline: 0(exc)")
    # 输入问题
    js("""
    var ta=document.querySelector('textarea');
    if(!ta) return 'no_ta';
    var setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    setter.call(ta, arguments);
    ta.dispatchEvent(new Event('input',{bubbles:true}));
    """.replace("arguments", json.dumps(prompt)))
    time.sleep(0.5)
    # 发送
    r = js("""
    var ta=document.querySelector('textarea');
    var c=ta.closest('div[class*=input],div[class*=composer],div[class*=footer]')||ta.parentElement.parentElement;
    var btns=c.querySelectorAll('div[role=button]');
    for(var i=0;i<btns.length;i++){var x=(''+btns[i].className);
      if(x.indexOf('ds-button--primary')>=0 && x.indexOf('iconLabelPrimary')<0){btns[i].click();return 'sent';}}
    return 'no_send';
    """)
    log("[dsc] 发送:", r)
    # 等发送成功: textarea 被清空 或 出现新消息(网页端发送成功标志)
    sent_ok = wait_for("""
    var ta=document.querySelector('textarea');
    var cleared = !ta || ta.value.length === 0;
    return String(cleared);
    """, timeout=10, interval=0.8, desc="textarea清空(发送成功)")
    if not sent_ok:
        # 兜底: 用 Enter 键发送
        log("[dsc] 按钮发送疑似失败,尝试 Enter")
        js("""
        var ta=document.querySelector('textarea');
        if(!ta) return 'no_ta';
        ta.focus();
        var ev=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true});
        ta.dispatchEvent(ev);
        return 'enter_sent';
        """)
        wait_for("""
        var ta=document.querySelector('textarea');
        return String(!ta || ta.value.length === 0);
        """, timeout=10, interval=0.8, desc="Enter发送后清空")
    # 发送后立即检测错误(违规/被拒时快速失败)
    time.sleep(2)
    err_early = js("""
    var t=document.body.innerText;
    if(t.indexOf('违反使用规范')>=0 || t.indexOf('消息未能发送')>=0){return 'rejected';}
    return 'ok';
    """)
    if str(err_early).strip() == "rejected":
        return "(DeepSeek拒绝处理: 内容违反使用规范或消息未能发送 - 换个提示词)"
    # 等回答开始出现: 消息条数增加 或 最后一条内容变化(流式生成) 或 出现"停止生成"按钮
    poll_js = """
    var m=document.querySelectorAll('.ds-markdown');
    var last = m.length? m[m.length-1].innerText : '';
    var stop = [...document.querySelectorAll('div[role=button],button')].some(function(b){
      var x=(b.innerText||'').trim(); return x.indexOf('停止')>=0 || x.indexOf('Stop')>=0;});
    return String((m.length > %d) || (last !== %s) || stop);
    """ % (base_n, json.dumps(base_txt))
    appeared = wait_for(poll_js, timeout=timeout, interval=1, desc="回答开始出现")
    if not appeared:
        # 兜底: 直接取最后一条文本
        cur = js("""
        var m=document.querySelectorAll('.ds-markdown');
        return m.length? m[m.length-1].innerText : '';
        """)
        return str(cur) if cur else "(超时未获取回答)"
    # 等回答稳定: 文本长度连续2次不再增长即视为完成(网页端回复结束标志)
    start = time.time()
    last_len = -1
    stable_count = 0
    last_text = ""
    while time.time() - start < timeout:
        # 错误检测: 消息被拒/违规时快速失败,不傻等
        err = js("""
        var t=document.body.innerText;
        if(t.indexOf('违反使用规范')>=0 || t.indexOf('消息未能发送')>=0){return 'rejected';}
        return 'ok';
        """)
        if str(err).strip() == "rejected":
            return "(DeepSeek拒绝处理: 内容违反使用规范或消息未能发送 - 换个提示词)"
        # 完成检测: 生成中"停止生成"按钮存在, 消失且内容>=15字=完成
        js("""
        var btns=[...document.querySelectorAll('div[role=button],button')];
        for(var i=0;i<btns.length;i++){var x=(''+(btns[i].innerText||'')).trim();
          if(x.indexOf('停止')>=0||x.indexOf('Stop')>=0){return 'false';}}
        return 'true';
        """)
        cur = js("""
        var m=document.querySelectorAll('.ds-markdown');
        return m.length? m[m.length-1].innerText : '';
        """)
        s = str(cur)
        l = len(s)
        if l > 0 and l == last_len and s == last_text:
            stable_count += 1
            if stable_count >= 2:  # 长度连续2次不变(~2s) → 回复完成
                return s
        elif l > 0:
            last_len = l
            last_text = s
            stable_count = 0
        time.sleep(0.5)
    return last_text if last_text else "(超时未获取回答)"

def delete_session(session_id, tok):
    """删除指定会话(用后即删,避免会话列表混乱)"""
    try:
        req = urllib.request.Request(BASE + "/api/v0/chat_session/delete",
            data=json.dumps({"chat_session_id": session_id}).encode(),
            headers={"Authorization": "Bearer " + tok, "accept": "application/json",
                     "content-type": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
            return d.get("data", {}).get("biz_code") == 0
    except Exception as e:
        log("[dsc] 删除会话失败:", e)
        return False

def get_current_session_id():
    """从当前页面 URL 提取 chat_session_id"""
    r = js("return location.href;")
    url = str(r) if r else ""
    # 格式: /a/chat/s/<session_id>
    if "/a/chat/s/" in url:
        sid = url.split("/a/chat/s/")[-1].split("?")[0].strip()
        if len(sid) == 36:  # uuid
            return sid
    log("[dsc] 未识别到会话URL:", url[:80])
    return None

def solve_captcha_with_opencv():
    """从页面数美验证弹窗下载验证图, 用OpenCV定位目标, 返回页面点击坐标"""
    # 取验证图 URL 和页面位置
    info = js("""
    var imgs=[...document.querySelectorAll('img')].filter(function(im){return /fengkong/.test(im.src||'');});
    if(!imgs.length) return 'NO_IMG';
    var im=imgs[0]; var b=im.getBoundingClientRect();
    return [im.src, b.x, b.y, b.width, b.height, im.naturalWidth, im.naturalHeight].join('|');
    """)
    if not info or info == 'NO_IMG' or not isinstance(info, str):
        log("[dsc] 未找到验证图")
        return None
    try:
        parts = str(info).strip().split("|")
        d = {"src": parts[0], "x": float(parts[1]), "y": float(parts[2]),
             "w": float(parts[3]), "h": float(parts[4]),
             "nw": float(parts[5]), "nh": float(parts[6])}
    except Exception:
        log("[dsc] 验证图信息解析失败:", info)
        return None
    # 下载图片
    import urllib.request as _u
    import tempfile as _tf
    tmp = os.path.join(_tf.gettempdir(), "dsv_captcha.jpg")
    try:
        _u.urlretrieve(d["src"], tmp)
    except Exception as e:
        log("[dsc] 验证图下载失败:", e)
        return None
    # OpenCV 分析: HSV 提取目标色 → 连通域 → 中心
    try:
        import numpy as np
        from PIL import Image
        from scipy import ndimage
        img = np.array(Image.open(tmp).convert('HSV'), dtype=int)
        H, S, V = img[:,:,0], img[:,:,1], img[:,:,2]
        # 题目文本(从弹窗读)
        text = js("""
        var d=[...document.querySelectorAll('[class*=modal]')].map(function(e){return e.innerText||'';}).join(' ');
        return String(d);
        """)
        # 提取颜色词 (PIL HSV 范围 0-255!)
        text = js("""
        var d=[...document.querySelectorAll('[class*=modal]')].map(function(e){return e.innerText||'';}).join(' ');
        return String(d);
        """)
        # 颜色词 → PIL HSV 掩码 (H: 0-255, 红≈0/255, 黄≈30, 绿≈85, 蓝≈150)
        color = None
        for c in [("红", (H<=20)|(H>=230)), ("黄", (H>=25)&(H<=60)),
                  ("绿", (H>=70)&(H<=140)), ("蓝", (H>=140)&(H<=190))]:
            if c[0] in str(text):
                color = c[1]
                log(f"[dsc] 目标色: {c[0]}")
                break
        if color is None:
            color = (H>=25)&(H<=60)  # 默认黄色
        mask = color & (S>50) & (V>80)
        lab, n = ndimage.label(mask.astype(np.uint8))
        comps = []
        for i in range(1, n+1):
            ys, xs = np.where(lab == i)
            if len(ys) >= 30:
                comps.append((len(ys), int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())))
        if not comps:
            log("[dsc] 未找到目标色物体")
            return None
        comps.sort(reverse=True)
        # 题目含"最小"选面积最小, 否则选最大(通常唯一)
        if "最小" in str(text):
            comp = min(comps)
        else:
            comp = comps[0]
        cx_img = (comp[1] + comp[2]) / 2
        cy_img = (comp[3] + comp[4]) / 2
        # 映射到页面: css = img坐标 × (w/nw) + 左上角
        scale_x = d["w"] / d["nw"]
        scale_y = d["h"] / d["nh"]
        px = d["x"] + cx_img * scale_x
        py = d["y"] + cy_img * scale_y
        log(f"[dsc] 验证目标: 图片中心({int(cx_img)},{int(cy_img)}) → 页面({int(px)},{int(py)})")
        return (int(px), int(py))
    except Exception as e:
        log("[dsc] OpenCV分析失败:", e)
        return None

def do_login(phone):
    """自动登录: 填手机号 → 破数美验证 → 等短信 → 登录 → 存token"""
    if not phone:
        log("用法: dsc --login <手机号>")
        sys.exit(2)
    log(f"[dsc] 开始登录: {phone[:3]}****{phone[-2:]}")
    # 1. 打开登录页
    run(["navigate", "--url", BASE + "/sign_in"])
    time.sleep(2)
    # 2. 填手机号
    js("""
    var ins=[...document.querySelectorAll('input')];
    var p=ins.find(function(i){return i.placeholder==='请输入手机号';});
    if(!p) return 'NO_INPUT';
    var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    s.call(p,%s); p.dispatchEvent(new Event('input',{bubbles:true}));
    return 'ok';
    """ % json.dumps(phone))
    time.sleep(0.5)
    # 3. 点"发送验证码"
    js("""
    var all=[...document.querySelectorAll('*')];
    for(var i=0;i<all.length;i++){var e=all[i];
      if(e.children.length===0&&(e.textContent||'').trim()==='发送验证码'){e.click();return 'clicked';}}
    return 'NO_BTN';
    """)
    time.sleep(2)
    # 4. 检查是否弹数美验证, 破解之
    sent = False
    for attempt in range(3):
        has = js("""var t=document.body.innerText; return String(t.indexOf('点击图中')>=0);""")
        if str(has).strip().lower() == "true":
            log(f"[dsc] 数美验证出现, OpenCV破解中(第{attempt+1}次)...")
            pt = solve_captcha_with_opencv()
            if pt:
                js("""
                var el=document.elementFromPoint(%d,%d);
                [['mousedown',1],['mouseup',1],['click',1]].forEach(function(ev){
                  var e=new MouseEvent(ev[0],{clientX:%d,clientY:%d,button:0,bubbles:true,cancelable:true,view:window});
                  if(el) el.dispatchEvent(e);
                });
                return 'clicked';
                """ % (pt[0], pt[1], pt[0], pt[1]))
                time.sleep(3)
                # 验证是否通过(弹窗消失 + 出现倒计时)
                ok = js("""var t=document.body.innerText; return String(t.indexOf('秒后可再次获取')>=0);""")
                if str(ok).strip().lower() == "true":
                    log("[dsc] ✅ 验证通过, 短信已发送!")
                    sent = True
                    break
                else:
                    log("[dsc] 验证后未检测到倒计时, 重试...")
            else:
                log("[dsc] 验证图分析失败, 等新题...")
                time.sleep(3)
        else:
            # 无验证 → 可能直接发码了
            ok2 = js("""var t=document.body.innerText; return String(t.indexOf('秒后可再次获取')>=0);""")
            if str(ok2).strip().lower() == "true":
                log("[dsc] ✅ 短信已发送(无验证)")
                sent = True
                break
            time.sleep(2)
    # 5. 异步化: 发码后立即退出, 不阻塞调用方!
    #    用户收到短信后, 单独运行: dsc --verify <验证码> 完成登录
    if not sent:
        log("[dsc] ❌ 短信未发送成功(验证未通过/被风控)。请等待 30s 后重试 dsc --login")
        sys.exit(3)
    log("[dsc] ⏭ 短信已发送。CLI 立即退出(不阻塞调用方)。")
    log("[dsc] 收到验证码后, 请运行: dsc --verify <验证码>")
    log("[dsc] (验证码 5 分钟内有效)")
    return True

def do_verify(code):
    """用短信验证码完成登录(独立命令, 快速执行不阻塞)"""
    if not code:
        log("用法: dsc --verify <短信验证码>")
        sys.exit(2)
    log("[dsc] 使用验证码完成登录...")
    # 确保在登录页
    url = js("return location.href;")
    if "sign_in" not in str(url):
        run(["navigate", "--url", BASE + "/sign_in"])
        time.sleep(2)
    # 1. 填验证码
    js("""
    var ins=[...document.querySelectorAll('input')];
    var c=ins.find(function(i){return i.placeholder==='请输入验证码';});
    if(!c) return 'NO_INPUT';
    var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    s.call(c,%s); c.dispatchEvent(new Event('input',{bubbles:true}));
    return 'ok';
    """ % json.dumps(code))
    time.sleep(0.5)
    # 2. 点登录
    js("""
    var all=[...document.querySelectorAll('div,span,button,[role=button]')];
    for(var i=0;i<all.length;i++){var e=all[i];
      if((e.innerText||'').trim()==='登录'&&e.children.length===0){e.click();return 'clicked';}}
    return 'NO_BTN';
    """)
    time.sleep(4)
    # 3. 检查是否登录成功, 提取token
    url = js("return location.href;")
    if str(url).find("chat.deepseek.com/") >= 0 and "sign_in" not in str(url):
        tok = js("""var t=localStorage.getItem('userToken'); return t? JSON.parse(t).value : '';""")
        if tok:
            with open(TOKEN_FILE, "w") as f:
                f.write(str(tok).strip())
            os.chmod(TOKEN_FILE, 0o600)
            log(f"[dsc] ✅ 登录成功! token已保存 ({len(str(tok).strip())}字符)")
            return True
        else:
            log("[dsc] 登录后未获取到 token")
    else:
        log("[dsc] 登录可能失败, 当前URL:", str(url)[:80])
        err = js("""var t=document.body.innerText; return String(t.slice(-150));""")
        log("[dsc] 页面提示:", str(err)[:150])
    return False

LOCK_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), ".dsv.lock")

def acquire_lock():
    """简单并发锁: 已有 dsc/dsv 在跑则退出, 避免浏览器状态互相干扰
    锁文件记录 PID, 持有者已死(被 SIGTERM/崩溃)则自动接管, 不留死锁"""
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                with open(LOCK_FILE) as f:
                    pid = int(f.read().strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and os.path.exists("/proc/%d" % pid):
                log("[dsc] ⚠️ 另一个 dsc/dsv 进程正在运行, 请等待完成或删除 .dsv.lock")
                return False
            # 持有者已死: 抢占锁
            try:
                os.remove(LOCK_FILE)
            except OSError:
                return False
            log("[dsc] 检测到残留锁(持有者已退出), 已接管")

def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

def main():
    T0 = time.time()
    def lap(msg):
        log(f"[dsc] ⏱ {msg}: {time.time()-T0:.1f}s")
    args = sys.argv[1:]
    keep = "--keep" in args
    args = [a for a in args if a != "--keep"]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--login":
        phone = args[1] if len(args) > 1 else None
        do_login(phone)
        return
    if args[0] == "--verify":
        code = args[1] if len(args) > 1 else None
        if not do_verify(code):
            sys.exit(1)
        return
    if args[0] == "--logout":
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
            log("[dsc] 已删除本地 token")
        return
    # 提示词: 命令行参数拼接; 无参数时尝试读 stdin(管道)
    if args:
        prompt = " ".join(args)
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    else:
        print(__doc__ + "\n用法: dsc <提示词> 或 echo '提示词' | dsc", file=sys.stderr)
        sys.exit(1)
    prompt = prompt.strip()
    if not prompt:
        print("提示词不能为空", file=sys.stderr)
        sys.exit(1)
    locked = False
    try:
        # 参数校验通过后才拿锁(校验失败无需锁, 不会残留 .dsv.lock)
        if not acquire_lock():
            sys.exit(5)
        locked = True
        tok = ensure_login(); lap('登录')
        if not tok:
            # 快速失败: 绝不阻塞调用方等待人工输入!
            log("[dsc] ❌ token 无效/缺失, 无法对话")
            log("[dsc] 请先完成登录(不阻塞): dsc --login <手机号> → 收到短信后 → dsc --verify <验证码>")
            sys.exit(4)
        open_new_chat(); lap('新对话')
        ans = send_and_wait(prompt); lap('回答')
        print(ans)
        # 用后即删(默认删除本次会话, --keep 保留)
        if not keep:
            sid = get_current_session_id()
            if sid:
                if delete_session(sid, tok):
                    log("[dsc] 已删除会话:", sid)
                else:
                    log("[dsc] 会话删除失败(可手动清理):", sid)
            else:
                log("[dsc] 未获取到会话ID,跳过删除")
    finally:
        # 任何路径(含 sys.exit/异常)都必须释放锁, 否则后续调用全被卡死
        if locked:
            release_lock()

# ================= Windows 浏览器驱动 (Playwright/Edge) =================
import sys, os, json, subprocess, time, urllib.request

# ---------- 配置 ----------
EDGE_CANDIDATES = [
    os.environ.get("DSV_EDGE", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
EDGE = next((p for p in EDGE_CANDIDATES if p and os.path.exists(p)), None)
USER_DIR = os.path.expanduser(os.environ.get("DSV_EDGE_PROFILE", "~/.dsv_edge_profile"))
EDGE_LOG = os.path.join(USER_DIR, "edge_err.log")

_PW = None


def _pw():
    """playwright 单例(坑4: 每次 start() 会残留 driver, 同进程重连报 asyncio loop 错误)"""
    global _PW
    if _PW is None:
        from playwright.sync_api import sync_playwright
        _PW = sync_playwright().start()
    return _PW


def _get_port():
    """从 user-data-dir/DevToolsActivePort 读实际 CDP 端口(Chromium 启动时写入, 随机分配)"""
    try:
        with open(os.path.join(USER_DIR, "DevToolsActivePort")) as f:
            return int(f.readline().strip())
    except Exception:
        return None


def _connect():
    port = _get_port()
    if not port:
        raise ConnectionError("no cdp port")
    return _pw().chromium.connect_over_cdp(f"http://127.0.0.1:{port}")


def kill_stale():
    """杀掉占用本 profile 的残留浏览器进程。
    只匹配命令行里含 user-data-dir 的进程, 不影响用户日常 Edge/Chrome。
    注意: PowerShell -like 中反斜杠是字面字符, 不能转义; 但 [ ] 是通配符, 需反引号转义。"""
    # 路径中 [ ] * ? 是 -like 通配符, 用反引号转义; 单引号防注入
    esc = (USER_DIR.replace("[", "`[").replace("]", "`]")
                  .replace("*", "`*").replace("?", "`?").replace("'", "''"))
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*%s*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % esc)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30)
    time.sleep(1.0)


def get_page():
    """连接常驻浏览器, 返回可用页面(优先 chat.deepseek.com 页面, 避免选错标签页)"""
    browser = _connect()
    ctx = browser.contexts[0]
    # 优先当前激活且是 DeepSeek 的页面
    for p in ctx.pages:
        if not p.is_closed() and "chat.deepseek.com" in (p.url or ""):
            return p
    for p in ctx.pages:
        if not p.is_closed() and p.url and p.url != "about:blank":
            return p
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def ensure_browser():
    """确保 Edge 在跑且可连接; 失败则清理残留并重启"""
    global _PW
    if not EDGE:
        print(json.dumps({"error": "未找到 Edge/Chrome。请安装 Edge, 或用环境变量 DSV_EDGE 指定浏览器路径。"}, ensure_ascii=True))
        sys.exit(1)
    try:
        return _connect()
    except Exception:
        # 坑4: 清理失败的 playwright driver, 否则重连报 asyncio loop 错误
        if _PW is not None:
            try:
                _PW.stop()
            except Exception:
                pass
            _PW = None
        kill_stale()
        os.makedirs(os.path.dirname(EDGE_LOG), exist_ok=True)
        # --remote-debugging-port=0: 随机端口, 从 DevToolsActivePort 读实际端口。
        # 避免固定 9222 撞上用户已开调试口的浏览器(连错浏览器=操作真实用户页面)。
        with open(EDGE_LOG, "a", encoding="utf-8") as ef:
            subprocess.Popen([EDGE, "--remote-debugging-port=0",
                              f"--user-data-dir={USER_DIR}", "--headless=new",
                              "--disable-blink-features=AutomationControlled", "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=ef)
        # 轮询 DevToolsActivePort 文件出现(Chromium 启动完成后写入)
        for _ in range(60):
            port = _get_port()
            if port:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                    break
                except Exception:
                    pass
            time.sleep(0.5)
        for _ in range(30):
            try:
                return _connect()
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("浏览器 CDP 连接失败, 详见 %s" % EDGE_LOG)


def win_main():
    # 子进程入口: dsc.py --win-browser <cmd> ... (argv[1] 是 --win-browser, 裁剪掉)
    # 统一 UTF-8 管道, 避免 Windows GBK 解码子进程输出时崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    if len(sys.argv) < 2:
        print(json.dumps({"error": "no cmd"}, ensure_ascii=True))
        return
    cmd = sys.argv[1]
    ensure_browser()
    try:
        if cmd == "navigate":
            url = sys.argv[sys.argv.index("--url") + 1]
            page = get_page()
            page.goto(url, timeout=60000)
            print(json.dumps({"ok": True, "url": page.url}, ensure_ascii=True))
        elif cmd == "execute_js":
            script = sys.argv[sys.argv.index("--script") + 1]
            page = get_page()
            # 坑2: 裸 return 语句包成箭头函数体
            out = page.evaluate("() => { %s }" % script)
            print(json.dumps({"text": str(out)}, ensure_ascii=True))
        else:
            print(json.dumps({"error": "unknown cmd %s" % cmd}, ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"error": "%s: %s" % (cmd, e)}, ensure_ascii=True))


# ================= Linux 浏览器驱动 (Playwright/Chromium) =================
LINUX_USER_DIR = os.path.expanduser(os.environ.get("DSV_CHROME_PROFILE", "~/.dsc_chrome_profile"))
LINUX_LOG = os.path.join(LINUX_USER_DIR, "chrome_err.log")


def _linux_chrome_path():
    env = os.environ.get("DSV_CHROME")
    if env and os.path.exists(env):
        return env
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and os.path.exists(path):
                return path
    except Exception:
        pass
    return None


def _linux_get_port():
    try:
        with open(os.path.join(LINUX_USER_DIR, "DevToolsActivePort")) as f:
            return int(f.readline().strip())
    except Exception:
        return None


def _linux_connect():
    port = _linux_get_port()
    if not port:
        raise ConnectionError("no cdp port")
    return _pw().chromium.connect_over_cdp(f"http://127.0.0.1:{port}")


def kill_linux_stale():
    subprocess.run(["pkill", "-f", LINUX_USER_DIR],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)


def linux_get_page():
    browser = _linux_connect()
    ctx = browser.contexts[0]
    for p in ctx.pages:
        if not p.is_closed() and p.url and p.url != "about:blank":
            return p
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def ensure_linux_browser():
    global _PW
    chrome = _linux_chrome_path()
    if not chrome:
        print(json.dumps({"error": "未找到 Chromium/Chrome。请在 GitHub Actions 中先运行: playwright install chromium"}, ensure_ascii=True))
        sys.exit(1)
    try:
        return _linux_connect()
    except Exception:
        # 坑4: 清理失败的 playwright driver, 否则重连报 asyncio loop 错误
        if _PW is not None:
            try:
                _PW.stop()
            except Exception:
                pass
            _PW = None
        kill_linux_stale()
        os.makedirs(os.path.dirname(LINUX_LOG), exist_ok=True)
        # --remote-debugging-port=0: 随机端口, 从 DevToolsActivePort 读实际端口。
        with open(LINUX_LOG, "a", encoding="utf-8") as lf:
            subprocess.Popen([chrome, "--remote-debugging-port=0",
                              f"--user-data-dir={LINUX_USER_DIR}", "--headless=new",
                              "--no-sandbox", "--disable-dev-shm-usage",
                              "--disable-blink-features=AutomationControlled", "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=lf)
        # 轮询 DevToolsActivePort 文件出现(Chromium 启动完成后写入)
        for _ in range(60):
            port = _linux_get_port()
            if port:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                    break
                except Exception:
                    pass
            time.sleep(0.5)
        for _ in range(30):
            try:
                return _linux_connect()
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("浏览器 CDP 连接失败, 详见 %s" % LINUX_LOG)


def linux_main():
    # 子进程入口: dsc.py --linux-browser <cmd> ... (argv[1] 是 --linux-browser, 裁剪掉)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    if len(sys.argv) < 2:
        print(json.dumps({"error": "no cmd"}, ensure_ascii=True))
        return
    cmd = sys.argv[1]
    ensure_linux_browser()
    try:
        if cmd == "navigate":
            url = sys.argv[sys.argv.index("--url") + 1]
            page = linux_get_page()
            page.goto(url, timeout=60000)
            print(json.dumps({"ok": True, "url": page.url}, ensure_ascii=True))
        elif cmd == "execute_js":
            script = sys.argv[sys.argv.index("--script") + 1]
            page = linux_get_page()
            # 坑2: 裸 return 语句包成箭头函数体
            out = page.evaluate("() => { %s }" % script)
            print(json.dumps({"text": str(out)}, ensure_ascii=True))
        else:
            print(json.dumps({"error": "unknown cmd %s" % cmd}, ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"error": "%s: %s" % (cmd, e)}, ensure_ascii=True))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--win-browser":
        # Windows 浏览器驱动子进程模式(单文件)
        win_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "--linux-browser":
        # Linux/GitHub Actions 浏览器驱动子进程模式
        linux_main()
    else:
        main()
