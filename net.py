import configparser
import os
import re
import sys
import traceback
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

PORTAL_URL = "http://1.1.1.1"  # 校园网登录页
LOGIN_URL = "http://1.1.1.1:801/eportal/"  # eportal 登录接口
TIMEOUT = 5  # 请求超时(秒)

BAD_IPS = {"", "null", "0.0.0.0", "000.000.000.000.", "127.0.0.1"}  # 解析 IP 时视为无效的值

# ---------------- 校园网识别 ----------------
# 校内访问 http://1.1.1.1 拿到的一定是 Dr.COM AC 的门户页面：
#   未登录 -> 登录页(a70.htm，页面标识 Dr.COMWebLoginID_0)
#   已登录 -> 注销页(页面标识 Dr.COMWebLoginID_1，并带 uid / v4ip 等在线信息)
# Server 头是 DrcomServer1.0 / OMPXY。校外访问同一个地址拿到的是 Cloudflare 的公共 DNS
# 官网，或者干脆连不通。所以判断规则只有一条：
#   能拿到带门户特征的页面 = 在本校校园网内，接着再判断登录状态；
#   拿不到、或页面没有门户特征 = 非本校校园网，直接提示并正常退出。
CAMPUS_MARKERS = (
    "dr.com",          # Dr.COM 门户，页面标识里就带这个前缀
    "comwebloginid_",  # 门户页面标识：Dr.COMWebLoginID_0/_1/_3
    "eportal",
    "acsetting",
    "a70.htm",
    "wlanuserip",
    "drcomserver",     # Server: DrcomServer1.0
    "ompxy",           # Server: OMPXY/1.4.7
)

# 门户登录页特征：出现就说明还没登录
LOGIN_PAGE_MARKERS = (
    "comwebloginid_0",  # 登录页标识
    "a70.htm",          # 登录页文件名
)

# 门户注销页特征：出现就说明已经登录；注销页里还带着当前在线账号 uid='学号@运营商'
ONLINE_PAGE_MARKERS = (
    "comwebloginid_1",  # 注销页标识
    "注销页",
)
ONLINE_UID_RE = re.compile(r"uid\s*=\s*'[^']*@", re.I)

# 公网 1.1.1.1 是 Cloudflare 的服务，只在提示里用来解释“为什么判断为非校园网”
CLOUDFLARE_MARKERS = (
    "cloudflare",
    "one.one.one.one",
    "1.1.1.1 for families",
)

# ---------------- 外部配置 ----------------
# 账号信息不再写死在代码里：首次运行会在 exe / 脚本同目录生成 net.ini，填好后即可用。
# 改 net.ini 后直接重新运行即可生效，不需要重新打包。
CONFIG_NAME = "net.ini"  # 配置文件默认名字（与 exe 同目录）
CONFIG_ENV = "NET_CONFIG"  # 也可用该环境变量直接指定配置文件路径

CONFIG_TEMPLATE = """# 校园网自动登录配置文件（UTF-8 编码）
# 放在 net.exe 同一目录下即可生效，改完直接重新运行，不用重新打包。
# 注意：密码是明文保存的，不要把本文件外传。

[account]
# 学号 / 账号
user = 
# 密码
password = 
# 运营商：电信 telecom，联通 unicom，移动 cmcc，校园用户 xyw
operator = 
# 本机 IP：留空则自动访问 http://1.1.1.1 获取；多网卡/虚拟机时可手动填写
ip =
"""

KNOWN_OPERATORS = ("telecom", "unicom", "cmcc", "xyw")  # 常见值，仅用于提示，不做强制校验


class ConfigError(Exception):
    """配置文件缺失、格式错误或内容不合法。"""


class OffCampusError(Exception):
    """当前不在本校校园网内：http://1.1.1.1 给的是公网页面，或者门户根本不可达。"""


def app_dir():
    """
    程序自身所在目录。

    - 打包成 exe 后不能用 __file__：onefile 模式下它指向临时解包目录，
      每次运行路径都不一样，必须用 sys.executable（exe 文件本身）；
    - 直接运行 net.py 时用脚本所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def parse_argv(argv):
    """解析 --config 路径 / --config=路径 / -c 路径，返回配置文件路径或 None。"""
    for i, arg in enumerate(argv):
        if arg in ("--config", "-c"):
            if i + 1 >= len(argv):
                raise ConfigError(f"{arg} 后面需要跟配置文件路径")
            return argv[i + 1]
        if arg.startswith("--config=") or arg.startswith("-c="):
            return arg.split("=", 1)[1]
    return None


def find_config(explicit=None):
    """
    按优先级查找已经存在的配置文件，返回其路径；都没找到返回 None：

    1. 命令行 --config 指定的路径
    2. 环境变量 NET_CONFIG 指定的路径
    3. exe / 脚本同目录下的 net.ini  ← 最常用，双击运行就走这里
    4. 当前工作目录下的 net.ini（例如快捷方式设了“起始位置”）
    """
    if explicit:
        return explicit if os.path.isfile(explicit) else None

    candidates = []
    env_path = os.environ.get(CONFIG_ENV)
    if env_path:
        candidates.append(env_path)
    base = app_dir()
    candidates.append(os.path.join(base, CONFIG_NAME))
    cwd = os.getcwd()
    if os.path.normcase(cwd) != os.path.normcase(base):
        candidates.append(os.path.join(cwd, CONFIG_NAME))

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def write_template(path):
    """写出一份带注释的配置文件模板。"""
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(CONFIG_TEMPLATE)
    return path


def read_account(path):
    """读取 [account] 段，返回 (user, password, operator, ip)。"""
    parser = configparser.ConfigParser()
    try:
        # utf-8-sig：兼容记事本“UTF-8 带 BOM”和普通 UTF-8
        with open(path, encoding="utf-8-sig") as f:
            parser.read_file(f)
    except UnicodeDecodeError as e:
        raise ConfigError(f"配置文件不是 UTF-8 编码，请用记事本另存为 UTF-8: {e}")
    except configparser.Error as e:
        raise ConfigError(f"配置文件格式有误: {e}")
    except OSError as e:
        raise ConfigError(f"无法读取配置文件: {e}")

    section = next((s for s in parser.sections() if s.strip().lower() == "account"), None)
    if section is None:
        raise ConfigError("配置文件里找不到 [account] 段")

    def get(key):
        return (parser.get(section, key, fallback="") or "").strip()

    user = get("user")
    password = get("password")
    operator = get("operator") or "cmcc"
    ip = get("ip")

    if not user:
        raise ConfigError("[account] 的 user 不能为空")
    if not password:
        raise ConfigError("[account] 的 password 不能为空")
    if operator not in KNOWN_OPERATORS:
        print(f"提示: operator={operator} 不在常见值 {KNOWN_OPERATORS} 中，将按原样提交。")

    return user, password, operator, ip


def pause_if_console():
    """
    停下来等用户按回车，让双击运行的人有时间看清结果。

    只有在“真有控制台”时才等：双击 / 在终端里手动运行会暂停；
    计划任务、把输出重定向到文件（isatty 为 False）不会暂停，避免卡住出错。
    """
    try:
        if sys.stdin and sys.stdin.isatty() and sys.stdout.isatty():
            input("\n按回车键退出...")
    except (EOFError, OSError):
        pass


def decode_body(body, content_type=""):
    """门户页面是 gbk/gb2312，按响应头声明的编码解码。"""
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    charset = m.group(1) if m else "gbk"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class PortalResponse:
    """一次请求的结果：最终地址、响应文本、响应头（识别门户要用到后两项以外的信息）。"""

    __slots__ = ("url", "text", "headers")

    def __init__(self, url="", text="", headers=None):
        self.url = url or ""
        self.text = text or ""
        self.headers = headers or {}


def request(url, data=None, timeout=TIMEOUT):
    """GET/POST 请求，返回 PortalResponse。会自动跟随门户的重定向。"""
    headers = {"User-Agent": "Mozilla/5.0"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 跟随重定向后，geturl() 是最终地址（如 1.1.1.1/a70.htm?wlanuserip=...）
            text = decode_body(resp.read(), resp.headers.get("Content-type"))
            return PortalResponse(resp.geturl(), text, resp.headers)
    except HTTPError as e:
        text = decode_body(e.read(), e.headers.get("Content-type"))
        return PortalResponse(e.geturl(), text, e.headers)


def parse_ip(text, final_url=""):
    """
    从访问登录页的结果中取本机 IP，依次尝试：

    1. 跳转后的 URL 参数（被 AC 拦截时就是这个形式）：
       http://1.1.1.1/a70.htm?wlanuserip=10.133.133.163&wlanacip=null&...&ip=10.133.133.163
    2. 页面正文里以文本出现的 wlanuserip=10.133.133.163
    3. 页面 JS 变量（直连门户、没有跳转时用这个）：
       登录页(a70.htm) v46ip='10.133.133.163'；注销页 v4ip='10.133.133.163'
    """
    query = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
    for key in ("wlanuserip", "userip", "ip"):
        for value in query.get(key, []):
            if value not in BAD_IPS:
                return value

    for value in re.findall(r"wlanuserip=(\d{1,3}(?:\.\d{1,3}){3})", text):
        if value not in BAD_IPS:
            return value

    for value in re.findall(r"""\b(?:v46ip|v4ip)\s*=\s*['"](\d{1,3}(?:\.\d{1,3}){3})['"]""", text):
        if value not in BAD_IPS:
            return value

    return ""


def parse_ac_params(final_url=""):
    """跳转 URL 里带的 wlanacip / wlanacname（直连门户时没有这些参数）。"""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
    return {key: (query.get(key) or [""])[0] for key in ("wlanacip", "wlanacname", "mac")}


def get_local_ip():
    """访问登录页 http://1.1.1.1，从中解析出本机 IP。"""
    resp = request(PORTAL_URL)
    return parse_ip(resp.text, resp.url)


def _marker_hit(resp, markers):
    """响应正文 / 最终地址 / Server 头里是否出现给定特征（忽略大小写）。"""
    blob = "\n".join((resp.text, resp.url, resp.headers.get("Server") or "")).lower()
    return any(marker in blob for marker in markers)


def looks_like_campus_portal(resp):
    """响应是不是本校校园网门户（Dr.COM AC）的页面。"""
    return _marker_hit(resp, CAMPUS_MARKERS)


def looks_like_public_cloudflare(resp):
    """响应是不是公网 1.1.1.1（Cloudflare）的页面，只用于把提示原因写清楚。"""
    return _marker_hit(resp, CLOUDFLARE_MARKERS)


def portal_logged_in(resp):
    """
    门户页面是否表示“已经登录”。

    已登录时门户给的是注销页（Dr.COMWebLoginID_1，带 uid='学号@运营商'）；
    未登录时给的是登录页（Dr.COMWebLoginID_0 / a70.htm）。
    两者都认不出来时按“未登录”处理：多登录一次没有副作用，漏登录会一直上不了网。
    """
    if _marker_hit(resp, LOGIN_PAGE_MARKERS):
        return False
    return _marker_hit(resp, ONLINE_PAGE_MARKERS) or bool(ONLINE_UID_RE.search(resp.text))


def check():
    """
    检查校园网状态，返回 (status, resp)：

    - "login" ：在本校校园网内，且未登录（resp 可继续用来解析本机 IP）
    - "online"：在本校校园网内，且已登录

    非本校校园网（1.1.1.1 连不上，或拿到的页面没有本校门户特征）时抛 OffCampusError，
    由 main() 提示“位于非本校校园网”并正常退出。
    """
    try:
        resp = request(PORTAL_URL)
    except (URLError, OSError, ValueError) as e:
        raise OffCampusError(f"无法访问校园网登录页 {PORTAL_URL}（{e}）")

    if not looks_like_campus_portal(resp):
        if looks_like_public_cloudflare(resp):
            reason = f"{PORTAL_URL} 返回的是公网 Cloudflare 页面（{resp.url}），不是本校门户页面"
        else:
            reason = f"{PORTAL_URL} 没有返回本校门户页面（{resp.url}）"
        raise OffCampusError(reason)

    return ("online" if portal_logged_in(resp) else "login"), resp


def build_login_url(ip, ac_params=None):
    """拼接 eportal 登录 URL；wlanacip/wlanacname 优先用跳转 URL 里的真实值。"""
    ac_params = ac_params or {}
    query = {
        "c": "ACSetting",
        "a": "Login",
        "protocol": "http:",
        "hostname": "1.1.1.1",
        "iTermType": "1",
        "wlanuserip": ip,
        "wlanacip": ac_params.get("wlanacip") or "null",
        "wlanacname": ac_params.get("wlanacname") or "null",
        "mac": ac_params.get("mac") or "00-00-00-00-00-00",
        "ip": ip,
        "enAdvert": "0",
        "queryACIP": "0",
        "loginMethod": "1",
    }
    # safe=":" 保证 protocol=http: 不被转义
    return LOGIN_URL + "?" + urllib.parse.urlencode(query, safe=":")


def login(user, password, operator, ip="", portal=None):
    """
    执行登录。

    portal 是 check() 已经取到的门户响应：未登录时门户会返回登录页，
    里面带着 wlanuserip 等参数，直接复用可以少发一次请求。
    """
    if portal is None:  # 单独调用 login() 时的兜底：自己先确认一遍校园网状态
        status, portal = check()
        if status == "online":
            print("已是登录状态！")
            return

    # ip 优先用配置里的值（多网卡/虚拟机时可从 ipconfig 查看），留空则从门户页面里取
    if not ip:
        ip = parse_ip(portal.text, portal.url)  # 从跳转URL参数或页面变量中解析本机IP
        if ip:
            print(f"获取ip成功: {ip}")
        else:
            print("获取ip失败")
            sys.exit(1)  # 退出去之前由 run() 统一停住等回车
        ac_params = parse_ac_params(portal.url)
    else:
        ac_params = {}
        print(f"使用配置文件中的ip: {ip}")

    url = build_login_url(ip, ac_params)
    data = {
        "DDDDD": f",0,{user}@{operator}",
        "upass": password,
        "R1": "0",
        "R2": "0",
        "R3": "0",
        "R6": "0",
        "para": "00",
        "0MKKey": "123456",
        "buttonClicked": "",
        "redirect_url": "",
        "err_flag": "",
        "username": "",
        "password": "",
        "user": "",
        "cmd": "",
        "Login": "",
        "v6ip": "",
    }
    try:
        resp_text = request(url, data=data).text
    except URLError as e:
        print("登录请求失败:", e)
        sys.exit(1)  # 退出去之前由 run() 统一停住等回车

    if "COMWebLoginID_3" in resp_text or user in resp_text:
        print("登录成功!")
    else:
        # 输出错误信息
        print("登录失败，错误信息为:", resp_text)


def create_template_and_report(target):
    """没找到配置文件时：生成模板并提示用户去填。"""
    try:
        write_template(target)
    except OSError as e:
        print(f"未找到配置文件，且无法在 {target} 生成模板: {e}")
        print(f"请手动创建配置文件，或用 --config 路径 / 环境变量 {CONFIG_ENV} 指定位置。")
    else:
        print(f"未找到配置文件，已生成模板: {target}")
        print("请填写 user / password / operator（可选 ip）后重新运行。")
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # 先确认自己在不在本校校园网里。校外访问 http://1.1.1.1 拿到的是 Cloudflare 官网或连不通，
    # 这里直接提示并正常退出，不再往下折腾登录接口。
    try:
        status, portal = check()
    except OffCampusError as e:
        print(f"位于非本校校园网：{e}")
        print("当前不在校园网环境，无需登录，脚本正常退出。")
        return 0

    # 已经在网就不用读账号密码了
    if status == "online":
        print("已是登录状态！")
        return 0

    try:
        explicit = parse_argv(argv)
        path = find_config(explicit)
        if path is None:
            # 没有配置文件：在 exe 同目录（或 --config 指定的位置）生成一份模板
            return create_template_and_report(explicit or os.path.join(app_dir(), CONFIG_NAME))
        user, password, operator, ip = read_account(path)
    except ConfigError as e:
        print("配置错误:", e)
        return 2

    print(f"配置文件: {path}")
    print("未登录")
    login(user, password, operator, ip, portal)
    return 0


def run(argv=None):
    """
    执行 main()，并保证无论怎么结束，退出去之前都停在“按回车键退出...”上：

    - 正常结束 / main() 返回非 0：保留退出码；
    - 脚本内部 sys.exit(...)：退出码照旧，不再重复提示；
    - 没预料到的异常：先把 traceback 打完，再停住等回车，
      这样双击运行时不会“报错一闪、窗口就没了”。

    计划任务、管道重定向时没控制台，pause_if_console() 会自动跳过，不会卡住。
    """
    try:
        return main(argv)
    except SystemExit as e:  # main() 内部（如获取 IP 失败）直接 sys.exit 的情况
        status = getattr(e, "code", 0)
        return status if isinstance(status, int) else (0 if status is None else 1)
    except Exception:
        traceback.print_exc()  # 先打完异常信息，再由 finally 停住
        return 1
    finally:
        pause_if_console()


def setup_console():
    """
    冻结成 exe 后 stdout 可能按本地代码页(cp936)编码：
    若打印的内容里出现无法编码的字符（如 decode 时产生的 U+FFFD），
    会抛 UnicodeEncodeError。这里统一降级为转义输出，保证不因打印而崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):  # 非 TextIOWrapper（如已重定向/被替换）
            pass


if __name__ == "__main__":
    setup_console()
    sys.exit(run())
