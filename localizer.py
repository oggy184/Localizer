"""
Localizer - pengelola lingkungan pengembangan lokal (portabel) ala Laragon.
Hanya butuh Python 3.8+ (Tkinter sudah bawaan). Jalankan: python localizer.py
Jadikan exe: pyinstaller --onefile --noconsole --name Localizer localizer.py
"""
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
import tkinter as tk
from collections import namedtuple
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from urllib.parse import urljoin

# Folder program: tempat exe (PyInstaller) atau tempat skrip ini
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts" if IS_WIN else "/etc/hosts"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
MARK_BEGIN, MARK_END = "# LOCALIZER BEGIN", "# LOCALIZER END"

DEFAULT_CONFIG = {
    "www": "www",
    "tld": "test",
    "apache_dir": "bin/apache",
    "php_dir": "bin/php",
    "mysql_dir": "bin/mysql",
    "nginx_dir": "bin/nginx",
    "phpmyadmin_dir": "bin/phpmyadmin",
    "autostart": ["Apache", "MariaDB"],
    "php_extensions": ["curl", "fileinfo", "gd", "intl", "mbstring", "mysqli", "openssl",
                       "pdo_mysql", "pdo_sqlite", "sqlite3", "zip"],
    "path_extra": ["bin/php", "bin/mysql/bin", "bin/nodejs"],
    "services": [
        {"name": "Apache", "cwd": "bin/apache/bin", "cmd": ["httpd" + EXE], "port": 80},
        {"name": "MariaDB", "cwd": "bin/mysql/bin", "cmd": ["mysqld" + EXE, "--console"], "port": 3306},
        {"name": "Nginx", "cwd": "bin/nginx", "cmd": ["nginx" + EXE], "port": 8080},
        {"name": "PHP-CGI", "cwd": "bin/php", "cmd": ["php-cgi" + EXE, "-b", "127.0.0.1:9000"], "port": 9000,
         "env": {"PHP_FCGI_MAX_REQUESTS": "0"}},
        {"name": "Redis", "cwd": "bin/redis", "cmd": ["redis-server" + EXE], "port": 6379},
        {"name": "PHP Server", "cwd": "bin/php", "cmd": ["php" + EXE, "-S", "127.0.0.1:8000", "-t", "{www}"], "port": 8000},
    ],
}


def load_config():
    """Baca config.json; kunci/layanan baru dari DEFAULT_CONFIG ditambahkan otomatis."""
    default = json.loads(json.dumps(DEFAULT_CONFIG))
    if not os.path.isfile(CONFIG_PATH):
        cfg, changed = default, True
    else:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        changed = False
        for key, val in default.items():
            if key not in cfg:
                cfg[key], changed = val, True
        names = {s["name"] for s in cfg["services"]}
        for svc in default["services"]:
            if svc["name"] not in names:
                cfg["services"].append(svc)
                changed = True
    if changed:
        save_config(cfg)
    return cfg


_CONFIG_LOCK = threading.Lock()


def save_config(cfg):
    # tulis ke file sementara lalu ganti: crash/penyimpanan bersamaan tidak merusak config.json
    with _CONFIG_LOCK:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)


CFG = load_config()


def _dir(key):
    return os.path.normpath(os.path.join(BASE, CFG.get(key, DEFAULT_CONFIG[key])))


WWW = _dir("www")
APACHE = _dir("apache_dir")
PHP = _dir("php_dir")
MYSQL = _dir("mysql_dir")
NGINX = _dir("nginx_dir")
PMA = _dir("phpmyadmin_dir")
os.makedirs(WWW, exist_ok=True)


# ---------- utilitas ----------
def fwd(path):
    return path.replace("\\", "/")


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def strip_block(text):
    """Buang blok LOCALIZER BEGIN..END (dan blok MINILARAGON dari nama lama) dari teks."""
    pat = r"\n?# (?:LOCALIZER|MINILARAGON) BEGIN.*?# (?:LOCALIZER|MINILARAGON) END\n?"
    return re.sub(pat, "\n", text, flags=re.S).rstrip() + "\n"


def with_block(text, body):
    """Ganti (atau tambahkan) blok LOCALIZER di akhir teks."""
    return strip_block(text) + f"\n{MARK_BEGIN}\n{body}\n{MARK_END}\n"


def to_hostname(name):
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")


def valid_hostname(host):
    """Nama domain lengkap yang sah, mis. api.blog.test (minimal dua bagian)."""
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    return len(host) <= 253 and re.fullmatch(rf"{label}(?:\.{label})+", host) is not None


# Satu situs: nama folder, domain utama, document root, folder situs, domain tambahan, blok tambahan
Site = namedtuple("Site", "name host root folder aliases apache_extra nginx_extra")


def site_root(folder, root_cfg):
    """Document root situs. root_cfg kosong = otomatis (public/ bila ada, selain itu folder situs);
    relatif terhadap folder situs atau path absolut. None bila root_cfg diisi tapi tidak ada."""
    if root_cfg:
        path = root_cfg if os.path.isabs(root_cfg) else os.path.join(folder, root_cfg)
        return os.path.normpath(path) if os.path.isdir(path) else None
    public = os.path.join(folder, "public")
    return public if os.path.isdir(public) else folder


def tool_env(extra=None):
    env = os.environ.copy()
    paths = [os.path.join(BASE, p) for p in CFG.get("path_extra", [])]
    env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def php_apache_module():
    """(nama_modul, path_dll) untuk modul PHP Apache, atau None (butuh PHP Thread Safe)."""
    for dll, mod in (("php8apache2_4.dll", "php_module"), ("php7apache2_4.dll", "php7_module")):
        path = os.path.join(PHP, dll)
        if os.path.isfile(path):
            return mod, path
    return None


def pma_ready():
    return os.path.isfile(os.path.join(PMA, "index.php"))


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def switch_php_config(cfg, rel):
    """Arahkan php_dir, path_extra, dan cwd layanan dari folder PHP lama ke `rel`.
    Mengubah cfg langsung; kembalikan nama layanan yang cwd-nya berubah."""
    old = os.path.join(BASE, cfg.get("php_dir", "bin/php"))
    new = os.path.join(BASE, rel)
    cfg["php_dir"] = rel
    extra = [rel if same_path(os.path.join(BASE, p), old) else p for p in cfg.get("path_extra", [])]
    if not any(same_path(os.path.join(BASE, p), new) for p in extra):
        extra.insert(0, rel)
    cfg["path_extra"] = extra
    changed = []
    for s in cfg["services"]:
        if same_path(os.path.join(BASE, s["cwd"]), old):
            s["cwd"] = rel
            changed.append(s["name"])
    return changed


def find_php_versions():
    """Cari semua instalasi PHP: folder bin/php*, plus subfolder satu tingkat di dalamnya.
    Kembalikan [(path_relatif, label)]; label berisi versi dan status Thread Safe."""
    bin_dir = os.path.join(BASE, "bin")
    found = []
    if not os.path.isdir(bin_dir):
        return found
    for d in sorted(os.listdir(bin_dir)):
        top = os.path.join(bin_dir, d)
        if not (d.lower().startswith("php") and os.path.isdir(top)) or same_path(top, PMA):
            continue
        cands = [top] + [os.path.join(top, s) for s in sorted(os.listdir(top))
                         if os.path.isdir(os.path.join(top, s))]
        for path in cands:
            exe = os.path.join(path, "php" + EXE)
            if not os.path.isfile(exe):
                continue
            try:
                ver = subprocess.run([exe, "-n", "-r", "echo PHP_VERSION;"], capture_output=True,
                                     text=True, timeout=5, creationflags=NO_WINDOW).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                ver = ""
            rel = fwd(os.path.relpath(path, BASE))
            ts = "" if not IS_WIN else (" TS" if any(
                os.path.isfile(os.path.join(path, f)) for f in ("php8apache2_4.dll", "php7apache2_4.dll"))
                else " NTS (tanpa Apache)")
            found.append((rel, f"{ver or '?'}{ts}  —  {rel}"))
    return found


# ======================================================================
# Unduh komponen: Apache (Apache Lounge), PHP (windows.php.net),
# MariaDB (mariadb.org), phpMyAdmin (phpmyadmin.net)
# ======================================================================
DL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Localizer-setup"
PHP_RELEASES = "https://windows.php.net/downloads/releases/"
APACHE_PAGE = "https://www.apachelounge.com/download/"
MARIADB_API = "https://downloads.mariadb.org/rest-api/mariadb/"
PMA_VERSION = "https://www.phpmyadmin.net/home_page/version.json"
PMA_FILES = "https://files.phpmyadmin.net/phpMyAdmin/"
VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
COMPONENTS = ["apache", "php", "mariadb", "phpmyadmin"]
COMPONENT_LABELS = {"apache": "Apache", "php": "PHP", "mariadb": "MariaDB", "phpmyadmin": "phpMyAdmin"}


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


_SSL_CTX = None


def ssl_context():
    """Konteks HTTPS yang memercayai sertifikat Windows DAN bundel certifi (bila tersedia).
    - Sertifikat Windows: diperlukan bila antivirus/proxy kantor memeriksa HTTPS.
    - certifi: menolong di Windows yang daftar sertifikat akarnya usang/tidak lengkap."""
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:  # certifi tidak terpasang: cukup sertifikat Windows
            pass
        _SSL_CTX = ctx
    return _SSL_CTX


def open_url(url):
    """urlopen dengan konteks SSL di atas; error sertifikat diubah jadi pesan yang jelas."""
    req = urllib.request.Request(url, headers={"User-Agent": DL_UA})
    try:
        return urllib.request.urlopen(req, timeout=60, context=ssl_context())
    except (ssl.SSLError, urllib.error.URLError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLError):
            raise RuntimeError(
                f"sertifikat SSL {urllib.parse.urlsplit(url).hostname} tidak bisa diverifikasi ({reason}). "
                f"Kemungkinan penyebab: jam/tanggal komputer salah, antivirus atau proxy kantor memeriksa "
                f"HTTPS, atau Windows belum diperbarui. Periksa jam komputer lalu coba lagi.") from e
        raise


def http_get(url):
    with open_url(url) as r:
        return r.read()


def http_json(url):
    return json.loads(http_get(url))


def download_file(url, dest, log, sha=None, progress=None):
    """Unduh ke dest (log tiap 10%, `progress(done, total)` tiap potongan);
    jika sha diberikan, cocokkan SHA-256."""
    url = re.sub(r"^http://", "https://", url)
    log(f"  {url}")
    h = hashlib.sha256()
    with open_url(url) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, step = 0, 10
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
            if total and done * 100 // total >= step:
                log(f"  {done * 100 // total}%  ({done / 1048576:.1f}/{total / 1048576:.1f} MB)")
                step = (done * 100 // total) // 10 * 10 + 10
    if sha and h.hexdigest().lower() != sha.lower():
        os.remove(dest)
        raise RuntimeError(f"checksum SHA-256 tidak cocok untuk {os.path.basename(dest)}")
    # cek zip utuh SEBELUM pemasang menghapus versi lama (Apache tidak punya checksum)
    if dest.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(dest) as z:
                bad = z.testzip()
        except zipfile.BadZipFile:
            bad = "arsip tidak terbaca"
        if bad:
            os.remove(dest)
            raise RuntimeError(f"unduhan {os.path.basename(dest)} rusak/terpotong ({bad}); coba lagi")


def zip_top_folder(zip_path):
    """Nama folder induk tunggal di dalam zip (mis. 'mariadb-12.3.4-winx64/'), atau ''."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) == 1 and all("/" in n for n in names):
            return tops.pop() + "/"
    return ""


def extract_zip(zip_path, dest, strip_prefix=""):
    """Ekstrak zip ke dest; bila strip_prefix diisi, hanya isi folder itu yang diambil."""
    os.makedirs(dest, exist_ok=True)
    root = os.path.normpath(dest) + os.sep
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if strip_prefix:
                if not name.startswith(strip_prefix):
                    continue
                name = name[len(strip_prefix):]
            if not name:
                continue
            target = os.path.normpath(os.path.join(dest, name))
            if not target.startswith(root):
                continue  # lewati path mencurigakan (../)
            if name.endswith("/"):  # folder kosong (mis. logs/) tetap dibuat
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def clear_dir(path, keep=()):
    """Hapus isi folder kecuali nama di `keep` (dipakai saat menimpa instalasi)."""
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        if name in keep:
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)


def component_installed(name):
    if name == "apache":
        return os.path.isfile(os.path.join(APACHE, "bin", "httpd.exe"))
    if name == "php":
        return bool(find_php_versions())
    if name == "mariadb":
        return any(os.path.isfile(os.path.join(MYSQL, "bin", n)) for n in ("mysqld.exe", "mariadbd.exe"))
    if name == "phpmyadmin":
        return pma_ready()
    return False


def _run_version(cmd, pattern):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=15,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(pattern, (r.stdout or "") + (r.stderr or ""))
    return m.group(1) if m else None


def php_version_of(php_dir):
    exe = os.path.join(php_dir, "php" + EXE)
    if not os.path.isfile(exe):
        return None
    return _run_version([exe, "-n", "-r", "echo PHP_VERSION;"], r"(\d+\.\d+\.\d+\S*)")


def installed_version(name):
    """Versi komponen yang terpasang, atau None."""
    if name == "apache":
        exe = os.path.join(APACHE, "bin", "httpd.exe")
        return _run_version([exe, "-v"], r"Apache/(\d+\.\d+\.\d+)") if os.path.isfile(exe) else None
    if name == "mariadb":
        for n in ("mariadbd.exe", "mysqld.exe"):
            exe = os.path.join(MYSQL, "bin", n)
            if os.path.isfile(exe):
                return _run_version([exe, "--version"], r"(\d+\.\d+\.\d+)-MariaDB")
        return None
    if name == "phpmyadmin":
        if not pma_ready():
            return None
        for rel, pat in (("libraries/classes/Version.php", r"VERSION\s*=\s*'([\d.]+)"),
                         ("src/Version.php", r"VERSION\s*=\s*'([\d.]+)"),
                         ("README", r"Version\s+([\d.]+)")):
            path = os.path.join(PMA, rel)
            if os.path.isfile(path):
                m = re.search(pat, read_text(path))
                if m:
                    return m.group(1)
        return "?"
    return None


def branch_of(ver):
    """'8.4.15' -> '8.4'"""
    return ".".join(ver.split(".")[:2])


# --- versi terbaru dari situs resmi ---
def latest_apache():
    """(versi, url, vs) Apache terbaru di Apache Lounge."""
    html = http_get(APACHE_PAGE).decode("utf-8", "replace")
    links = re.findall(r'href="([^"]*?httpd-(2\.4\.\d+)-[^"]*?win64-VS(\d+)\.zip)"', html, re.I)
    if not links:
        raise RuntimeError(f"link unduhan tidak ditemukan di {APACHE_PAGE}")
    href, ver, vs = max(links, key=lambda l: (int(l[2]), vtuple(l[1])))  # VS & versi terbaru
    return ver, urljoin(APACHE_PAGE, href), vs


def php_releases():
    """{cabang: {'version': ..., 'pkg': {...}, 'build': 'ts-vs17-x64'}} hanya yang punya build TS x64."""
    out = {}
    for branch, info in http_json(PHP_RELEASES + "releases.json").items():
        keys = sorted((k for k in info if re.fullmatch(r"ts-vs\d+-x64", k)), key=vtuple)
        if keys:
            out[branch] = {"version": info["version"], "pkg": info[keys[-1]]["zip"], "build": keys[-1]}
    return out


def mariadb_branches():
    """(daftar cabang stabil terbaru dulu, cabang LTS terbaru)."""
    majors = http_json(MARIADB_API)["major_releases"]
    stable = [m for m in majors if m["release_status"] == "Stable"]
    lts = max((m["release_id"] for m in stable if m.get("release_support_type") == "Long Term Support"),
              key=vtuple)
    return sorted((m["release_id"] for m in stable), key=vtuple, reverse=True), lts


def mariadb_latest(branch):
    """(versi, info_file_zip) rilis terbaru satu cabang MariaDB."""
    ver, rel = next(iter(http_json(f"{MARIADB_API}{branch}/latest/")["releases"].items()))
    pkg = next((f for f in rel["files"] if re.fullmatch(r"mariadb-[\d.]+-winx64\.zip", f["file_name"])), None)
    if not pkg:
        raise RuntimeError(f"ZIP Windows untuk MariaDB {ver} tidak ditemukan")
    return ver, pkg


def pma_latest():
    info = http_json(PMA_VERSION)
    return info["version"], info


# --- pemasangan / pembaruan ---
def install_apache(dl_dir, log, force=False, progress=None):
    if component_installed("apache") and not force:
        log(f"Apache sudah ada di {APACHE}.")
        return True
    log("Mencari Apache terbaru di Apache Lounge…")
    ver, url, vs = latest_apache()
    zip_path = os.path.join(dl_dir, url.rsplit("/", 1)[-1])
    log(f"Mengunduh Apache {ver} (VS{vs})…")
    download_file(url, zip_path, log, progress=progress)  # Apache Lounge tidak menyediakan checksum
    conf = os.path.join(APACHE, "conf")
    if os.path.isdir(conf):  # simpan konfigurasi lama (bisa berisi editan manual)
        backup = os.path.join(os.path.dirname(APACHE), "apache-conf-cadangan")
        shutil.rmtree(backup, ignore_errors=True)
        shutil.copytree(conf, backup)
        log(f"Konfigurasi Apache lama dicadangkan ke {backup}")
    clear_dir(APACHE)
    log("Mengekstrak Apache…")
    extract_zip(zip_path, APACHE, strip_prefix="Apache24/")
    if not component_installed("apache"):
        log("  httpd.exe tidak ditemukan setelah ekstrak; struktur zip mungkin berubah.")
        return False
    log(f"Apache {ver} terpasang di {APACHE}.")
    return True


def install_php(dl_dir, log, branches=None, force=False, progress=None):
    """Pasang PHP Thread Safe x64 per cabang (mis. 8.4) ke bin/php/php-<versi>;
    kembalikan [(versi, path_relatif)] yang terpasang."""
    log("Mengambil daftar rilis PHP…")
    releases = php_releases()
    available = sorted(releases, key=vtuple)
    installed = []
    for branch in branches or [available[-1]]:
        info = releases.get(branch)
        if not info:
            log(f"PHP {branch} tidak tersedia. Pilihan: {', '.join(available)}")
            continue
        pkg, ver = info["pkg"], info["version"]
        rel = f"bin/php/php-{ver}"
        dest = os.path.join(BASE, rel)
        if os.path.isfile(os.path.join(dest, "php.exe")) and not force:
            log(f"PHP {ver} sudah ada di {dest}.")
            installed.append((ver, rel))
            continue
        zip_path = os.path.join(dl_dir, pkg["path"])
        log(f"Mengunduh PHP {ver} ({info['build']})…")
        download_file(PHP_RELEASES + pkg["path"], zip_path, log, pkg.get("sha256"), progress)
        clear_dir(dest, keep=("php.ini",))  # ekstensi yang sudah diaktifkan dipertahankan
        log("Mengekstrak PHP…")
        extract_zip(zip_path, dest)
        log(f"PHP {ver} terpasang di {dest}.")
        installed.append((ver, rel))
    return installed


def install_mariadb(dl_dir, log, branch=None, force=False, progress=None):
    if component_installed("mariadb") and not force:
        log(f"MariaDB sudah ada di {MYSQL}.")
        return True
    log("Mengambil daftar rilis MariaDB…")
    stable, lts = mariadb_branches()
    branch = branch or lts
    if branch not in stable:
        log(f"MariaDB {branch} tidak tersedia. Pilihan stabil: {', '.join(stable)}")
        return False
    ver, pkg = mariadb_latest(branch)
    zip_path = os.path.join(dl_dir, pkg["file_name"])
    log(f"Mengunduh MariaDB {ver}…")
    download_file(pkg["file_download_url"], zip_path, log, pkg.get("checksum", {}).get("sha256sum"),
                  progress)
    clear_dir(MYSQL, keep=("data",))  # folder data = database Anda, jangan dihapus
    log("Mengekstrak MariaDB…")
    extract_zip(zip_path, MYSQL, strip_prefix=zip_top_folder(zip_path))
    # versi MariaDB baru bisa hanya punya mariadbd.exe
    svc = next((s for s in CFG["services"] if s["name"] == "MariaDB"), None)
    if svc and not os.path.isfile(os.path.join(MYSQL, "bin", svc["cmd"][0])):
        for exe in ("mariadbd.exe", "mysqld.exe"):
            if os.path.isfile(os.path.join(MYSQL, "bin", exe)):
                svc["cmd"][0] = exe
                save_config(CFG)
                log(f"config.json: MariaDB sekarang memakai {exe}.")
                break
    log(f"MariaDB {ver} terpasang (user root, password kosong; data lama tetap dipakai).")
    return True


def install_phpmyadmin(dl_dir, log, force=False, progress=None):
    if component_installed("phpmyadmin") and not force:
        log(f"phpMyAdmin sudah ada di {PMA}.")
        return True
    log("Mengambil versi phpMyAdmin terbaru…")
    ver, info = pma_latest()
    name = f"phpMyAdmin-{ver}-all-languages.zip"
    sha = http_get(f"{PMA_FILES}{ver}/{name}.sha256").decode().split()[0]
    zip_path = os.path.join(dl_dir, name)
    log(f"Mengunduh phpMyAdmin {ver}…")
    download_file(f"{PMA_FILES}{ver}/{name}", zip_path, log, sha, progress)
    clear_dir(PMA, keep=("config.inc.php",))  # pengaturan login tetap dipakai
    log("Mengekstrak phpMyAdmin…")
    extract_zip(zip_path, PMA, strip_prefix=zip_top_folder(zip_path))
    log(f"phpMyAdmin {ver} terpasang di {PMA}.")
    # beri tahu bila PHP aktif di luar rentang versi yang didukung resmi
    php_ver = php_version_of(PHP)
    rel = next((r for r in info.get("releases", []) if r.get("version") == ver), None)
    if php_ver and rel and rel.get("php_versions"):
        ok = True
        for cond in rel["php_versions"].split(","):
            m = re.fullmatch(r"\s*(>=|<=|>|<)\s*([\d.]+)\s*", cond)
            if m:
                a, b = vtuple(php_ver)[:2], vtuple(m.group(2))[:2]
                ok &= {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[m.group(1)]
        if not ok:
            log(f"  Catatan: phpMyAdmin {ver} resmi mendukung PHP {rel['php_versions']}, PHP aktif {php_ver}. "
                f"Biasanya tetap jalan; bila ada error, pasang PHP versi lebih lama di halaman Komponen "
                f"lalu aktifkan di halaman PHP.")
    return True


def vc_redist_missing():
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    return not all(os.path.isfile(os.path.join(sys32, f)) for f in ("vcruntime140.dll", "vcruntime140_1.dll"))


class Service:
    def __init__(self, cfg, log):
        self.cfg, self.log, self.proc = cfg, log, None
        self.name = cfg["name"]
        self.prepare = None          # fungsi persiapan sebelum start (opsional)
        self.lock = threading.Lock()  # cegah start/stop bersamaan (klik ganda)

    # dihitung dari cfg agar ikut berubah saat versi PHP diganti
    @property
    def cwd(self):
        return os.path.normpath(os.path.join(BASE, self.cfg["cwd"]))

    @property
    def exe(self):
        return os.path.join(self.cwd, self.cfg["cmd"][0])

    @property
    def available(self):
        return os.path.isfile(self.exe)

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            if self.running:
                return
            if not self.available:
                self.log(f"[{self.name}] Tidak ditemukan: {self.exe}\n"
                         f"    Letakkan binary-nya di folder tersebut atau ubah config.json.")
                return
            if self.prepare:
                try:
                    if self.prepare() is False:
                        return
                except Exception as e:  # jangan sampai thread mati diam-diam
                    self.log(f"[{self.name}] Persiapan gagal: {e}")
                    return
            port = self.cfg.get("port")
            if port and port_in_use(port):
                self.log(f"[{self.name}] Peringatan: port {port} sudah dipakai program lain.")
            args = [self.exe] + [a.replace("{www}", WWW).replace("{base}", BASE)
                                 for a in self.cfg["cmd"][1:]]
            try:
                self.proc = subprocess.Popen(
                    args, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", creationflags=NO_WINDOW,
                    env=tool_env(self.cfg.get("env")))
            except OSError as e:
                self.log(f"[{self.name}] Gagal start: {e}")
                return
            self.log(f"[{self.name}] Dimulai (PID {self.proc.pid})")
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.log(f"[{self.name}] {line.rstrip()}")
        self.log(f"[{self.name}] Berhenti.")

    def stop(self):
        with self.lock:
            if not self.running:
                return
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ======================================================================
# Windows: ikon system tray, jalan otomatis saat login, satu instance
# (hanya ctypes/winreg bawaan Python, tanpa dependensi tambahan)
# ======================================================================
APP_ID = "Localizer"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

if IS_WIN:
    import ctypes
    import winreg
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]

    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = LRESULT
    _user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = LRESULT
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    _user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
    _shell32.ExtractIconW.restype = wintypes.HICON
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE


class TrayIcon:
    """Ikon di system tray Windows dengan menu klik kanan.
    `menu` = [(teks, fungsi) atau None untuk garis pemisah]; `on_open` dipanggil saat ikon diklik.
    Callback dijalankan di thread tray: bungkus dengan pengirim ke thread GUI bila menyentuh Tk."""
    WM_TRAY = 0x0400 + 20  # WM_USER + 20
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10

    def __init__(self, tooltip, on_open, menu):
        self.tooltip, self.on_open, self.menu = tooltip, on_open, menu
        self.hwnd = None
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)  # simpan referensi agar tidak dibuang GC

    def start(self):
        """Tampilkan ikon; kembalikan False bila gagal (mis. bukan Windows)."""
        if not IS_WIN:
            return False
        threading.Thread(target=self._loop, daemon=True).start()
        self._ready.wait(5)
        return bool(self.hwnd)

    def stop(self):
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def notify(self, title, text):
        """Tampilkan notifikasi balon/toast dari ikon tray."""
        if self.hwnd:
            nid = self._nid(self.NIF_INFO)
            nid.szInfoTitle, nid.szInfo = title[:63], text[:255]
            _shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(nid))

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        return nid

    def _add_icon(self):
        nid = self._nid(self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP)
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip[:127]
        _shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid))

    def _loop(self):
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, f"{APP_ID}TrayWindow"
            _user32.RegisterClassW(ctypes.byref(wc))  # boleh gagal bila kelas sudah terdaftar
            self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
            # ikon diambil dari exe (Localizer.exe / python.exe); cadangan: ikon aplikasi standar
            self._hicon = _shell32.ExtractIconW(hinst, sys.executable, 0) or \
                _user32.LoadIconW(None, ctypes.c_void_p(32512))
            self.hwnd = _user32.CreateWindowExW(0, wc.lpszClassName, APP_ID, 0, 0, 0, 0, 0,
                                                None, None, hinst, None)
            if self.hwnd:
                self._add_icon()
        finally:
            self._ready.set()
        if not self.hwnd:
            return
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _show_menu(self):
        menu = _user32.CreatePopupMenu()
        for i, item in enumerate(self.menu, start=1):
            if item is None:
                _user32.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
            else:
                _user32.AppendMenuW(menu, 0, i, item[0])  # MF_STRING
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        _user32.SetForegroundWindow(self.hwnd)  # agar menu tertutup saat klik di luar
        cmd = _user32.TrackPopupMenu(menu, 0x0100 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD|RIGHTBUTTON
        _user32.DestroyMenu(menu)
        if cmd and self.menu[cmd - 1]:
            self.menu[cmd - 1][1]()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self.WM_TRAY:
                if lparam in (0x0202, 0x0203):  # WM_LBUTTONUP, WM_LBUTTONDBLCLK
                    self.on_open()
                elif lparam == 0x0205:  # WM_RBUTTONUP
                    self._show_menu()
                return 0
            if msg == getattr(self, "_taskbar_created", None):  # Explorer restart: pasang ulang ikon
                self._add_icon()
                return 0
            if msg == 0x0002:  # WM_DESTROY
                _shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._nid(0)))
                self.hwnd = None
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass  # jangan biarkan error Python lolos ke Windows
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def already_running():
    """True bila Localizer lain sudah berjalan (mutex bernama per folder program)."""
    if not IS_WIN:
        return False
    name = "Local\\" + APP_ID + "-" + hashlib.md5(os.path.normcase(BASE).encode()).hexdigest()
    ctypes.set_last_error(0)
    globals()["_INSTANCE_MUTEX"] = _kernel32.CreateMutexW(None, False, name)  # tetap hidup selama program
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def is_admin():
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def autostart_command():
    """Perintah untuk menjalankan Localizer saat login (mode tray + Start All)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.isfile(pyw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}" --autostart'


def _task_exists():
    r = subprocess.run(["schtasks", "/Query", "/TN", APP_ID], capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def autostart_enabled():
    if not IS_WIN:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_ID)
            return True
    except OSError:
        pass
    return _task_exists()


def set_autostart(enable):
    """Aktifkan/matikan jalan otomatis saat login. Kembalikan pesan hasil.
    Bila berjalan sebagai Administrator dipakai Task Scheduler (hak admin tetap dibawa, karena
    Windows tidak menjalankan program yang butuh admin dari kunci Run); selain itu kunci Run HKCU."""
    # bersihkan dulu kedua cara agar tidak dobel
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_ID)
    except OSError:
        pass
    if _task_exists():
        subprocess.run(["schtasks", "/Delete", "/TN", APP_ID, "/F"], capture_output=True,
                       creationflags=NO_WINDOW)
    if not enable:
        return "Jalan otomatis saat Windows menyala dimatikan."
    cmd = autostart_command()
    if is_admin():
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
        r = subprocess.run(["schtasks", "/Create", "/TN", APP_ID, "/TR", cmd, "/SC", "ONLOGON",
                            "/RL", "HIGHEST", "/IT", "/RU", user, "/F"],
                           capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip() or f"schtasks gagal (kode {r.returncode})")
        return "Jalan otomatis aktif (Task Scheduler, dengan hak Administrator)."
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, cmd)
    return "Jalan otomatis aktif (tanpa hak Administrator: file hosts tidak bisa ditulis otomatis)."


# ======================================================================
# Tampilan: warna, font, dan widget kecil
# ======================================================================
C_BG = "#ffffff"
C_SIDEBAR = "#f5f6f8"
C_BORDER = "#e4e7ec"
C_TEXT = "#101828"
C_MUTED = "#667085"
C_ACCENT = "#3b5bdb"
C_ACCENT_DARK = "#2f4ac0"
C_ACCENT_SOFT = "#e9edfc"
C_GREEN = "#12b76a"
C_GREEN_SOFT = "#e7f8ef"
C_RED = "#f04438"
C_GRAY = "#98a2b3"
FONT = "Segoe UI" if IS_WIN else "Helvetica"
ICON_FONT = "Segoe MDL2 Assets"
F_BODY = (FONT, 10)
F_SMALL = (FONT, 9)
F_BOLD = (FONT, 10, "bold")
F_H1 = (FONT, 18, "bold")
F_H2 = (FONT, 13, "bold")
F_VALUE = (FONT, 16, "bold")

# ikon dari font Segoe MDL2 Assets (bawaan Windows 10/11)
ICONS = {"dashboard": "", "sites": "", "services": "", "php": "",
         "components": "", "log": "", "settings": "",
         "exit": ""}

SERVICE_INFO = {
    "Apache": "Web server",
    "MariaDB": "Database (MySQL)",
    "Nginx": "Web server alternatif",
    "PHP-CGI": "PHP untuk Nginx",
    "Redis": "Cache & antrean",
    "PHP Server": "Server bawaan PHP",
}


class Toggle(tk.Canvas):
    """Sakelar on/off bergaya modern."""

    def __init__(self, master, command, bg=C_BG, scale=1.0):
        self.W, self.H = int(42 * scale), int(24 * scale)
        super().__init__(master, width=self.W, height=self.H, bg=bg, highlightthickness=0, cursor="hand2")
        self.command, self.on, self.enabled = command, False, True
        self.bind("<Button-1>", lambda _e: self.enabled and self.command())
        self._draw()

    def set(self, on, enabled=True):
        if (on, enabled) != (self.on, self.enabled):
            self.on, self.enabled = on, enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self._draw()

    def _draw(self):
        self.delete("all")
        color = (C_GREEN if self.on else "#d0d5dd") if self.enabled else "#eaecf0"
        h, w, r = self.H, self.W, self.H / 2
        self.create_oval(1, 1, h - 1, h - 1, fill=color, outline=color)
        self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=color, outline=color)
        self.create_rectangle(r, 1, w - r, h - 1, fill=color, outline=color)
        x = w - r if self.on else r
        self.create_oval(x - r + 3, 3, x + r - 3, h - 3, fill="white", outline="white")


def make_app_icon(size=32):
    """Ikon jendela: kotak membulat biru dengan huruf L (digambar, tanpa file gambar)."""
    img = tk.PhotoImage(width=size, height=size)
    rad = size // 5
    for y in range(size):
        for x in range(size):
            cx = min(max(x, rad), size - 1 - rad)
            cy = min(max(y, rad), size - 1 - rad)
            if (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad:
                img.put(C_ACCENT, (x, y))
    s = size / 32
    img.put("white", to=(int(10 * s), int(7 * s), int(14 * s), int(25 * s)))
    img.put("white", to=(int(10 * s), int(21 * s), int(23 * s), int(25 * s)))
    return img


class App(tk.Tk):
    def __init__(self, autostart=False):
        if IS_WIN:  # teks tajam di layar berskala 125%/150% (tanpa ini Windows memburamkan jendela)
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        super().__init__()
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title("Localizer")
        self.geometry(f"{self.px(1120)}x{self.px(720)}")
        self.minsize(self.px(960), self.px(600))
        self.logq = queue.Queue()
        self.services = [Service(c, self.logq.put) for c in CFG["services"]]
        hooks = {"Apache": self.prepare_apache, "MariaDB": self.prepare_mariadb,
                 "Nginx": self.prepare_nginx, "PHP-CGI": self.prepare_php,
                 "PHP Server": self.prepare_php}
        for s in self.services:
            s.prepare = hooks.get(s.name)
        self._closing = False
        self._downloading = False
        self._tray_hint_shown = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._drain_log)
        self.after(100, self._refresh_status)
        self.log(f"Folder proyek: {WWW}")
        self._check_components()

        # ikon tray: klik = buka jendela; menu klik kanan. Callback dikirim ke thread GUI lewat self.ui
        self.tray = TrayIcon(APP_ID, lambda: self.ui(self.show_window), [
            ("Buka Localizer", lambda: self.ui(self.show_window)),
            ("Buka di Browser", lambda: self.ui(self.open_browser)),
            None,
            ("Start All", lambda: self.ui(self.start_all)),
            ("Stop All", lambda: self.ui(self.stop_all)),
            None,
            ("Keluar", lambda: self.ui(self.quit_app)),
        ])
        if not self.tray.start():
            self.tray = None
        if autostart:
            self.log("Dijalankan otomatis saat login: menyalakan layanan…")
            if self.tray:
                self.withdraw()
            self.start_all()

    # ---------- UI ----------
    def px(self, n):
        """Ukuran piksel yang mengikuti skala layar."""
        return int(n * self.scale)

    def _setup_style(self):
        self.configure(bg=C_BG)
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", font=F_BODY, background=C_BG, foreground=C_TEXT, bordercolor=C_BORDER,
                     lightcolor=C_BG, darkcolor=C_BG, troughcolor=C_SIDEBAR, focuscolor=C_BG)
        st.configure("TFrame", background=C_BG)
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("Muted.TLabel", foreground=C_MUTED)
        st.configure("TButton", padding=(14, 7), background="#f2f4f7", foreground=C_TEXT,
                     bordercolor=C_BORDER, lightcolor="#f2f4f7", darkcolor="#f2f4f7", relief="flat")
        st.map("TButton", background=[("disabled", "#f9fafb"), ("pressed", "#e4e7ec"), ("active", "#eaecf0")],
               lightcolor=[("active", "#eaecf0")], darkcolor=[("active", "#eaecf0")],
               foreground=[("disabled", C_GRAY)])
        st.configure("Accent.TButton", background=C_ACCENT, foreground="white", bordercolor=C_ACCENT,
                     lightcolor=C_ACCENT, darkcolor=C_ACCENT)
        st.map("Accent.TButton", background=[("pressed", C_ACCENT_DARK), ("active", C_ACCENT_DARK)],
               lightcolor=[("active", C_ACCENT_DARK)], darkcolor=[("active", C_ACCENT_DARK)],
               bordercolor=[("active", C_ACCENT_DARK)])
        st.configure("Small.TButton", padding=(10, 4), font=F_SMALL)
        st.configure("TCheckbutton", background=C_BG, padding=(0, 4))
        st.map("TCheckbutton", background=[("active", C_BG)])
        st.configure("TEntry", padding=(8, 6), fieldbackground="white")
        st.configure("TCombobox", padding=(6, 4), fieldbackground="white", arrowsize=self.px(12))
        st.map("TCombobox", fieldbackground=[("readonly", "white")], selectbackground=[("readonly", "white")],
               selectforeground=[("readonly", C_TEXT)])
        st.configure("Treeview", rowheight=self.px(36), fieldbackground=C_BG, background=C_BG, borderwidth=0)
        st.configure("Treeview.Heading", font=F_SMALL, background="#f9fafb", foreground=C_MUTED,
                     relief="flat", padding=(8, 6), bordercolor=C_BORDER, lightcolor="#f9fafb",
                     darkcolor="#f9fafb")
        st.map("Treeview", background=[("selected", C_ACCENT_SOFT)], foreground=[("selected", C_TEXT)])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # tanpa bingkai
        st.configure("Accent.Horizontal.TProgressbar", troughcolor="#e4e7ec", background=C_ACCENT,
                     bordercolor="#e4e7ec", lightcolor=C_ACCENT, darkcolor=C_ACCENT,
                     thickness=self.px(6))
        self._icon_img = make_app_icon(self.px(32))
        self.iconphoto(True, self._icon_img)

    def _build_ui(self):
        self._setup_style()
        self._pending = {}  # nama layanan -> "start"/"stop" selama proses berjalan

        # --- sidebar ---
        side = tk.Frame(self, bg=C_SIDEBAR, width=self.px(220))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(self, bg=C_BORDER, width=1).pack(side="left", fill="y")

        brand = tk.Frame(side, bg=C_SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(20, 22))
        size = self.px(32)
        logo = tk.Canvas(brand, width=size, height=size, bg=C_SIDEBAR, highlightthickness=0)
        logo.pack(side="left")
        logo.create_image(size // 2, size // 2, image=self._icon_img)
        tk.Label(brand, text="Localizer", font=(FONT, 14, "bold"), bg=C_SIDEBAR, fg=C_TEXT).pack(
            side="left", padx=(10, 0))

        self.nav = {}
        for key, text in [("dashboard", "Dashboard"), ("sites", "Situs"), ("services", "Layanan"),
                          ("php", "PHP"), ("components", "Komponen"), ("log", "Log"),
                          ("settings", "Pengaturan")]:
            self.nav[key] = self._nav_item(side, key, text)

        bottom = tk.Frame(side, bg=C_SIDEBAR)
        bottom.pack(side="bottom", fill="x", padx=12, pady=14)
        self.side_status = tk.Label(bottom, text="", font=F_SMALL, bg=C_SIDEBAR, fg=C_MUTED, anchor="w")
        self.side_status.pack(fill="x", padx=8, pady=(0, 8))
        self._nav_item(bottom, "exit", "Keluar", command=self.quit_app, pack_pad=0)

        # --- area konten ---
        self.content = tk.Frame(self, bg=C_BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self.pages = {}
        for key, builder in [("dashboard", self._page_dashboard), ("sites", self._page_sites),
                             ("services", self._page_services), ("php", self._page_php),
                             ("components", self._page_components), ("log", self._page_log),
                             ("settings", self._page_settings)]:
            page = tk.Frame(self.content, bg=C_BG)
            page.grid(row=0, column=0, sticky="nsew", padx=32, pady=26)
            builder(page)
            self.pages[key] = page
        self._build_download_banner()
        self.refresh_php_versions()
        self.refresh_sites()
        self.show_page("dashboard")

    def _nav_item(self, parent, key, text, command=None, pack_pad=12):
        row = tk.Frame(parent, bg=C_SIDEBAR, cursor="hand2")
        row.pack(fill="x", padx=pack_pad, pady=1)
        icon = tk.Label(row, text=ICONS.get(key, ""), font=(ICON_FONT, 11), bg=C_SIDEBAR, fg=C_MUTED,
                        width=2)
        icon.pack(side="left", padx=(10, 6), pady=9)
        label = tk.Label(row, text=text, font=F_BODY, bg=C_SIDEBAR, fg=C_TEXT, anchor="w")
        label.pack(side="left", fill="x", expand=True)
        widgets = (row, icon, label)
        action = command or (lambda: self.show_page(key))

        def paint(bg, fg=C_TEXT, ifg=C_MUTED):
            for w in widgets:
                w.configure(bg=bg)
            label.configure(fg=fg)
            icon.configure(fg=ifg)

        def enter(_e):
            if getattr(self, "current_page", None) != key:
                paint("#eaecf0")

        def leave(_e):
            if getattr(self, "current_page", None) != key:
                paint(C_SIDEBAR)

        for w in widgets:
            w.bind("<Button-1>", lambda _e: action())
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
        return paint

    def show_page(self, key):
        self.current_page = key
        for k, paint in self.nav.items():
            if k == key:
                paint(C_ACCENT_SOFT, C_ACCENT, C_ACCENT)
            else:
                paint(C_SIDEBAR)
        self.pages[key].tkraise()
        if key == "sites":
            self.refresh_sites()
        elif key == "php":
            self.refresh_php_versions()
        elif key == "components":
            self.refresh_components()
            if not self.comp_latest and not self._checking:
                self.check_updates()  # periksa versi terbaru saat halaman pertama kali dibuka

    # --- komponen halaman ---
    def _header(self, page, title, subtitle, buttons=()):
        head = tk.Frame(page, bg=C_BG)
        head.pack(fill="x", pady=(0, 20))
        txt = tk.Frame(head, bg=C_BG)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=title, font=F_H1, bg=C_BG, fg=C_TEXT, anchor="w").pack(fill="x")
        tk.Label(txt, text=subtitle, font=F_BODY, bg=C_BG, fg=C_MUTED, anchor="w").pack(fill="x", pady=(2, 0))
        made = []
        for text, cmd, accent in reversed(buttons):
            b = ttk.Button(head, text=text, command=cmd, style="Accent.TButton" if accent else "TButton")
            b.pack(side="right", padx=(8, 0))
            made.append(b)
        return head

    def _card(self, parent, **pack):
        card = tk.Frame(parent, bg=C_BG, highlightthickness=1, highlightbackground=C_BORDER)
        if pack:
            card.pack(**pack)
        return card

    def _section(self, page, title):
        tk.Label(page, text=title, font=F_H2, bg=C_BG, fg=C_TEXT, anchor="w").pack(fill="x", pady=(18, 8))

    # --- Dashboard ---
    def _page_dashboard(self, page):
        self._header(page, "Dashboard", "Ringkasan lingkungan pengembangan lokal Anda.",
                     [("Stop All", self.stop_all, False), ("Start All", self.start_all, True)])
        grid = tk.Frame(page, bg=C_BG)
        grid.pack(fill="x")
        self.dash_cards = {}
        for i, (key, title) in enumerate([("web", "Web server"), ("db", "Database"),
                                          ("php", "PHP aktif"), ("sites", "Situs")]):
            grid.grid_columnconfigure(i, weight=1, uniform="cards")
            card = self._card(grid)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 12, 0))
            tk.Label(card, text=title, font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(
                fill="x", padx=16, pady=(14, 2))
            value = tk.Label(card, text="–", font=F_VALUE, bg=C_BG, fg=C_TEXT, anchor="w")
            value.pack(fill="x", padx=16)
            sub = tk.Label(card, text="", font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w")
            sub.pack(fill="x", padx=16, pady=(2, 14))
            self.dash_cards[key] = (value, sub)

        self._section(page, "Akses cepat")
        quick = tk.Frame(page, bg=C_BG)
        quick.pack(fill="x")
        for text, cmd in [("Buka localhost", self.open_browser), ("phpMyAdmin", self.open_phpmyadmin),
                          ("Folder www", self.open_www), ("Terminal", self.open_terminal),
                          ("Situs baru…", self.new_project)]:
            ttk.Button(quick, text=text, command=cmd).pack(side="left", padx=(0, 8))

        self._section(page, "Aktivitas terbaru")
        box = self._card(page, fill="both", expand=True)
        self.recent = tk.Text(box, font=("Consolas", 9), bg=C_BG, fg="#344054", relief="flat",
                              wrap="word", state="disabled", padx=12, pady=10, height=8,
                              highlightthickness=0, cursor="arrow")
        self.recent.pack(fill="both", expand=True)

    def _update_dashboard(self):
        def status(name):
            s = self.svc(name)
            if not s or not s.available:
                return "Belum terpasang", C_GRAY
            return ("Berjalan", C_GREEN) if s.running else ("Berhenti", C_MUTED)

        web = next((n for n in ("Apache", "Nginx") if self.svc(n) and self.svc(n).running), "Apache")
        text, color = status(web)
        value, sub = self.dash_cards["web"]
        value.configure(text=text, fg=color)
        sub.configure(text=f"{web} · port {self.port_of(web, 80)}")
        text, color = status("MariaDB")
        value, sub = self.dash_cards["db"]
        value.configure(text=text, fg=color)
        sub.configure(text=f"MariaDB · port {self.port_of('MariaDB', 3306)}")
        value, sub = self.dash_cards["php"]
        ver = getattr(self, "active_php", None)
        value.configure(text=ver[0] if ver else "Belum ada", fg=C_TEXT if ver else C_GRAY)
        sub.configure(text=ver[1] if ver else "Unduh lewat halaman PHP")
        value, sub = self.dash_cards["sites"]
        n = len(getattr(self, "site_rows", []))
        value.configure(text=str(n))
        sub.configure(text=f"domain *.{CFG.get('tld', 'test')}")

    # --- Situs ---
    def _page_sites(self, page):
        self._header(page, "Situs", "Setiap folder di www otomatis menjadi domain lokal. "
                                    "Klik dua kali untuk membuka situs.",
                     [("Folder www", self.open_www, False), ("Sinkronkan", self.sync_vhosts, False),
                      ("+ Situs Baru", self.new_project, True)])
        box = self._card(page, fill="both", expand=True)
        cols = ("url", "type", "folder")
        self.sites_tree = ttk.Treeview(box, columns=cols, show="tree headings", selectmode="browse")
        self.sites_tree.heading("#0", text="NAMA", anchor="w")
        self.sites_tree.heading("url", text="URL", anchor="w")
        self.sites_tree.heading("type", text="JENIS", anchor="w")
        self.sites_tree.heading("folder", text="FOLDER", anchor="w")
        self.sites_tree.column("#0", width=170, minwidth=120, stretch=False)
        self.sites_tree.column("url", width=230, minwidth=150, stretch=False)
        self.sites_tree.column("type", width=90, minwidth=70, stretch=False)
        self.sites_tree.column("folder", width=300, minwidth=150, stretch=True)
        self.sites_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.sites_tree.bind("<Double-1>", lambda _e: self.site_action("browser"))
        self.sites_tree.bind("<<TreeviewSelect>>", lambda _e: self._sites_selection_changed())
        self.sites_empty = tk.Label(box, text="Belum ada situs.\nKlik \"+ Situs Baru\" atau buat folder di www.",
                                    font=F_BODY, bg=C_BG, fg=C_MUTED, justify="center")

        bar = tk.Frame(page, bg=C_BG)
        bar.pack(fill="x", pady=(12, 0))
        self.site_buttons = []
        actions = [("Buka di browser", "browser"), ("Pengaturan situs…", "settings"),
                   ("Buka folder", "folder"), ("Terminal", "terminal")]
        if shutil.which("code"):
            actions.append(("Buka di VS Code", "code"))
        for text, act in actions:
            b = ttk.Button(bar, text=text, command=lambda a=act: self.site_action(a), state="disabled")
            b.pack(side="left", padx=(0, 8))
            self.site_buttons.append(b)

    def refresh_sites(self):
        self.site_rows = []
        tree = self.sites_tree
        tree.delete(*tree.get_children())
        custom = CFG.get("sites", {})
        for site in self.collect_sites():
            folder = site.folder
            if os.path.isfile(os.path.join(folder, "artisan")):
                kind = "Laravel"
            elif os.path.isfile(os.path.join(folder, "wp-config.php")) or \
                    os.path.isfile(os.path.join(folder, "wp-login.php")):
                kind = "WordPress"
            else:
                kind = "PHP"
            self.site_rows.append(site)
            url = self.site_url(site.host) + (f"   +{len(site.aliases)} domain" if site.aliases else "")
            name = "  " + site.host.rsplit(".", 1)[0] + ("   ⚙" if custom.get(site.name) else "")
            tree.insert("", "end", iid=site.host, text=name,
                        values=(url, kind, os.path.relpath(folder, BASE)))
        if self.site_rows:
            self.sites_empty.place_forget()
        else:
            self.sites_empty.place(relx=0.5, rely=0.5, anchor="center")
        self._sites_selection_changed()
        self._update_dashboard()

    def _sites_selection_changed(self):
        state = "normal" if self.sites_tree.selection() else "disabled"
        for b in self.site_buttons:
            b.configure(state=state)

    def site_url(self, host):
        web = next((n for n in ("Apache", "Nginx") if self.svc(n) and self.svc(n).running), "Apache")
        port = self.port_of(web, 80 if web == "Apache" else 8080)
        return f"http://{host}" + ("" if port == 80 else f":{port}")

    def site_action(self, action):
        sel = self.sites_tree.selection()
        if not sel:
            return
        host = sel[0]
        site = next((s for s in self.site_rows if s.host == host), None)
        if not site:
            return
        folder = site.folder
        if action == "settings":
            self.open_site_settings(site)
        elif action == "browser":
            if not self.web_url():
                self.log("Apache/Nginx belum berjalan — nyalakan dulu di halaman Layanan.")
                self.show_page("services")
                return
            webbrowser.open(self.site_url(host))
        elif action == "folder":
            self.open_path(folder)
        elif action == "terminal":
            self.open_terminal(folder)
        elif action == "code":
            subprocess.Popen(["code", os.path.normpath(folder)], shell=IS_WIN, creationflags=NO_WINDOW)

    # --- Pengaturan per situs ---
    def open_site_settings(self, site):
        if getattr(self, "_site_saving", False):
            messagebox.showinfo("Pengaturan situs", "Pengaturan sebelumnya masih diterapkan, tunggu sebentar.")
            return
        sc = CFG.get("sites", {}).get(site.name, {})
        tld = CFG.get("tld", "test")
        dlg = tk.Toplevel(self)
        dlg.title(f"Pengaturan situs — {site.name}")
        dlg.transient(self)
        dlg.configure(bg=C_BG)
        dlg.minsize(self.px(560), self.px(520))
        frm = tk.Frame(dlg, bg=C_BG)
        frm.pack(fill="both", expand=True, padx=24, pady=20)
        frm.grid_columnconfigure(0, weight=1)

        def label(row, text, help_text=""):
            tk.Label(frm, text=text, font=F_BOLD, bg=C_BG, fg=C_TEXT, anchor="w").grid(
                row=row, column=0, columnspan=2, sticky="ew", pady=(12, 2))
            if help_text:
                tk.Label(frm, text=help_text, font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w", justify="left",
                         wraplength=self.px(520)).grid(row=row + 1, column=0, columnspan=2, sticky="ew")

        def textbox(row, value, height, mono=False):
            box = tk.Frame(frm, bg=C_BG, highlightthickness=1, highlightbackground=C_BORDER,
                           highlightcolor=C_ACCENT)
            box.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
            t = tk.Text(box, height=height, font=("Consolas", 10) if mono else F_BODY, relief="flat",
                        wrap="none" if mono else "word", padx=8, pady=6, bg="white", undo=True)
            t.pack(fill="both", expand=True)
            t.insert("1.0", value)
            return t

        tk.Label(frm, text=site.name, font=F_H2, bg=C_BG, fg=C_TEXT, anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(frm, text=f"Domain utama: {site.host}   ·   Folder: {os.path.relpath(site.folder, BASE)}",
                 font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").grid(row=1, column=0, columnspan=2, sticky="ew")

        label(2, "Domain tambahan",
              f"Satu domain per baris. Tanpa titik otomatis ditambah .{tld} (mis. api → api.{tld}). "
              f"Contoh: api.{site.host}")
        aliases_txt = textbox(4, "\n".join(sc.get("aliases", [])), 3)

        label(5, "Folder root (document root)",
              "Kosongkan / (otomatis) = folder public bila ada, selain itu folder situs. "
              "Bisa relatif terhadap folder situs (mis. web, dist) atau path lengkap.")
        root_var = tk.StringVar(value=sc.get("root", "") or "(otomatis)")
        subdirs = sorted(d for d in os.listdir(site.folder)
                         if os.path.isdir(os.path.join(site.folder, d)) and not d.startswith("."))
        root_row = tk.Frame(frm, bg=C_BG)
        root_row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Combobox(root_row, textvariable=root_var, values=["(otomatis)", *subdirs]).pack(
            side="left", fill="x", expand=True)

        def browse():
            path = filedialog.askdirectory(parent=dlg, initialdir=site.folder, title="Pilih folder root")
            if path:
                rel = os.path.relpath(path, site.folder)
                root_var.set(fwd(rel) if not rel.startswith("..") else fwd(path))
        ttk.Button(root_row, text="Pilih…", style="Small.TButton", command=browse).pack(side="left", padx=(8, 0))

        label(8, "Konfigurasi Apache tambahan",
              "Disisipkan di dalam <VirtualHost> situs ini. Contoh: Header set X-Frame-Options \"DENY\"")
        apache_txt = textbox(10, sc.get("apache", ""), 5, mono=True)
        label(11, "Konfigurasi Nginx tambahan", "Disisipkan di dalam blok server { } situs ini (bila memakai Nginx).")
        nginx_txt = textbox(13, sc.get("nginx", ""), 3, mono=True)
        frm.grid_rowconfigure(10, weight=1)

        def reset():
            for t in (aliases_txt, apache_txt, nginx_txt):
                t.delete("1.0", "end")
            root_var.set("(otomatis)")

        def save():
            # domain tambahan
            aliases = []
            others = {h for s in self.site_rows if s.name != site.name for h in (s.host, *s.aliases)}
            for raw in re.split(r"[\s,;]+", aliases_txt.get("1.0", "end")):
                a = raw.strip().lower().rstrip(".")
                if not a:
                    continue
                if "." not in a:
                    a = f"{a}.{tld}"
                if not valid_hostname(a):
                    messagebox.showerror("Domain tidak valid", f"'{raw}' bukan nama domain yang sah.", parent=dlg)
                    return
                if a in others:
                    owner = next(s.name for s in self.site_rows if a in (s.host, *s.aliases))
                    messagebox.showerror("Domain sudah dipakai", f"{a} sudah dipakai situs '{owner}'.", parent=dlg)
                    return
                if a != site.host and a not in aliases:
                    aliases.append(a)
            # folder root
            root = root_var.get().strip()
            root = "" if root in ("", "(otomatis)") else root
            if root and site_root(site.folder, root) is None:
                messagebox.showerror("Folder tidak ditemukan", f"Folder root '{root}' tidak ada.", parent=dlg)
                return
            new = {k: v for k, v in (("aliases", aliases), ("root", root),
                                     ("apache", apache_txt.get("1.0", "end").strip()),
                                     ("nginx", nginx_txt.get("1.0", "end").strip())) if v}
            old = json.loads(json.dumps(CFG.get("sites", {}).get(site.name)))
            self._store_site_settings(site.name, new)
            dlg.destroy()
            self._site_saving = True
            self._dl_show(f"Menerapkan pengaturan situs {site.name}…", "Menulis vhost dan menguji konfigurasi",
                          icon=ICONS["settings"])
            threading.Thread(target=self._apply_site_settings, args=(site.name, old), daemon=True).start()

        btns = tk.Frame(frm, bg=C_BG)
        btns.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(btns, text="Kosongkan semua", command=reset).pack(side="left")
        ttk.Button(btns, text="Simpan", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(btns, text="Batal", command=dlg.destroy).pack(side="right", padx=(0, 8))
        dlg.grab_set()

    @staticmethod
    def _store_site_settings(name, settings):
        sites = CFG.setdefault("sites", {})
        if settings:
            sites[name] = settings
        else:
            sites.pop(name, None)
        save_config(CFG)

    def _apply_site_settings(self, name, old):
        """Tulis vhost, uji konfigurasi; bila ditolak, kembalikan pengaturan lama (thread terpisah)."""
        try:
            self.sync_vhosts(restart=False)
            error = self.test_server_configs()
            if error:
                self._store_site_settings(name, old)
                self.sync_vhosts(restart=False)
                self.log(f"Pengaturan situs '{name}' ditolak server dan dikembalikan:\n{error}")
                self.ui(lambda: self._dl_done(False, f"Pengaturan situs '{name}' tidak disimpan",
                                              "Konfigurasi ditolak server; pengaturan dikembalikan. Lihat log."))
                self.ui(lambda: messagebox.showerror(
                    "Konfigurasi ditolak", f"{error[:1500]}\n\nPengaturan situs '{name}' dikembalikan ke sebelumnya."))
            else:
                self.restart_web_servers()
                self.log(f"Pengaturan situs '{name}' disimpan.")
                self.ui(lambda: self._dl_done(True, f"Pengaturan situs '{name}' disimpan",
                                              "Vhost dan file hosts sudah diperbarui."))
        except Exception as e:
            self.log(f"Gagal menerapkan pengaturan situs '{name}': {e}")
            self.ui(lambda: self._dl_done(False, f"Gagal menerapkan pengaturan situs '{name}'", str(e)))
        finally:
            self._site_saving = False

    # --- Layanan ---
    def _page_services(self, page):
        self._header(page, "Layanan", "Nyalakan atau matikan server yang Anda butuhkan.",
                     [("Stop All", self.stop_all, False), ("Start All", self.start_all, True)])
        self.svc_rows = {}
        for svc in self.services:
            card = self._card(page, fill="x", pady=(0, 10))
            dot = tk.Label(card, text="●", font=(FONT, 12), bg=C_BG, fg=C_GRAY)
            dot.pack(side="left", padx=(16, 10), pady=14)
            txt = tk.Frame(card, bg=C_BG)
            txt.pack(side="left", fill="x", expand=True)
            tk.Label(txt, text=svc.name, font=F_BOLD, bg=C_BG, fg=C_TEXT, anchor="w").pack(fill="x")
            desc = tk.Label(txt, text="", font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w")
            desc.pack(fill="x")
            toggle = Toggle(card, command=lambda s=svc: self.toggle(s), scale=self.scale)
            toggle.pack(side="right", padx=16)
            status = tk.Label(card, text="", font=F_SMALL, bg=C_BG, fg=C_MUTED, width=16, anchor="e")
            status.pack(side="right")
            self.svc_rows[svc.name] = (dot, desc, status, toggle)
        tk.Label(page, text="Layanan yang belum terpasang bisa dipasang lewat halaman Komponen.",
                 font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(fill="x", pady=(6, 0))

    def _refresh_status(self):
        running = 0
        for svc in self.services:
            dot, desc, status, toggle = self.svc_rows[svc.name]
            pending = self._pending.get(svc.name)
            port = svc.cfg.get("port")
            desc.configure(text=SERVICE_INFO.get(svc.name, "Layanan") + (f" · port {port}" if port else ""))
            if not svc.available:
                dot.configure(fg=C_GRAY)
                status.configure(text="Belum terpasang", fg=C_GRAY)
                toggle.set(False, enabled=False)
                continue
            if svc.running:
                running += 1
            if pending:
                status.configure(text="Memulai…" if pending == "start" else "Menghentikan…", fg=C_ACCENT)
                dot.configure(fg=C_ACCENT)
            elif svc.running:
                status.configure(text=f"Berjalan · PID {svc.proc.pid}", fg=C_GREEN)
                dot.configure(fg=C_GREEN)
            else:
                status.configure(text="Berhenti", fg=C_MUTED)
                dot.configure(fg=C_GRAY)
            toggle.set(svc.running if not pending else pending == "start", enabled=not pending)
        self.side_status.configure(
            text=f"●  {running} layanan berjalan" if running else "○  Semua layanan berhenti",
            fg=C_GREEN if running else C_MUTED)
        self._update_dashboard()
        self.after(1000, self._refresh_status)

    # --- PHP ---
    def _page_php(self, page):
        self._header(page, "PHP", "Kelola versi PHP. Versi aktif dipakai Apache, Nginx, dan terminal.",
                     [("Folder PHP", lambda: self.open_path(PHP), False),
                      ("Edit php.ini", self.edit_php_ini, False),
                      ("Pasang versi PHP lain…", lambda: self.show_page("components"), True)])
        self.php_list = tk.Frame(page, bg=C_BG)
        self.php_list.pack(fill="x")
        exts = ", ".join(CFG.get("php_extensions", []))
        tk.Label(page, text=f"Ekstensi yang diaktifkan otomatis: {exts}", font=F_SMALL, bg=C_BG,
                 fg=C_MUTED, anchor="w", wraplength=700, justify="left").pack(fill="x", pady=(10, 0))

    def refresh_php_versions(self):
        self.php_versions = find_php_versions()
        self.active_php = None
        for w in self.php_list.winfo_children():
            w.destroy()
        if not self.php_versions:
            card = self._card(self.php_list, fill="x")
            tk.Label(card, text="Belum ada PHP terpasang. Klik \"Pasang versi PHP lain…\".", font=F_BODY,
                     bg=C_BG, fg=C_MUTED).pack(padx=16, pady=18, anchor="w")
        for rel, label in self.php_versions:
            ver = label.split()[0]
            active = same_path(os.path.join(BASE, rel), PHP)
            nts = "NTS" in label
            card = self._card(self.php_list, fill="x", pady=(0, 10))
            txt = tk.Frame(card, bg=C_BG)
            txt.pack(side="left", fill="x", expand=True, padx=16, pady=12)
            tk.Label(txt, text=f"PHP {ver}", font=F_BOLD, bg=C_BG, fg=C_TEXT, anchor="w").pack(fill="x")
            tk.Label(txt, text=f"{rel}  ·  {'Non Thread Safe (tanpa Apache)' if nts else 'Thread Safe'}",
                     font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(fill="x")
            if active:
                self.active_php = (ver, rel)
                tk.Label(card, text="  Aktif  ", font=F_SMALL, bg=C_GREEN_SOFT, fg="#027a48").pack(
                    side="right", padx=16)
            else:
                ttk.Button(card, text="Aktifkan", style="Small.TButton",
                           command=lambda r=rel: self.set_php(r)).pack(side="right", padx=16)
        if hasattr(self, "dash_cards"):
            self._update_dashboard()

    def edit_php_ini(self):
        ini = os.path.join(PHP, "php.ini")
        if not os.path.isfile(ini):
            self.prepare_php()
        if os.path.isfile(ini):
            self.open_path(ini)
        else:
            self.log("php.ini belum ada — pasang PHP dulu.")

    # --- Log ---
    def _page_log(self, page):
        self._header(page, "Log", "Keluaran semua layanan dan aktivitas Localizer.",
                     [("Bersihkan", self.clear_log, False)])
        box = self._card(page, fill="both", expand=True)
        self.logbox = scrolledtext.ScrolledText(box, state="disabled", font=("Consolas", 9), wrap="word",
                                                relief="flat", bg=C_BG, fg="#344054", padx=12, pady=10,
                                                highlightthickness=0)
        self.logbox.pack(fill="both", expand=True)

    def clear_log(self):
        for box in (self.logbox, self.recent):
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.configure(state="disabled")

    # --- Pengaturan ---
    def _page_settings(self, page):
        self._header(page, "Pengaturan", "Atur perilaku Localizer.")

        self._section(page, "Umum")
        if IS_WIN:
            self.autostart_var = tk.BooleanVar(value=autostart_enabled())
            ttk.Checkbutton(page, text="Jalankan Localizer saat Windows menyala (di tray, lalu Start All)",
                            variable=self.autostart_var, command=self.toggle_autostart).pack(anchor="w")
        tk.Label(page, text="Tombol tutup (X) menyembunyikan Localizer ke tray; layanan tetap berjalan.",
                 font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(fill="x")

        self._section(page, "Domain")
        row = tk.Frame(page, bg=C_BG)
        row.pack(fill="x")
        tk.Label(row, text="Akhiran domain (TLD)   .", font=F_BODY, bg=C_BG).pack(side="left")
        self.tld_var = tk.StringVar(value=CFG.get("tld", "test"))
        ttk.Entry(row, textvariable=self.tld_var, width=12).pack(side="left")
        ttk.Button(row, text="Simpan", command=self.save_tld).pack(side="left", padx=8)
        tk.Label(page, text="Contoh: folder www/blog menjadi blog.test. Hindari .dev dan .app (wajib HTTPS "
                            "di browser).", font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(fill="x",
                                                                                              pady=(4, 0))

        self._section(page, "Folder")
        for label, path in [("Proyek (www)", WWW), ("Program", BASE)]:
            r = tk.Frame(page, bg=C_BG)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=label, font=F_BODY, bg=C_BG, width=14, anchor="w").pack(side="left")
            tk.Label(r, text=path, font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(side="left")
            ttk.Button(r, text="Buka", style="Small.TButton",
                       command=lambda p=path: self.open_path(p)).pack(side="right")

        self._section(page, "Komponen")
        r = tk.Frame(page, bg=C_BG)
        r.pack(fill="x")
        ttk.Button(r, text="Pasang & perbarui komponen…", style="Accent.TButton",
                   command=lambda: self.show_page("components")).pack(side="left")
        ttk.Button(r, text="Sinkronkan Virtual Host", command=self.sync_vhosts).pack(side="left", padx=8)
        ttk.Button(r, text="Edit config.json", command=self.edit_config).pack(side="left")

    def save_tld(self):
        tld = re.sub(r"[^a-z0-9-]", "", self.tld_var.get().strip().lower().lstrip("."))
        if not tld:
            messagebox.showerror("Pengaturan", "Akhiran domain tidak valid.")
            return
        self.tld_var.set(tld)
        if tld == CFG.get("tld"):
            return
        CFG["tld"] = tld
        save_config(CFG)
        self.log(f"Akhiran domain diganti ke .{tld}.")
        self.sync_vhosts()

    # ---------- log ----------
    def log(self, msg):
        self.logq.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self.logq.get_nowait()
                if callable(msg):  # pekerjaan UI yang dititipkan thread lain
                    msg()
                    continue
                for box, limit in ((self.logbox, 5000), (self.recent, 200)):
                    box.configure(state="normal")
                    box.insert("end", msg + "\n")
                    lines = int(box.index("end-1c").split(".")[0])
                    if lines > limit:
                        box.delete("1.0", f"{lines - limit}.0")
                    box.see("end")
                    box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    def _check_components(self):
        missing = [f"  - {s.name}: {s.exe}" for s in self.services if not s.available]
        if not pma_ready():
            missing.append(f"  - phpMyAdmin: {PMA}{os.sep}index.php")
            if os.path.isdir(PMA):
                subs = [d for d in os.listdir(PMA)
                        if os.path.isfile(os.path.join(PMA, d, "index.php"))]
                if subs:
                    missing.append(f"    (phpMyAdmin ada di subfolder '{subs[0]}' — pindahkan isinya "
                                   f"langsung ke {PMA})")
        if missing:
            self.log("Komponen belum terpasang:\n" + "\n".join(missing)
                     + "\n  Buka halaman Komponen untuk memasangnya otomatis.")

    # ---------- versi PHP ----------
    def set_php(self, rel):
        """Ganti versi PHP aktif: php_dir, path_extra, dan cwd layanan yang memakai folder PHP lama."""
        global PHP
        names = switch_php_config(CFG, rel)  # s.cfg adalah objek yang sama dengan CFG["services"]
        affected = [s for s in self.services if s.name in names]
        PHP = os.path.normpath(os.path.join(BASE, rel))
        save_config(CFG)
        self.log(f"Versi PHP diganti ke {rel}.")

        running = [s for s in self.services if s.running and (s in affected or s.name == "Apache")]
        if running:
            self.log("Restart " + ", ".join(s.name for s in running) + " untuk memakai PHP baru…")

            def restart():
                for s in running:
                    s.stop()
                for s in running:
                    s.start()
            threading.Thread(target=restart, daemon=True).start()
        else:
            threading.Thread(target=self.prepare_php, daemon=True).start()
        self.ui(self.refresh_php_versions)

    # ---------- unduh komponen ----------
    def ui(self, fn):
        """Jalankan fn di thread GUI (aman dipanggil dari thread mana pun)."""
        self.logq.put(fn)

    # ---------- halaman Komponen: pasang & perbarui ----------
    def _page_components(self, page):
        self._header(page, "Komponen", "Pasang dan perbarui komponen langsung dari situs resminya.",
                     [("Periksa pembaruan", self.check_updates, True)])
        self.comp_status = tk.Label(page, text="Belum diperiksa.", font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w")
        self.comp_status.pack(fill="x", pady=(0, 10))
        self.comp_list = tk.Frame(page, bg=C_BG)
        self.comp_list.pack(fill="x")
        self.comp_latest = {}
        self.comp_errors = {}
        self._checking = False

    def _comp_card(self, title, desc):
        card = self._card(self.comp_list, fill="x", pady=(0, 10))
        head = tk.Frame(card, bg=C_BG)
        head.pack(fill="x", padx=16, pady=(12, 10))
        txt = tk.Frame(head, bg=C_BG)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=title, font=F_BOLD, bg=C_BG, fg=C_TEXT, anchor="w").pack(fill="x")
        tk.Label(txt, text=desc, font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(fill="x")
        right = tk.Frame(head, bg=C_BG)
        right.pack(side="right")
        return card, right

    def _comp_state(self, parent, installed, latest, on_install, on_update, install_text="Pasang"):
        """Status + tombol untuk satu komponen/versi: Pasang / Perbarui / Terbaru."""
        busy = self._downloading
        if not installed:
            tk.Label(parent, text="Belum terpasang", font=F_SMALL, bg=C_BG, fg=C_GRAY).pack(side="left", padx=10)
            ttk.Button(parent, text=install_text, style="Accent.TButton", command=on_install,
                       state="disabled" if busy else "normal").pack(side="left")
        elif latest and vtuple(latest) > vtuple(installed):
            tk.Label(parent, text=f"{installed}  →  {latest}", font=F_SMALL, bg=C_BG, fg=C_ACCENT).pack(
                side="left", padx=10)
            ttk.Button(parent, text="Perbarui", style="Accent.TButton", command=on_update,
                       state="disabled" if busy else "normal").pack(side="left")
        elif latest:
            tk.Label(parent, text=installed, font=F_SMALL, bg=C_BG, fg=C_MUTED).pack(side="left", padx=10)
            tk.Label(parent, text="  Terbaru  ", font=F_SMALL, bg=C_GREEN_SOFT, fg="#027a48").pack(side="left")
        else:
            tk.Label(parent, text=f"Terpasang  {installed}", font=F_SMALL, bg=C_BG, fg=C_MUTED).pack(side="left")

    def refresh_components(self):
        for w in self.comp_list.winfo_children():
            w.destroy()
        L = self.comp_latest

        # Apache
        _, right = self._comp_card("Apache", "Web server · build Apache Lounge (x64)")
        self._comp_state(right, installed_version("apache"), L.get("apache"),
                         lambda: self.start_job("Apache", lambda d, log, p: install_apache(d, log, True, p),
                                                stop=["Apache"]),
                         lambda: self.start_job("Apache", lambda d, log, p: install_apache(d, log, True, p),
                                                stop=["Apache"]))

        # PHP: satu baris per versi terpasang + pasang cabang lain
        card, right = self._comp_card("PHP", "Thread Safe x64 · bisa lebih dari satu versi")
        releases = L.get("php") or {}
        php_rows = [(rel, label.split()[0]) for rel, label in find_php_versions()]
        installed_vers = {v for _, v in php_rows}
        for rel, ver in php_rows:
            row = tk.Frame(card, bg=C_BG)
            row.pack(fill="x", padx=16, pady=(0, 8))
            active = same_path(os.path.join(BASE, rel), PHP)
            tk.Label(row, text=f"PHP {ver}" + ("   (aktif)" if active else ""), font=F_BODY, bg=C_BG,
                     fg=C_TEXT, anchor="w").pack(side="left")
            latest = (releases.get(branch_of(ver)) or {}).get("version")
            if latest and latest in installed_vers and latest != ver:
                tk.Label(row, text=f"versi lebih baru ({latest}) sudah terpasang", font=F_SMALL, bg=C_BG,
                         fg=C_MUTED).pack(side="right")
                continue
            right_row = tk.Frame(row, bg=C_BG)
            right_row.pack(side="right")
            self._comp_state(right_row, ver, latest, None,
                             lambda b=branch_of(ver), r=rel: self.start_job(
                                 f"PHP {b}", lambda d, log, p: self._job_php(d, log, p, b, r)))
        if not php_rows:
            tk.Label(right, text="Belum terpasang", font=F_SMALL, bg=C_BG, fg=C_GRAY).pack(side="left")
        add = tk.Frame(card, bg=C_BG)
        add.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(add, text="Pasang versi PHP:", font=F_SMALL, bg=C_BG, fg=C_MUTED).pack(side="left")
        branches = sorted(releases, key=vtuple, reverse=True)
        php_var = tk.StringVar(value=next((b for b in branches
                                           if releases[b]["version"] not in installed_vers), branches[0])
                               if branches else "terbaru")
        ttk.Combobox(add, textvariable=php_var, width=10, state="readonly",
                     values=branches or ["terbaru"]).pack(side="left", padx=8)
        ttk.Button(add, text="Pasang", style="Small.TButton",
                   state="disabled" if self._downloading else "normal",
                   command=lambda: self.start_job(
                       f"PHP {php_var.get()}",
                       lambda d, log, p, b=php_var.get(): self._job_php(d, log, p, None if b == "terbaru" else b))
                   ).pack(side="left")

        # MariaDB: pembaruan hanya di cabang yang sama (pindah cabang butuh migrasi data)
        card, right = self._comp_card("MariaDB", "Database (kompatibel MySQL) · data di bin/mysql/data tetap aman")
        m = L.get("mariadb") or {}
        inst = installed_version("mariadb")
        if inst:
            self._comp_state(right, inst, (m.get("latest") or {}).get(branch_of(inst)), None,
                             lambda b=branch_of(inst): self.start_job(
                                 f"MariaDB {b}", lambda d, log, p: install_mariadb(d, log, b, True, p),
                                 stop=["MariaDB"]))
            tk.Label(card, text=f"Pembaruan otomatis hanya di cabang {branch_of(inst)}. Pindah ke cabang lain "
                                f"perlu migrasi data dan tidak dilakukan otomatis.",
                     font=F_SMALL, bg=C_BG, fg=C_MUTED, anchor="w").pack(fill="x", padx=16, pady=(0, 12))
        else:
            stable, lts = m.get("stable") or [], m.get("lts")
            db_var = tk.StringVar(value=lts or "LTS terbaru")
            ttk.Combobox(right, textvariable=db_var, width=10, state="readonly",
                         values=stable or ["LTS terbaru"]).pack(side="left", padx=(0, 8))
            self._comp_state(right, None, None,
                             lambda: self.start_job(
                                 "MariaDB", lambda d, log, p, b=db_var.get(): install_mariadb(
                                     d, log, None if b == "LTS terbaru" else b, True, p), stop=["MariaDB"]),
                             None)

        # phpMyAdmin
        _, right = self._comp_card("phpMyAdmin", "Kelola database lewat browser · config login tetap dipakai")
        job = lambda: self.start_job("phpMyAdmin", lambda d, log, p: install_phpmyadmin(d, log, True, p))
        self._comp_state(right, installed_version("phpmyadmin"), L.get("phpmyadmin"), job, job)

        # Visual C++ Redistributable (dibutuhkan semua komponen di atas)
        card, right = self._comp_card("Visual C++ Redistributable x64",
                                      "Dibutuhkan Apache, PHP, dan MariaDB · dipasang lewat installer Microsoft")
        if vc_redist_missing():
            tk.Label(right, text="Belum terpasang", font=F_SMALL, bg=C_BG, fg=C_RED).pack(side="left", padx=10)
            ttk.Button(right, text="Unduh installer", style="Accent.TButton",
                       command=lambda: webbrowser.open(VC_REDIST)).pack(side="left")
        else:
            tk.Label(right, text="  Terpasang  ", font=F_SMALL, bg=C_GREEN_SOFT, fg="#027a48").pack(side="left")

        for name, err in self.comp_errors.items():
            tk.Label(self.comp_list, text=f"Gagal memeriksa {COMPONENT_LABELS[name]}: {err}", font=F_SMALL,
                     bg=C_BG, fg=C_RED, anchor="w", wraplength=self.px(760), justify="left").pack(fill="x")

    def _job_php(self, dl_dir, log, progress, branch, old_rel=None):
        """Pasang PHP satu cabang; bila old_rel (versi lama) sedang aktif, pindah ke versi baru."""
        installed = install_php(dl_dir, log, [branch] if branch else None, progress=progress)
        if not installed:
            return False
        new_rel = installed[0][1]
        if not os.path.isfile(os.path.join(PHP, "php" + EXE)) or (old_rel and same_path(
                os.path.join(BASE, old_rel), PHP)):
            self.set_php(new_rel)
        if old_rel and not same_path(os.path.join(BASE, old_rel), os.path.join(BASE, new_rel)):
            log(f"Versi lama tetap ada di {old_rel}; hapus foldernya bila tidak diperlukan lagi.")
        return True

    def check_updates(self):
        if self._checking:
            return
        self._checking = True
        self.comp_status.configure(text="Memeriksa versi terbaru…", fg=C_ACCENT)

        def work():
            latest, errors = {}, {}
            for name, fn in (("apache", lambda: latest_apache()[0]), ("php", php_releases),
                             ("phpmyadmin", lambda: pma_latest()[0])):
                try:
                    latest[name] = fn()
                except Exception as e:
                    errors[name] = str(e)
            try:
                stable, lts = mariadb_branches()
                inst = installed_version("mariadb")
                wanted = {lts} | ({branch_of(inst)} if inst else set())
                latest["mariadb"] = {"stable": stable, "lts": lts,
                                     "latest": {b: mariadb_latest(b)[0] for b in wanted if b in stable}}
            except Exception as e:
                errors["mariadb"] = str(e)
            self.ui(lambda: self._updates_checked(latest, errors))

        threading.Thread(target=work, daemon=True).start()

    def _updates_checked(self, latest, errors):
        self._checking = False
        self.comp_latest, self.comp_errors = latest, errors
        self.refresh_components()
        n = self._count_updates()
        when = time.strftime("%H:%M")
        if errors:
            self.comp_status.configure(text=f"Diperiksa pukul {when}; sebagian gagal diperiksa (lihat di bawah).",
                                       fg=C_RED)
        elif not any(component_installed(c) for c in COMPONENTS):
            self.comp_status.configure(text=f"Diperiksa pukul {when}. Belum ada komponen terpasang — "
                                            f"klik Pasang pada komponen yang dibutuhkan.", fg=C_MUTED)
        else:
            self.comp_status.configure(
                text=f"Diperiksa pukul {when}. " + (f"{n} pembaruan tersedia." if n else "Semua sudah terbaru."),
                fg=C_ACCENT if n else C_MUTED)

    def _count_updates(self):
        L, n = self.comp_latest, 0
        for name in ("apache", "phpmyadmin"):
            inst = installed_version(name)
            n += bool(inst and L.get(name) and inst != "?" and vtuple(L[name]) > vtuple(inst))
        inst = installed_version("mariadb")
        latest = ((L.get("mariadb") or {}).get("latest") or {}).get(branch_of(inst)) if inst else None
        n += bool(latest and vtuple(latest) > vtuple(inst))
        vers = {label.split()[0] for _, label in find_php_versions()}
        for ver in vers:
            latest = ((L.get("php") or {}).get(branch_of(ver)) or {}).get("version")
            n += bool(latest and latest not in vers and vtuple(latest) > vtuple(ver))
        return n

    def start_job(self, title, fn, stop=()):
        """Jalankan pemasangan/pembaruan di latar belakang. Layanan di `stop` yang sedang jalan
        dihentikan dulu (file exe yang berjalan terkunci) lalu dinyalakan lagi setelah selesai."""
        if self._downloading:
            messagebox.showinfo("Komponen", "Masih ada proses yang berjalan — lihat panel di bagian bawah.")
            return
        running = [s for s in self.services if s.name in stop and s.running]
        if running and not messagebox.askyesno(
                "Komponen", f"{', '.join(s.name for s in running)} sedang berjalan dan perlu dihentikan "
                            f"sementara. Lanjutkan? (akan dinyalakan lagi setelah selesai)"):
            return
        self._downloading = True
        self._dl_show(f"Menyiapkan {title}…", "")
        if self.current_page == "components":
            self.refresh_components()  # nonaktifkan tombol selama proses
        threading.Thread(target=self._run_job, args=(title, fn, running), daemon=True).start()

    def _run_job(self, title, fn, restart):
        dl_dir = os.path.join(BASE, "bin", "_unduhan")
        state = {"pct": -1}

        def dlog(msg):
            """Log biasa + judul panel progres untuk tahap-tahap penting."""
            self.log(msg)
            if msg.startswith(("Mencari", "Mengambil", "Mengunduh", "Mengekstrak")):
                state["pct"] = -1
                indeterminate = not msg.startswith("Mengunduh")
                self.ui(lambda: self._dl_show(msg, title, None if indeterminate else 0))

        def progress(done, total):
            pct = done * 100 // total if total else -1
            if pct == state["pct"]:
                return  # perbarui UI hanya bila persen berubah
            state["pct"] = pct
            size = f"{done / 1048576:.1f} / {total / 1048576:.1f} MB" if total else f"{done / 1048576:.1f} MB"
            detail = f"{title}  ·  {size}" + (f"  ·  {pct}%" if total else "")
            self.ui(lambda: self._dl_progress(pct, detail))

        ok, error = False, ""
        try:
            for s in restart:
                self.log(f"Menghentikan {s.name} sementara…")
                s.stop()
            os.makedirs(dl_dir, exist_ok=True)
            self.log(f"=== {title} ===")
            ok = bool(fn(dl_dir, dlog, progress))
        except Exception as e:  # jaringan putus, server menolak, checksum salah, dsb.
            error = str(e)
            self.log(f"  Gagal: {e}")
        finally:
            shutil.rmtree(dl_dir, ignore_errors=True)
            for s in restart:
                self.log(f"Menyalakan lagi {s.name}…")
                s.start()
            self._downloading = False

        if ok:
            head, detail = f"{title} selesai", "Siap dipakai."
            if vc_redist_missing():
                detail = "Pasang Visual C++ Redistributable x64 (halaman Komponen) agar bisa berjalan."
        else:
            head, detail = f"{title} gagal", (error[:160] + "  ·  " if error else "") + "Lihat log untuk detail."
        self.log(head)
        self.ui(lambda: self._dl_done(ok, head, detail))
        if self.tray:
            self.tray.notify(head, detail)
        self.ui(self.refresh_php_versions)
        self.ui(self.refresh_sites)
        self.ui(lambda: self.current_page == "components" and self.refresh_components())

    # --- panel progres unduhan (selalu terlihat di bawah halaman mana pun) ---
    def _build_download_banner(self):
        bar = tk.Frame(self.content, bg="#f8f9fc", highlightthickness=1, highlightbackground=C_BORDER)
        bar.grid(row=1, column=0, sticky="ew", padx=32, pady=(0, 20))
        icon = tk.Label(bar, text="", font=(ICON_FONT, 16), bg="#f8f9fc", fg=C_ACCENT)
        icon.pack(side="left", padx=(16, 12), pady=12)
        right = tk.Frame(bar, bg="#f8f9fc")
        right.pack(side="right", padx=(8, 12))
        self.dl_close = tk.Label(right, text="", font=(ICON_FONT, 10), bg="#f8f9fc", fg=C_MUTED,
                                 cursor="hand2")
        self.dl_close.bind("<Button-1>", lambda _e: self.dl_banner.grid_remove())
        ttk.Button(right, text="Lihat log", style="Small.TButton",
                   command=lambda: self.show_page("log")).pack(side="left")
        body = tk.Frame(bar, bg="#f8f9fc")
        body.pack(side="left", fill="x", expand=True, pady=10)
        self.dl_title = tk.Label(body, text="", font=F_BOLD, bg="#f8f9fc", fg=C_TEXT, anchor="w")
        self.dl_title.pack(fill="x")
        self.dl_detail = tk.Label(body, text="", font=F_SMALL, bg="#f8f9fc", fg=C_MUTED, anchor="w")
        self.dl_detail.pack(fill="x")
        self.dl_bar = ttk.Progressbar(body, style="Accent.Horizontal.TProgressbar", maximum=100)
        self.dl_bar.pack(fill="x", pady=(6, 0))
        self.dl_icon = icon
        self.dl_banner = bar
        self._dl_hide_job = None
        bar.grid_remove()

    def _dl_show(self, title, detail="", value=None, icon=""):
        """Tampilkan panel progres; value None = animasi berjalan (lama belum diketahui)."""
        if self._dl_hide_job:
            self.after_cancel(self._dl_hide_job)
            self._dl_hide_job = None
        self.dl_banner.grid()
        self.dl_close.pack_forget()
        self.dl_icon.configure(text=icon, fg=C_ACCENT)
        self.dl_title.configure(text=title, fg=C_TEXT)
        self.dl_detail.configure(text=detail)
        self.dl_bar.pack(fill="x", pady=(6, 0))
        if value is None:
            if str(self.dl_bar.cget("mode")) != "indeterminate":
                self.dl_bar.configure(mode="indeterminate")
                self.dl_bar.start(12)
        else:
            self.dl_bar.stop()
            self.dl_bar.configure(mode="determinate", value=value)

    def _dl_progress(self, pct, detail):
        if str(self.dl_bar.cget("mode")) != "determinate":
            self.dl_bar.stop()
            self.dl_bar.configure(mode="determinate")
        if pct >= 0:
            self.dl_bar.configure(value=pct)
        self.dl_detail.configure(text=detail)

    def _dl_done(self, ok, title, detail):
        self.dl_banner.grid()
        self.dl_bar.stop()
        self.dl_bar.pack_forget()
        self.dl_icon.configure(text="" if ok else "", fg=C_GREEN if ok else C_RED)
        self.dl_title.configure(text=title, fg=C_TEXT)
        self.dl_detail.configure(text=detail)
        self.dl_close.pack(side="left", padx=(10, 0))
        if ok:  # pesan sukses hilang sendiri; pesan gagal tetap sampai ditutup
            self._dl_hide_job = self.after(15000, self.dl_banner.grid_remove)

    # ---------- helper layanan ----------
    def svc(self, name):
        return next((s for s in self.services if s.name == name), None)

    def port_of(self, name, default):
        s = self.svc(name)
        return (s.cfg.get("port") if s else None) or default

    # ---------- layanan ----------
    def _run_service(self, svc, action):
        """Start/stop di thread terpisah; status 'Memulai…/Menghentikan…' tampil selama proses."""
        if svc.name in self._pending:
            return
        self._pending[svc.name] = action

        def work():
            try:
                svc.start() if action == "start" else svc.stop()
            finally:
                self._pending.pop(svc.name, None)
        threading.Thread(target=work, daemon=True).start()

    def toggle(self, svc):
        self._run_service(svc, "stop" if svc.running else "start")

    def start_all(self):
        names = CFG.get("autostart", ["Apache", "MariaDB"])
        for s in self.services:
            if s.name in names and s.available and not s.running:
                self._run_service(s, "start")

    def stop_all(self):
        for s in self.services:
            if s.running:
                self._run_service(s, "stop")

    def on_close(self):
        """Tombol X: sembunyikan ke tray (layanan tetap jalan); tanpa tray langsung keluar."""
        if not self.tray:
            self.quit_app()
            return
        self.withdraw()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(APP_ID, "Localizer tetap berjalan di sini. Klik kanan ikon untuk menu atau keluar.")

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def toggle_autostart(self):
        want = self.autostart_var.get()
        try:
            self.log(set_autostart(want))
        except OSError as e:
            self.log(f"Gagal mengatur jalan otomatis: {e}")
        self.autostart_var.set(autostart_enabled())

    def quit_app(self):
        if self._closing:
            return
        if any(s.running for s in self.services):
            self.show_window()  # dialog butuh jendela yang terlihat
            if not messagebox.askyesno("Keluar", "Masih ada layanan berjalan. Hentikan semua dan keluar?"):
                return
        self._closing = True
        self.title("Localizer — menghentikan layanan…")
        self.log("Menghentikan semua layanan…")
        threads = [threading.Thread(target=s.stop, daemon=True) for s in self.services if s.running]
        for t in threads:
            t.start()
        self._wait_close(threads)

    def _wait_close(self, threads):
        # menunggu tanpa membekukan jendela
        if any(t.is_alive() for t in threads):
            self.after(100, lambda: self._wait_close(threads))
        else:
            if self.tray:
                self.tray.stop()
            self.destroy()

    # ---------- persiapan sebelum start ----------
    def prepare_php(self):
        """Buat php.ini bila belum ada dan aktifkan ekstensi yang dibutuhkan (Laravel, phpMyAdmin)."""
        if not os.path.isdir(PHP):
            return True
        ini = os.path.join(PHP, "php.ini")
        if os.path.isfile(ini):
            text = orig = read_text(ini)
        else:
            src = next((n for n in ("php.ini-development", "php.ini-production")
                        if os.path.isfile(os.path.join(PHP, n))), None)
            if not src:
                return True
            text, orig = read_text(os.path.join(PHP, src)), ""
            self.log(f"[PHP] php.ini dibuat dari {src}.")

        ext_dir = os.path.join(PHP, "ext")
        enabled = []
        if IS_WIN and os.path.isdir(ext_dir):
            # path absolut agar tetap benar saat dijalankan dari Apache (cwd berbeda)
            line = f'extension_dir = "{fwd(ext_dir)}"'
            text, n = re.subn(r'^\s*extension_dir\s*=.*$', lambda m: line, text, flags=re.M)
            if not n:
                text, n = re.subn(r'^;\s*extension_dir\s*=\s*"ext"\s*$', lambda m: line, text,
                                  count=1, flags=re.M)
            if not n:
                text = text.rstrip() + f"\n{line}\n"
            for ext in CFG.get("php_extensions", []):
                if not os.path.isfile(os.path.join(ext_dir, f"php_{ext}.dll")):
                    continue
                e = re.escape(ext)
                if re.search(rf'^\s*extension\s*=\s*"?(php_)?{e}(\.dll)?"?\s*$', text, re.M | re.I):
                    continue
                text, n = re.subn(rf'^\s*;\s*(extension\s*=\s*"?(php_)?{e}(\.dll)?"?)\s*$', r"\1",
                                  text, count=1, flags=re.M | re.I)
                if not n:
                    text = text.rstrip() + f"\nextension={ext}\n"
                enabled.append(ext)
        if text != orig:
            write_text(ini, text)
            if enabled:
                self.log(f"[PHP] Ekstensi diaktifkan: {', '.join(enabled)}")
        return True

    def prepare_phpmyadmin(self):
        if not pma_ready():
            return
        cfg = os.path.join(PMA, "config.inc.php")
        if os.path.isfile(cfg):
            return
        port = self.port_of("MariaDB", 3306)
        write_text(cfg, (
            "<?php\n"
            "// Dibuat otomatis oleh Localizer\n"
            f"$cfg['blowfish_secret'] = '{secrets.token_hex(16)}';\n"
            "$i = 1;\n"
            "$cfg['Servers'][$i]['auth_type'] = 'cookie';\n"
            "$cfg['Servers'][$i]['host'] = '127.0.0.1';\n"
            f"$cfg['Servers'][$i]['port'] = '{port}';\n"
            "$cfg['Servers'][$i]['AllowNoPassword'] = true;\n"
            "$cfg['TempDir'] = __DIR__ . '/tmp';\n"))
        os.makedirs(os.path.join(PMA, "tmp"), exist_ok=True)
        self.log("[phpMyAdmin] config.inc.php dibuat (login: root, password kosong).")

    def prepare_mariadb(self):
        """Inisialisasi folder data MariaDB saat pertama kali dijalankan."""
        data = os.path.join(MYSQL, "data")
        if os.path.isdir(data) and os.listdir(data):
            return True
        tool = next((p for p in (os.path.join(MYSQL, "bin", n + EXE)
                                 for n in ("mariadb-install-db", "mysql_install_db"))
                     if os.path.isfile(p)), None)
        if not tool:
            self.log("[MariaDB] Folder data belum ada dan mariadb-install-db tidak ditemukan; "
                     "mysqld mungkin gagal start.")
            return True
        if os.path.isdir(data):
            os.rmdir(data)  # kosong; install-db menolak folder yang sudah ada
        self.log("[MariaDB] Inisialisasi database pertama kali…")
        r = subprocess.run([tool, f"--datadir={data}"], cwd=os.path.dirname(tool),
                           capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                self.log(f"[MariaDB] {line}")
        if r.returncode != 0:
            self.log(f"[MariaDB] Inisialisasi gagal (kode {r.returncode}).")
            return False
        self.log("[MariaDB] Database siap. User: root, password: (kosong).")
        return True

    def prepare_apache(self):
        httpd_conf = os.path.join(APACHE, "conf", "httpd.conf")
        if not os.path.isfile(httpd_conf):
            self.log(f"[Apache] {httpd_conf} tidak ditemukan.")
            return True
        os.makedirs(os.path.join(APACHE, "logs"), exist_ok=True)  # Apache gagal start tanpa folder ini
        self.prepare_php()
        self.prepare_phpmyadmin()
        vhosts = self.write_apache_vhosts(self.collect_sites())

        text = orig = read_text(httpd_conf)
        root = fwd(APACHE)
        # ServerRoot portabel (Apache Lounge memakai "Define SRVROOT c:/Apache24")
        if re.search(r"^\s*Define\s+SRVROOT\b", text, re.M):
            text = re.sub(r"^(\s*Define\s+SRVROOT\s+).*$", lambda m: f'{m.group(1)}"{root}"',
                          text, count=1, flags=re.M)
        else:
            text = re.sub(r"^(\s*ServerRoot\s+).*$", lambda m: f'{m.group(1)}"{root}"',
                          text, count=1, flags=re.M)
        # mod_rewrite dibutuhkan .htaccess Laravel
        text = re.sub(r"^#\s*(LoadModule\s+rewrite_module\s)", r"\1", text, flags=re.M)
        # baris Include dari versi lama Localizer
        text = re.sub(r"^\s*Include\s+conf/extra/(?:minilaragon|localizer)-vhosts\.conf\s*\n?",
                      "", text, flags=re.M)

        # port Listen mengikuti config.json (hanya baris Listen pertama, agar tidak jadi duplikat)
        port = self.port_of("Apache", 80)
        outside = strip_block(text)
        if re.search(r"^\s*Listen\s", outside, re.M):
            text = re.sub(r"^(\s*Listen\s+(?:\d{1,3}(?:\.\d{1,3}){3}:|\[[^\]]*\]:)?)\d+(\s*)$",
                          lambda m: f"{m.group(1)}{port}{m.group(2)}", text, count=1, flags=re.M)
            outside = strip_block(text)
        lines = []
        if not re.search(r"^\s*Listen\s", outside, re.M):
            lines.append(f"Listen {port}")
        if not re.search(r"^\s*ServerName\s", outside, re.M):
            lines.append(f"ServerName localhost:{port}")
        mod = php_apache_module()
        if mod:
            if not re.search(r"^\s*LoadModule\s+php\d?_module", outside, re.M):
                lines.append(f'LoadModule {mod[0]} "{fwd(mod[1])}"')
            lines += [f'PHPIniDir "{fwd(PHP)}"',
                      '<FilesMatch "\\.php$">',
                      "  SetHandler application/x-httpd-php",
                      "</FilesMatch>",
                      "DirectoryIndex index.php index.html"]
        else:
            self.log("[Apache] Modul PHP (php8apache2_4.dll) tidak ada di folder PHP — file .php tidak "
                     "akan dijalankan. Gunakan PHP versi 'Thread Safe' dari windows.php.net.")
        if vhosts:
            lines.append(f'Include "{fwd(vhosts)}"')
        text = with_block(text, "\n".join(lines))
        if text != orig:
            write_text(httpd_conf, text)
            self.log("[Apache] httpd.conf diperbarui.")
        return True

    def prepare_nginx(self):
        if not self.configure_nginx():
            return True
        cgi = self.svc("PHP-CGI")
        if cgi and cgi.available:
            if not cgi.running:
                self.log("[Nginx] Menyalakan PHP-CGI untuk memproses PHP…")
                cgi.start()
        else:
            self.log("[Nginx] PHP-CGI (php-cgi.exe) tidak tersedia; file .php tidak akan jalan.")
        return True

    def configure_nginx(self):
        """Tulis situs dan pastikan nginx.conf memuatnya. False bila Nginx belum terpasang."""
        main = os.path.join(NGINX, "conf", "nginx.conf")
        if not os.path.isfile(main):
            self.log(f"[Nginx] {main} tidak ditemukan.")
            return False
        self.prepare_php()
        self.prepare_phpmyadmin()
        self.write_nginx_sites(self.collect_sites())

        text = orig = read_text(main)
        text = text.replace("include minilaragon-sites.conf;", "include localizer-sites.conf;")  # nama lama
        if "localizer-sites.conf" not in text:
            text, n = re.subn(r"^(\s*http\s*\{[^\n]*\n)",
                              lambda m: m.group(1) + "    include localizer-sites.conf;\n",
                              text, count=1, flags=re.M)
            if not n:
                self.log("[Nginx] Blok 'http {' tidak ditemukan di nginx.conf; situs tidak dimuat.")
        port = self.port_of("Nginx", 8080)
        if port != 80:  # server bawaan nginx (port 80) akan bentrok dengan Apache
            text = re.sub(r"^(\s*listen\s+)80(\s*;)", lambda m: f"{m.group(1)}{port}{m.group(2)}",
                          text, flags=re.M)
        if text != orig:
            write_text(main, text)
            self.log("[Nginx] nginx.conf diperbarui.")
        return True

    # ---------- menu cepat ----------
    def web_url(self, path=""):
        """URL server web yang sedang berjalan (Apache diutamakan), atau None."""
        for name, default in (("Apache", 80), ("Nginx", 8080)):
            s = self.svc(name)
            if s and s.running:
                port = self.port_of(name, default)
                return f"http://localhost{'' if port == 80 else f':{port}'}/{path}"
        return None

    def open_browser(self):
        url = self.web_url()
        if not url:
            self.log("Apache/Nginx belum berjalan — klik Start dulu.")
            return
        webbrowser.open(url)

    def open_phpmyadmin(self):
        if not pma_ready():
            messagebox.showinfo(
                "phpMyAdmin belum ada",
                "Unduh phpMyAdmin (.zip 'all-languages') dari\nhttps://www.phpmyadmin.net/downloads/\n\n"
                f"lalu ekstrak isinya ke:\n{PMA}\n\n"
                "Pastikan index.php berada langsung di folder itu, bukan di subfolder.")
            return
        url = self.web_url("phpmyadmin/")
        if not url:
            self.log("Jalankan Apache (atau Nginx) dan MariaDB dulu, lalu klik phpMyAdmin lagi.")
            return
        if not os.path.isfile(os.path.join(PMA, "config.inc.php")):
            # phpMyAdmin baru diekstrak saat server sudah jalan: siapkan & restart server dulu
            self.prepare_phpmyadmin()
            self.sync_vhosts()
            self.log("phpMyAdmin baru disiapkan; tunggu server restart lalu klik phpMyAdmin lagi.")
            return
        db = self.svc("MariaDB")
        if db and not db.running:
            self.log("Catatan: MariaDB belum berjalan — login phpMyAdmin akan gagal.")
        webbrowser.open(url)

    @staticmethod
    def open_path(path):
        """Buka file/folder dengan aplikasi bawaan sistem."""
        path = os.path.normpath(path)
        if IS_WIN:
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def open_www(self):
        self.open_path(WWW)

    def open_terminal(self, folder=None):
        env = tool_env()
        cwd = os.path.normpath(folder or WWW)
        if IS_WIN:
            subprocess.Popen(["cmd", "/k"], cwd=cwd, env=env, creationflags=NEW_CONSOLE)
        else:
            subprocess.Popen(["x-terminal-emulator"], cwd=cwd, env=env)

    def edit_config(self):
        self.open_path(CONFIG_PATH)

    # ---------- proyek baru ----------
    def new_project(self):
        name = simpledialog.askstring("Proyek Baru", "Nama proyek (tanpa spasi):", parent=self)
        if not name:
            return
        name = to_hostname(name.strip())
        if not name:
            messagebox.showerror("Gagal", "Nama proyek tidak valid.")
            return
        path = os.path.join(WWW, name)
        if os.path.exists(path):
            messagebox.showerror("Gagal", f"Folder '{name}' sudah ada.")
            return
        if messagebox.askyesno("Template", "Buat dengan Laravel (butuh composer di PATH)?\n\n"
                                           "Pilih 'No' untuk proyek PHP kosong."):
            threading.Thread(target=self._create_laravel, args=(name,), daemon=True).start()
        else:
            os.makedirs(path)
            write_text(os.path.join(path, "index.php"),
                       f"<?php\necho '<h1>{name}</h1><p>PHP ' . PHP_VERSION . '</p>';\n")
            self.log(f"Proyek '{name}' dibuat.")
            self.sync_vhosts()

    def _create_laravel(self, name):
        self.log(f"Membuat proyek Laravel '{name}' (bisa beberapa menit)…")
        self.prepare_php()  # composer memakai php.exe dari bin/php (lewat PATH)
        try:
            p = subprocess.Popen(["composer", "create-project", "laravel/laravel", name],
                                 cwd=WWW, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, errors="replace", creationflags=NO_WINDOW,
                                 shell=IS_WIN, env=tool_env())
            for line in p.stdout:
                self.log(f"[composer] {line.rstrip()}")
            rc = p.wait()
        except OSError as e:
            self.log(f"Composer tidak bisa dijalankan: {e}")
            return
        if rc != 0:
            self.log(f"Composer gagal (kode {rc}). Periksa log di atas; pastikan composer terpasang "
                     f"dan ada di PATH.")
            return
        self.log(f"Proyek Laravel '{name}' selesai dibuat.")
        self.sync_vhosts()

    # ---------- virtual host ----------
    def collect_sites(self, verbose=False):
        """Daftar Site dari subfolder www, digabung dengan pengaturan per situs di config.json
        (CFG["sites"][nama_folder] = {"aliases": [...], "root": "...", "apache": "...", "nginx": "..."})."""
        tld = CFG.get("tld", "test")
        site_cfg = CFG.get("sites", {})
        primaries, used = [], set()
        for d in sorted(os.listdir(WWW)):
            full = os.path.join(WWW, d)
            if not os.path.isdir(full) or d.startswith("."):
                continue
            host = to_hostname(d)
            if not host:
                if verbose:
                    self.log(f"Folder '{d}' dilewati: nama tidak bisa dijadikan domain.")
                continue
            domain = f"{host}.{tld}"
            if domain in used:
                if verbose:
                    self.log(f"Folder '{d}' dilewati: domain {domain} sudah dipakai folder lain.")
                continue
            if verbose and host != d.lower():
                self.log(f"Folder '{d}' → {domain} (nama disesuaikan agar jadi domain valid).")
            used.add(domain)
            primaries.append((d, domain, full))

        # domain tambahan diproses setelah semua domain utama, agar domain utama selalu menang
        sites = []
        for d, domain, full in primaries:
            sc = site_cfg.get(d, {})
            aliases = []
            for a in sc.get("aliases", []):
                if a in used:
                    if verbose:
                        self.log(f"Domain tambahan {a} untuk '{d}' dilewati: sudah dipakai situs lain.")
                    continue
                used.add(a)
                aliases.append(a)
            root = site_root(full, sc.get("root", ""))
            if sc.get("root") and root is None:
                if verbose:
                    self.log(f"Folder root '{sc['root']}' untuk '{d}' tidak ditemukan; memakai bawaan.")
            sites.append(Site(d, domain, fwd(root or site_root(full, "")), fwd(full), aliases,
                              sc.get("apache", ""), sc.get("nginx", "")))
        return sites

    @staticmethod
    def _apache_vhost(host, root, port, indexes=False, aliases=(), extra=""):
        opts = "Indexes FollowSymLinks" if indexes else "FollowSymLinks"
        names = ["127.0.0.1"] if host == "localhost" else list(aliases)
        alias = f"  ServerAlias {' '.join(names)}\n" if names else ""
        extra_block = ""
        if extra.strip():
            extra_block = "  # Konfigurasi tambahan (Pengaturan situs)\n" + \
                "".join(f"  {line}\n" for line in extra.strip().splitlines())
        return (f'<VirtualHost *:{port}>\n  ServerName {host}\n{alias}  DocumentRoot "{root}"\n'
                f'  <Directory "{root}">\n    Options {opts}\n    AllowOverride All\n'
                f'    Require all granted\n  </Directory>\n{extra_block}</VirtualHost>\n')

    def write_apache_vhosts(self, sites):
        """Tulis conf/extra/localizer-vhosts.conf; kembalikan path-nya (None jika Apache belum ada)."""
        if not os.path.isdir(os.path.join(APACHE, "conf")):
            return None
        port = self.port_of("Apache", 80)
        parts = ["# Dibuat otomatis oleh Localizer - perubahan manual akan ditimpa.\n"
                 "# Ubah lewat halaman Situs -> Pengaturan situs.\n"]
        if pma_ready():
            pma = fwd(PMA)
            parts.append(f'Alias /phpmyadmin "{pma}"\n<Directory "{pma}">\n  Options FollowSymLinks\n'
                         f'  DirectoryIndex index.php\n  AllowOverride All\n  Require all granted\n'
                         f'</Directory>\n')
        # vhost pertama = default: http://localhost menampilkan folder www
        parts.append(self._apache_vhost("localhost", fwd(WWW), port, indexes=True))
        parts += [self._apache_vhost(s.host, s.root, port, aliases=s.aliases, extra=s.apache_extra)
                  for s in sites]
        conf_dir = os.path.join(APACHE, "conf", "extra")
        os.makedirs(conf_dir, exist_ok=True)
        path = os.path.join(conf_dir, "localizer-vhosts.conf")
        write_text(path, "\n".join(parts))
        return path

    @staticmethod
    def _nginx_server(host, root, port, fcgi, default=False, aliases=(), extra=""):
        lines = ["server {",
                 f"    listen {port}{' default_server' if default else ''};",
                 f"    server_name {' '.join([host, *aliases])};",
                 f'    root "{root}";',
                 "    index index.php index.html;",
                 "    client_max_body_size 128m;"]
        if default:
            lines.append("    autoindex on;")
            if pma_ready():
                lines += ["    location = /phpmyadmin { return 301 /phpmyadmin/; }",
                          "    location ^~ /phpmyadmin/ {",
                          f'        alias "{fwd(PMA)}/";',
                          "        index index.php;",
                          "        location ~ \\.php$ {",
                          f"            fastcgi_pass {fcgi};",
                          "            include fastcgi_params;",
                          "            fastcgi_param SCRIPT_FILENAME $request_filename;",
                          "        }",
                          "    }"]
        else:
            lines.append("    location / { try_files $uri $uri/ /index.php?$query_string; }")
        lines += ["    location ~ \\.php$ {",
                  "        try_files $uri =404;",
                  f"        fastcgi_pass {fcgi};",
                  "        fastcgi_index index.php;",
                  "        include fastcgi_params;",
                  "        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;",
                  "    }"]
        if extra.strip():
            lines.append("    # Konfigurasi tambahan (Pengaturan situs)")
            lines += [f"    {line}" for line in extra.strip().splitlines()]
        lines += ["}", ""]
        return "\n".join(lines)

    def write_nginx_sites(self, sites):
        conf_dir = os.path.join(NGINX, "conf")
        if not os.path.isfile(os.path.join(conf_dir, "nginx.conf")):
            return None
        port = self.port_of("Nginx", 8080)
        fcgi = f"127.0.0.1:{self.port_of('PHP-CGI', 9000)}"
        parts = ["# Dibuat otomatis oleh Localizer - perubahan manual akan ditimpa.\n"
                 "# Ubah lewat halaman Situs -> Pengaturan situs.\n",
                 self._nginx_server("localhost", fwd(WWW), port, fcgi, default=True)]
        parts += [self._nginx_server(s.host, s.root, port, fcgi, aliases=s.aliases, extra=s.nginx_extra)
                  for s in sites]
        path = os.path.join(conf_dir, "localizer-sites.conf")
        write_text(path, "\n".join(parts))
        return path

    def sync_vhosts(self, restart=True):
        sites = self.collect_sites(verbose=True)
        if self.write_apache_vhosts(sites):
            self.log(f"Virtual host Apache ditulis ({len(sites)} situs + localhost).")
        else:
            self.log("Folder Apache belum ada — vhost Apache dilewati.")
        if self.write_nginx_sites(sites):
            self.log(f"Virtual host Nginx ditulis ({len(sites)} situs + localhost).")
        self._write_hosts([h for s in sites for h in [s.host, *s.aliases]])
        if restart:
            self.restart_web_servers()
        self.ui(self.refresh_sites)  # aman dipanggil dari thread (mis. setelah composer)

    def restart_web_servers(self):
        for name in ("Apache", "Nginx"):
            s = self.svc(name)
            if s and s.running:
                self.log(f"Restart {name} untuk menerapkan perubahan…")
                threading.Thread(target=lambda s=s: (s.stop(), s.start()), daemon=True).start()

    def test_server_configs(self):
        """Uji konfigurasi Apache/Nginx yang terpasang (httpd -t / nginx -t).
        Kembalikan teks error, atau '' bila semua lolos / tidak ada yang bisa diuji."""
        errors = []
        a = self.svc("Apache")
        if a and a.available and os.path.isfile(os.path.join(APACHE, "conf", "httpd.conf")):
            self.prepare_apache()  # pastikan Include, modul PHP, dan vhost terbaru terpasang
            r = subprocess.run([a.exe, "-t"], cwd=a.cwd, capture_output=True, text=True, errors="replace",
                               env=tool_env(), creationflags=NO_WINDOW)
            if r.returncode != 0:
                errors.append("Apache:\n" + ((r.stderr or "") + (r.stdout or "")).strip())
        n = self.svc("Nginx")
        if n and n.available and os.path.isfile(os.path.join(NGINX, "conf", "nginx.conf")):
            self.configure_nginx()
            r = subprocess.run([n.exe, "-t"], cwd=n.cwd, capture_output=True, text=True, errors="replace",
                               creationflags=NO_WINDOW)
            if r.returncode != 0:
                errors.append("Nginx:\n" + ((r.stderr or "") + (r.stdout or "")).strip())
        return "\n\n".join(errors)

    def _write_hosts(self, hosts):
        try:
            text = read_text(HOSTS_PATH)
            text = with_block(text, "\n".join(f"127.0.0.1 {h}" for h in hosts))
            write_text(HOSTS_PATH, text)
            self.log(f"File hosts diperbarui ({len(hosts)} domain).")
        except PermissionError:
            self.log("File hosts butuh hak Administrator. Jalankan Localizer sebagai Administrator "
                     "(klik kanan → Run as administrator).")
        except OSError as e:
            self.log(f"Gagal menulis hosts: {e}")


if __name__ == "__main__":
    if already_running():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_ID, "Localizer sudah berjalan.\nCek ikonnya di system tray (pojok kanan bawah).")
        root.destroy()
        sys.exit(0)
    App(autostart="--autostart" in sys.argv[1:]).mainloop()
