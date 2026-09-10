#!/usr/bin/env python3
"""
dsc - DeepSeek C端对话CLI (纯文本对话版, 参考 dsv 设计)

已适配 chat.deepseek.com 最新前端 (逆向 commit-id dda740b5; 2026-09-10 对 main.5748b4eb39 真机复测修复)。

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

  登录页当前为「验证码登录 / 密码登录」双 Tab, 短信流程:
    输入手机号/邮箱 -> 点「发送验证码」-> 过数美(国内)/hCaptcha(海外)风控
    -> 输入验证码 -> 点「登录」。
  若风控要求人工过验证, 用 DSC_HEADFUL=1 dsc --login <手机号> 开有头浏览器手动完成。

输出:
  纯文本到 stdout(DeepSeek 的回答),日志到 stderr。

环境变量:
  DSC_TIMEOUT       回答等待上限秒数(默认 300)
  DSC_HEADFUL=1     有头浏览器(人工过风控验证 / 观察过程)
  DSV_DEBUG=1       打印等待轮询细节
  DSV_EDGE          指定 Edge/Chrome 可执行文件路径
  DSV_EDGE_PROFILE  Windows 浏览器 profile 目录(默认 ~/.dsv_edge_profile)
  DSV_CHROME_PROFILE Linux 浏览器 profile 目录(默认 ~/.dsc_chrome_profile)
"""
import sys, os, subprocess, json, time, urllib.request, shutil

# ---------- 配置 ----------
TOKEN_FILE = os.path.expanduser("~/.dsv_token")
BASE = "https://chat.deepseek.com"

# ---- 最新前端 DOM 契约(站点改版时主要改这一段) ----
# 注意: 站点按地区/语言渲染 zh_CN 或 en_US, 故所有文案列表都带中英双语,
#       匹配时按列表顺序「精确 -> 包含」试探, 任一中选即通过。
SEL_MARKDOWN = ".ds-markdown"                    # 用户/助手消息正文容器
SEL_TEXTAREA = "textarea"                        # 输入框实为 <textarea class="ds-textarea__textarea">
PH_CHAT = ["给 DeepSeek 发送消息", "Message DeepSeek"]                  # chatInputPlaceholderChat
PH_ACCOUNT = ["请输入手机号/邮箱地址", "Phone number / email address"]    # signInEmailAndPhonePlaceholder
PH_CODE = ["请输入验证码", "Code"]                                       # inputSmsVerificationCode
BTN_SEND_CODE = ["发送验证码", "Send code"]                              # sendVerificationCode
BTN_LOGIN = ["登录", "Log in"]                                           # signIn
TAB_SMS = ["验证码登录", "Code login"]                                   # signInFormTabBySmsOption
BTN_NEW_CHAT = ["开启新对话", "新对话", "New chat"]                       # 侧栏新建
TXT_STOP = ["停止生成", "Stop"]                                          # chatInputStopButtonTooltip
CODE_SENT = ["秒后可再次获取", "Resend after"]                            # verificationCodeCountDown
# 错误/拒绝提示 -> 快速失败, 不傻等 (中英双语)
ERR_TEXTS = ["违反使用规范", "消息未能发送", "你输入的信息过长", "服务器繁忙",
             "账户已被停用", "内容违规",
             "Message failed to send", "too long", "Server busy",
             "suspended due to violation"]

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


def headful():
    """DSC_HEADFUL=1 -> 有头浏览器, 便于人工过风控验证"""
    return os.environ.get("DSC_HEADFUL", "").strip() not in ("", "0", "false", "False")


def jsl(seq):
    """python list -> JS 数组字面量"""
    return json.dumps(seq, ensure_ascii=False)


def run(args, timeout=120):
    """执行浏览器驱动命令,返回 JSON"""
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

    def clean(s):
        if isinstance(s, str):
            # 去掉 minis-browser-use 附加的 tab_id 尾巴
            # (非空结果: "xxx\n  tab_id: 0"; 空结果: "  tab_id: 0" 无换行前缀, 旧正则漏匹配)
            import re
            s = re.sub(r"\s*tab_id:\s*\d+\s*$", "", s).strip()
        return s

    def extract(v):
        if isinstance(v, dict):
            if "text" in v:
                return clean(v["text"])
            for k in ("data", "value", "result"):
                if k in v:
                    return extract(v[k])
            return v
        return v

    return extract(r)


def js_val(script, default=None):
    """执行 JS 并按 JSON 解析返回值(用于返回数组/对象的探针)"""
    raw = js(script)
    if isinstance(raw, (list, dict)):
        return raw
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


# ---------- 页内 JS 工具库 (注入 driver 的 () => { ... } 内, 只用 var/function) ----------
JS_LIB = """
function __vis(e){ if(!e) return false; var r=e.getBoundingClientRect();
  if(!(r.width>0&&r.height>0)) return false;
  var s=getComputedStyle(e);
  return s.visibility!=='hidden' && s.display!=='none' && s.opacity!=='0'; }

function __findInput(phs){
  var all=[].slice.call(document.querySelectorAll('input,textarea'));
  var i,j,e,ph;
  for(i=0;i<phs.length;i++){
    for(j=0;j<all.length;j++){ e=all[j]; ph=(e.placeholder||'');
      if(__vis(e) && ph===phs[i]) return e; }
  }
  for(i=0;i<phs.length;i++){
    for(j=0;j<all.length;j++){ e=all[j]; ph=(e.placeholder||'');
      if(__vis(e) && ph.indexOf(phs[i])>=0) return e; }
  }
  for(i=0;i<phs.length;i++){
    for(j=0;j<all.length;j++){ e=all[j];
      var al=(e.getAttribute('aria-label')||'');
      if(__vis(e) && al.indexOf(phs[i])>=0) return e; }
  }
  for(j=0;j<all.length;j++){ e=all[j];
    if(e.tagName==='TEXTAREA' && __vis(e)) return e; }
  for(j=0;j<all.length;j++){ e=all[j];
    var t=(e.type||'text').toLowerCase();
    if(e.tagName==='INPUT' && __vis(e) && (t==='text'||t==='tel'||t==='email'||t==='')) return e; }
  return null;
}

function __setValue(e,v){
  var proto=(e.tagName==='TEXTAREA')?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  var setter=Object.getOwnPropertyDescriptor(proto,'value').set;
  setter.call(e,v);
  e.dispatchEvent(new Event('input',{bubbles:true}));
  e.dispatchEvent(new Event('change',{bubbles:true}));
  return true;
}

function __clickText(list, exact){
  var all=[].slice.call(document.querySelectorAll('button,div,span,a,label,[role=button]'));
  var i,j,e,t;
  for(i=0;i<list.length;i++){
    for(j=0;j<all.length;j++){
      e=all[j];
      if(e.children.length>2) continue;
      t=(e.innerText||e.textContent||'').trim();
      if(!t || !__vis(e)) continue;
      if(exact ? (t===list[i]) : (t.indexOf(list[i])>=0)){ e.click(); return list[i]; }
    }
  }
  return null;
}

function __hasText(list){
  var b=document.body?(document.body.innerText||''):'';
  var i;
  for(i=0;i<list.length;i++){ if(b.indexOf(list[i])>=0) return list[i]; }
  return null;
}
"""


def get_token():
    """读取持久化 token 并调 /api/v0/users/current 校验有效性。

    最新接口统一外壳 {"code":0,"msg":"","data":{...}}, 顶层码 40002 = Missing Token;
    旧实现误判为 data.biz_code, 导致所有 token 一律失效。
    """
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        t = f.read().strip()
    if not t:
        return None
    try:
        req = urllib.request.Request(BASE + "/api/v0/users/current",
            headers={"Authorization": "Bearer " + t, "accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        if d.get("code") == 0:
            return t
        log("[dsc] token 校验未通过: code=%s msg=%s" % (d.get("code"), d.get("msg")))
    except Exception as e:
        log("[dsc] token验证异常:", e)
    return None


def ensure_login():
    """确保浏览器处于登录态,注入token"""
    tok = get_token()
    if not tok:
        return None
    run(["navigate", "--url", BASE + "/"])
    time.sleep(1.0)
    # localStorage['userToken'] 是 {"value": token, "__version": "0"} JSON 包装
    # (站点 storage 层读取时 JSON.parse(...).value, 解析失败回退 null;
    #  实测存裸字符串会永远卡在 /sign_in —— 2026-09-10 实测验证)
    js("localStorage.setItem('userToken', JSON.stringify({value: %s, __version: '0'})); location.reload();" % json.dumps(tok))
    time.sleep(1.5)
    return tok


def open_new_chat():
    """开新对话(每次对话独立会话, 便于用后即删)"""
    # 若当前已是空白新对话(无历史消息)则直接复用, 省一次navigate
    cur = js("""
    var marks=document.querySelectorAll(%s);
    return String(marks.length === 0);
    """ % json.dumps(SEL_MARKDOWN))
    if str(cur).strip().lower() == "true":
        return
    run(["navigate", "--url", BASE + "/"])
    time.sleep(1.5)
    js(JS_LIB + """
    var hit=__clickText(%s, true);
    return hit ? ('clicked:'+hit) : 'no_btn';
    """ % jsl(BTN_NEW_CHAT))
    time.sleep(1.0)


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


def _err_text():
    """返回命中的错误提示文案, 无错误返回 None"""
    return js(JS_LIB + """
    return String(__hasText(%s) || '');
    """ % jsl(ERR_TEXTS))


def _type_into(placeholder_list, text):
    """把 text 填进指定 placeholder 的输入框, 返回 ok/no_input"""
    return js(JS_LIB + """
    var e=__findInput(%s);
    if(!e) return 'no_input';
    __setValue(e, %s);
    return 'ok';
    """ % (jsl(placeholder_list), json.dumps(text)))


def _last_answer():
    """取最后一条消息文本"""
    return js("""
    var m=document.querySelectorAll(%s);
    return m.length ? m[m.length-1].innerText : '';
    """ % json.dumps(SEL_MARKDOWN))


def send_and_wait(prompt, timeout=None):
    """输入问题,发送(等发送成功),轮询回答直到稳定"""
    if timeout is None:
        timeout = int(os.environ.get("DSC_TIMEOUT", "300"))

    # 发送前采样基线(数量+文本): 必须在发送前采, 否则与快速回复竞态
    base = js_val("""
    var m=document.querySelectorAll(%s);
    return JSON.stringify({n:m.length, last:m.length? m[m.length-1].innerText : ''});
    """ % json.dumps(SEL_MARKDOWN), default={"n": 0, "last": ""})
    base_n = int(base.get("n", 0)) if isinstance(base, dict) else 0
    base_txt = base.get("last", "") if isinstance(base, dict) else ""
    log(f"[dsc] baseline(发送前): n={base_n} len={len(base_txt)}")

    # ---- 输入提示词 ----
    r = js(JS_LIB + """
    var ta=__findInput(%s);
    if(!ta) return 'no_ta';
    __setValue(ta, %s);
    return 'ok';
    """ % (jsl(PH_CHAT), json.dumps(prompt)))
    if str(r).strip() != "ok":
        return "(未找到输入框: 站点改版或页面未加载完成 - 检查 README 选择器契约)"
    time.sleep(0.5)

    # ---- 点发送 ----
    # 实测(2026-09-10, main.5748b4eb39): 真正的发送键 = 输入行最右的圆形主钮,
    # class token 精确含 "ds-button--primary"。旧「最小面积」结构法会被按钮内部的
    # 图标层(ds-button__icon / ds-button__background, 面积更小)截胡, 点到隔壁胶囊上,
    # 表现为 textarea 不清空、消息根本没发出去、等回答超时。
    # 修复: ① 排除按钮内部嵌套层(有 ds-button 祖先) ② 优先精确 token ds-button--primary
    r = js(JS_LIB + r"""
    var ta=__findInput(%s);
    if(!ta) return 'no_ta';
    var tr=ta.getBoundingClientRect();
    function __inBtn(e){
      var p=e.parentElement, d=0;
      while(p && d<10){
        if((''+(p.getAttribute('class')||'')).indexOf('ds-button')>=0) return true;
        p=p.parentElement; d++;
      }
      return false;
    }
    var cands=[].slice.call(document.querySelectorAll('button,[role=button],div,span,a'));
    var primary=null, best=null, bestScore=1e9;
    var i,e,cl,r2,s;
    for(i=0;i<cands.length;i++){
      e=cands[i];
      cl=(''+(e.getAttribute('class')||''));
      if(cl.indexOf('button')<0) continue;          // 只看按钮类组件
      if(__inBtn(e)) continue;                       // 排除按钮内部嵌套层(图标/背景)
      if(!__vis(e)) continue;
      if(e.disabled===true || e.getAttribute('aria-disabled')==='true') continue;
      r2=e.getBoundingClientRect();
      // 与输入框同区: 垂直中心相距不超过 3 倍输入框高度
      if(Math.abs((r2.y+r2.height/2)-(tr.y+tr.height/2)) > Math.max(tr.height*3, 160)) continue;
      if((' '+cl+' ').indexOf(' ds-button--primary ')>=0) primary=e;  // 精确 class token
      s=(r2.width*r2.height) + Math.abs(r2.x-tr.x)*0.4;
      if(s<bestScore){ bestScore=s; best=e; }
    }
    var hit=primary || best;
    if(!hit) return 'no_send';
    hit.click();
    return primary ? 'sent(primary)' : 'sent(structural)';
    """ % jsl(PH_CHAT))
    log("[dsc] 发送:", r)

    # ---- 等发送成功: textarea 被清空 ----
    sent_ok = wait_for("""
    var ta=document.querySelector(%s);
    var filled=ta ? (ta.value||'').length>0 : false;
    return String(!ta || !filled);
    """ % json.dumps(SEL_TEXTAREA), timeout=10, interval=0.8, desc="textarea清空(发送成功)")
    if not sent_ok:
        # 兜底: 用 Enter 键发送(站点 Enter 提交, Shift+Enter 换行)
        log("[dsc] 按钮发送疑似失败,尝试 Enter")
        js("""
        var ta=document.querySelector(%s);
        if(!ta) return 'no_ta';
        ta.focus();
        ['keydown','keypress','keyup'].forEach(function(k){
          ta.dispatchEvent(new KeyboardEvent(k,{key:'Enter',code:'Enter',
            keyCode:13,which:13,bubbles:true,cancelable:true,composed:true}));
        });
        return 'enter_sent';
        """ % json.dumps(SEL_TEXTAREA))
        wait_for("""
        var ta=document.querySelector(%s);
        return String(!ta || (ta.value||'').length===0);
        """ % json.dumps(SEL_TEXTAREA), timeout=10, interval=0.8, desc="Enter发送后清空")

    # ---- 发送后立即检测错误(违规/被拒/过长时快速失败) ----
    time.sleep(2)
    err = _err_text()
    if err:
        return "(DeepSeek 拒绝处理: %s - 换个提示词或稍后重试)" % err

    # ---- 等回答开始出现: 消息条数增加 / 最后一条变化 / 出现停止按钮 ----
    poll_js = JS_LIB + """
    var m=document.querySelectorAll(%s);
    var last=m.length? m[m.length-1].innerText : '';
    var stop=!!__hasText(%s);
    return String((m.length > %d) || (last !== %s) || stop);
    """ % (json.dumps(SEL_MARKDOWN), jsl(TXT_STOP), base_n, json.dumps(base_txt))
    appeared = wait_for(poll_js, timeout=timeout, interval=1, desc="回答开始出现")
    if not appeared:
        cur = _last_answer()
        return str(cur) if cur else "(超时未获取回答)"

    # ---- 等回答稳定: 文本连续 2 次不变即视为完成 ----
    start = time.time()
    last_len, stable_count, last_text = -1, 0, ""
    while time.time() - start < timeout:
        err = _err_text()
        if err:
            return "(DeepSeek 拒绝处理: %s - 换个提示词或稍后重试)" % err
        cur = _last_answer()
        s = str(cur)
        l = len(s)
        if l > 0 and l == last_len and s == last_text:
            stable_count += 1
            if stable_count >= 2:      # 连续 2 次(~1s)不变 -> 完成
                return s
        elif l > 0:
            last_len, last_text, stable_count = l, s, 0
        time.sleep(0.5)
    return last_text if last_text else "(超时未获取回答)"


def delete_session(session_id, tok):
    """删除指定会话(用后即删,避免会话列表混乱)。

    最新接口改为批量子段: {"chat_session_ids": [...]} (旧版单数 chat_session_id 已失效)
    """
    if not session_id:
        return False
    try:
        req = urllib.request.Request(BASE + "/api/v0/chat_session/delete",
            data=json.dumps({"chat_session_ids": [session_id]}).encode(),
            headers={"Authorization": "Bearer " + tok, "accept": "*/*",
                     "content-type": "application/json",
                     "referer": BASE + "/",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        # 统一外壳: 成功时 code==0; 业务码在 data.biz_code
        ok = (d.get("code") == 0)
        if ok and isinstance(d.get("data"), dict) and "biz_code" in d["data"]:
            ok = (d["data"]["biz_code"] == 0)
        return ok
    except Exception as e:
        log("[dsc] 删除会话失败:", e)
        return False


def get_current_session_id():
    """从当前页面 URL 提取 chat_session_id。

    路由为 /a/:agentId/s/:sessionId (agentId 通常为 chat),
    故 /a/chat/s/<uuid>。放宽匹配: 不写死 36 位。
    """
    r = js("return location.href;")
    url = str(r) if r else ""
    import re
    m = re.search(r"/a/[^/]+/s/([0-9a-zA-Z_-]+)", url)
    if m:
        return m.group(1)
    log("[dsc] 未识别到会话URL:", url[:100])
    return None

def _click_tab_sms():
    """登录页有「验证码登录 / 密码登录」双 Tab, 先确保切到验证码登录。"""
    r = js(JS_LIB + """
    var hit=__clickText(%s, false);
    return hit ? ('tab:'+hit) : 'no_tab';
    """ % jsl(TAB_SMS))
    log("[dsc] 切Tab:", r)
    return str(r).startswith("tab:")


def _detect_captcha():
    """探测风控验证组件是否出现。

    最新站点: 国内走数美 v1.0.4 (smcp.min.js, 包装类 ds-shumei-captcha-*),
    海外走 hCaptcha。旧版「点击图中XX」图片点选题型已从 bundle 移除,
    故不再做 OpenCV 解题, 只探测并交给人工/重试。
    """
    return js_val(JS_LIB + """
    var out={shumei:false,iframe:false,modal:false,text:''};
    out.shumei = !!document.querySelector('[class*=ds-shumei-captcha]');
    out.modal  = !!document.querySelector('[class*=captcha-modal],[class*=captcha-widget]');
    var fr=document.querySelectorAll('iframe');
    for(var i=0;i<fr.length;i++){
      var s=(fr[i].src||'');
      if(/hcaptcha|captcha|fengkong|shumei/i.test(s)) out.iframe=true;
    }
    out.text = (document.body?(document.body.innerText||''):'').slice(-300);
    return JSON.stringify(out);
    """, default={})


def do_login(phone):
    """登录第一步: 填手机号/邮箱 → 点「发送验证码」→ 等待短信发出后立即退出。

    异步设计(与 dsv 一致): 发码成功后 CLI 立即退出, 不阻塞调用方;
    收到短信后单独运行 `dsc --verify <验证码>` 完成登录。
    """
    if not phone:
        log("用法: dsc --login <手机号>")
        sys.exit(2)
    log(f"[dsc] 开始登录: {phone[:3]}****{phone[-2:]}")
    # 1. 打开登录页(注意: 外壳对 / 与 /sign_in 返回同一 HTML, 仍用 /sign_in 语义更明确)
    run(["navigate", "--url", BASE + "/sign_in"])
    time.sleep(2)
    # 2. 切到「验证码登录」Tab
    _click_tab_sms()
    time.sleep(0.5)
    # 3. 填手机号/邮箱
    r = _type_into(PH_ACCOUNT, phone)
    if str(r).strip() != "ok":
        log("[dsc] ❌ 未找到手机号/邮箱输入框(placeholder 契约: %s)" % PH_ACCOUNT[0])
        log("[dsc] 若站点已改版, 请更新 dsc.py 顶部的 PH_ACCOUNT")
        sys.exit(3)
    time.sleep(0.5)
    # 4. 点「发送验证码」
    r = js(JS_LIB + """
    var hit=__clickText(%s, false);
    return hit ? ('clicked:'+hit) : 'NO_BTN';
    """ % jsl(BTN_SEND_CODE))
    log("[dsc] 点发送验证码:", r)
    time.sleep(2)

    # 5. 处理风控验证(数美/ hCaptcha) + 等发码成功
    sent = False
    for attempt in range(3):
        cap = _detect_captcha()
        if isinstance(cap, dict) and (cap.get("shumei") or cap.get("modal") or cap.get("iframe")):
            log(f"[dsc] 检测到风控验证组件(第{attempt+1}次): "
                f"shumei={cap.get('shumei')} modal={cap.get('modal')} iframe={cap.get('iframe')}")
            # 尽力交互一次: 数美滑块/点选有时单击即可通过
            pt = js(JS_LIB + """
            var box=document.querySelector('[class*=ds-shumei-captcha-main],[class*=captcha-widget],[class*=captcha-modal]');
            if(!box) return 'no_box';
            var b=box.getBoundingClientRect();
            var cx=Math.round(b.x+b.width/2), cy=Math.round(b.y+b.height/2);
            var el=document.elementFromPoint(cx,cy) || box;
            [['mousedown',1],['mouseup',1],['click',1]].forEach(function(ev){
              var e=new MouseEvent(ev[0],{clientX:cx,clientY:cy,button:0,bubbles:true,cancelable:true,view:window});
              el.dispatchEvent(e);
            });
            return 'clicked:'+cx+','+cy;
            """)
            log("[dsc] 风控交互尝试:", pt)
            time.sleep(3)
        # 发码成功标志: 出现倒计时「N 秒后可再次获取」
        ok = js(JS_LIB + """
        return String(!!__hasText(%s));
        """ % jsl(CODE_SENT))
        if str(ok).strip().lower() == "true":
            log("[dsc] ✅ 短信已发送(检测到倒计时)")
            sent = True
            break
        # 风控未过时, 某些情况仍会直接发码, 也检查是否有错误提示
        err = _err_text()
        if err:
            log("[dsc] 页面提示:", err)
        time.sleep(2)

    if not sent:
        log("[dsc] ❌ 短信未发送成功(风控未通过或触发频率限制)。")
        if not headful():
            log("[dsc] 提示: 用 DSC_HEADFUL=1 dsc --login <手机号> 打开有头浏览器, 人工完成风控验证后重试。")
        log("[dsc] 请等待 30s 后重试。")
        sys.exit(3)
    log("[dsc] ⏭ 短信已发送。CLI 立即退出(不阻塞调用方)。")
    log("[dsc] 收到验证码后, 请运行: dsc --verify <验证码>")
    log("[dsc] (验证码 5 分钟内有效)")
    return True


def do_verify(code):
    """登录第二步: 用短信验证码完成登录并持久化 token(独立命令, 快速执行不阻塞)"""
    if not code:
        log("用法: dsc --verify <短信验证码>")
        sys.exit(2)
    log("[dsc] 使用验证码完成登录...")
    # 确保在登录页
    url = js("return location.href;")
    if "sign_in" not in str(url):
        run(["navigate", "--url", BASE + "/sign_in"])
        time.sleep(2)
        _click_tab_sms()
        time.sleep(0.5)
    # 1. 填验证码
    r = _type_into(PH_CODE, code)
    if str(r).strip() != "ok":
        log("[dsc] ❌ 未找到验证码输入框(placeholder 契约: %s)" % PH_CODE[0])
        log("[dsc] 若站点已改版, 请更新 dsc.py 顶部的 PH_CODE")
        return False
    time.sleep(0.5)
    # 2. 点「登录」
    r = js(JS_LIB + """
    var hit=__clickText(%s, true);
    return hit ? ('clicked:'+hit) : 'NO_BTN';
    """ % jsl(BTN_LOGIN))
    log("[dsc] 点登录:", r)
    time.sleep(4)
    # 3. 提取 token 并存盘
    #    最新前端 localStorage['userToken'] 为裸 token 字符串
    tok = js("""
    var t=localStorage.getItem('userToken');
    if(!t) return '';
    try { var o=JSON.parse(t); if(o && typeof o==='object' && o.value) return String(o.value); } catch(e){}
    return String(t);
    """)
    tok = str(tok).strip() if tok else ""
    if tok and len(tok) > 20:
        with open(TOKEN_FILE, "w") as f:
            f.write(tok)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
        # 4. 顺带校验一次, 确认真的可用
        if get_token():
            log(f"[dsc] ✅ 登录成功! token 已保存并校验通过 ({len(tok)} 字符)")
            return True
        log("[dsc] ⚠️ token 已保存, 但接口校验未通过(可能风控/需要重登)")
        return True
    # 失败: 输出页面提示便于定位
    cur = js("return location.href;")
    log("[dsc] 登录可能失败, 当前URL:", str(cur)[:100])
    tail = js("var t=document.body.innerText; return String(t.slice(-200));")
    log("[dsc] 页面提示:", str(tail)[:200])
    return False


# ---------- 并发锁 ----------
LOCK_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), ".dsv.lock")


def acquire_lock():
    """简单并发锁: 已有 dsc/dsv 在跑则退出, 避免浏览器状态互相干扰。
    锁文件记录 PID, 持有者已死(被 SIGTERM/崩溃)则自动接管, 不留死锁。
    注意: Windows 没有 /proc, 用 tasklist 判定存活。
    """
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
            if pid and _pid_alive(pid):
                log("[dsc] ⚠️ 另一个 dsc/dsv 进程正在运行, 请等待完成或删除 .dsv.lock")
                return False
            try:
                os.remove(LOCK_FILE)
            except OSError:
                return False
            log("[dsc] 检测到残留锁(持有者已退出), 已接管")


def _pid_alive(pid):
    """跨平台判断 PID 是否存活"""
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
    head = "--headful" in args
    args = [a for a in args if a not in ("--keep", "--headful")]
    if head:
        os.environ["DSC_HEADFUL"] = "1"
    # 注意: 不能写成 "if not args 就显示帮助" —— 无参数时应优先尝试读管道 stdin
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return
    sub = args[0] if args else None
    if sub == "--login":
        phone = args[1] if len(args) > 1 else None
        do_login(phone)
        return
    if sub == "--verify":
        code = args[1] if len(args) > 1 else None
        if not do_verify(code):
            sys.exit(1)
        return
    if sub == "--logout":
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
            log("[dsc] 已删除本地 token")
        else:
            log("[dsc] 本地无 token")
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
        tok = ensure_login()
        lap('登录')
        if not tok:
            # 快速失败: 绝不阻塞调用方等待人工输入!
            log("[dsc] ❌ token 无效/缺失, 无法对话")
            log("[dsc] 请先完成登录(不阻塞): dsc --login <手机号> → 收到短信后 → dsc --verify <验证码>")
            sys.exit(4)
        open_new_chat()
        lap('新对话')
        ans = send_and_wait(prompt)
        lap('回答')
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

    只匹配命令行里含 DSV_EDGE_PROFILE 路径的进程, 不影响用户日常 Edge/Chrome
    (用户 profile 是 %LOCALAPPDATA%\\Microsoft\\Edge\\User Data, 与 ~/.dsv_edge_profile 不同)。
    注意: PowerShell -like 中反斜杠是字面字符, 不能转义; 但 [ ] * ? 是通配符, 需反引号转义。
    """
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
        # DSC_HEADFUL=1 时不加 --headless, 便于人工过风控验证。
        flags = [EDGE, "--remote-debugging-port=0",
                 f"--user-data-dir={USER_DIR}",
                 "--disable-blink-features=AutomationControlled",
                 "--no-first-run", "--no-default-browser-check"]
        if not headful():
            flags.append("--headless=new")
        flags.append("about:blank")
        with open(EDGE_LOG, "a", encoding="utf-8") as ef:
            subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=ef)
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
        if not p.is_closed() and "chat.deepseek.com" in (p.url or ""):
            return p
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
        flags = [chrome, "--remote-debugging-port=0",
                 f"--user-data-dir={LINUX_USER_DIR}",
                 "--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-blink-features=AutomationControlled",
                 "--no-first-run", "--no-default-browser-check"]
        if not headful():
            flags.append("--headless=new")
        flags.append("about:blank")
        with open(LINUX_LOG, "a", encoding="utf-8") as lf:
            subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=lf)
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
