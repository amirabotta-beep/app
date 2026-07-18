#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
#!/usr/bin/env python
بوت تيليجرام لفك وتجميع وتوقيع تطبيقات APK (apktool + uber-apk-signer)
نفس منطق أداة سطح المكتب الأصلية، لكن بواجهة أزرار تيليجرام.

التشغيل:
    pip install -r requirements.txt
    python telegram_apk_bot.py

أول مرة تشغّله هيتعمل ملف config.json تلقائي، افتحه وضيف فيه:
  - bot_token       : توكن البوت من @BotFather
  - admin_ids       : آيدي تيليجرام بتاعك
  - github_token    : GitHub Personal Access Token (repo scope)
  - github_repo     : اسم الـ repo مثلاً "username/my-app"
  - github_key_path : مسار ملف الـ private key .pem (اختياري لو محتاجه)
"""

import os
import sys
import re
import json
import random
import string
import shutil
import zipfile
import tarfile
import glob
import time
import asyncio
import logging
import subprocess
import requests
import datetime
import urllib.parse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("apk_bot")

# =============================================================================
# المسارات الأساسية
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TOOLS_DIR       = os.path.join(BASE_DIR, "tools")
WORKSPACE_DIR   = os.path.join(BASE_DIR, "workspace")
KEYSTORES_DIR   = os.path.join(WORKSPACE_DIR, "keystores")
PROJECT_DIR     = os.path.join(WORKSPACE_DIR, "current_project")
PUBLISH_DRAFTS_DIR = os.path.join(WORKSPACE_DIR, "publish_drafts")
APK_COPY_PATH   = os.path.join(WORKSPACE_DIR, "current.apk")
CONFIG_PATH     = os.path.join(BASE_DIR, "config.json")

APKTOOL_JAR     = os.path.join(TOOLS_DIR, "apktool.jar")
UBER_SIGNER_JAR = os.path.join(TOOLS_DIR, "uber-apk-signer.jar")
CLASSES_ZIP_PATH= os.path.join(TOOLS_DIR, "classes.zip")
BAKSMALI_JAR    = os.path.join(TOOLS_DIR, "baksmali.jar")
SMALI_JAR       = os.path.join(TOOLS_DIR, "smali.jar")

# ── جافا: استخدم النسخة المحمولة جوه tools/jre لو موجودة، وإلا ارجع لجافا السيرفر ──
_LOCAL_JAVA_BIN = os.path.join(TOOLS_DIR, "jre", "bin", "java")
JAVA_BIN = _LOCAL_JAVA_BIN if os.path.isfile(_LOCAL_JAVA_BIN) else "java"
print(f"☕ Java path: {JAVA_BIN}  (محلي: {os.path.isfile(_LOCAL_JAVA_BIN)})")

# =============================================================================
# توزيع الموارد على سيرفر 1GB RAM / 2 vCPU (Railway) - محسّن لطريقة baksmali/smali
# ─────────────────────────────────────────────────────────────────────────
# كل عمليات الفك/التجميع بقت تسلسلية 100%: ملف dex واحد بس بيتفك أو
# يتجمع في كل لحظة (مفيش تعدد JVMs شغالة في نفس الوقت جوه نفس الطلب).
# ده معناه إننا نقدر نوزع الـ 1GB بذكاء أكتر بدل التوزيع القديم المتحفظ:
#
#   ~150-200 ميجا  → عملية البوت نفسها (python-telegram-bot) + نظام التشغيل
#   ~700-750 ميجا  → الجافا (JVM واحدة شغالة في كل لحظة، سواء baksmali/smali/signer)
#
# الـ 2 vCPU بيتوزعوا كده:
#   - وقت الفك/التجميع: core واحد بيشغل baksmali/smali، والتاني فاضي للـ GC
#     (ParallelGC بيستخدمه) وللبوت نفسه يفضل رد على تليجرام من غير تهنيج.
# =============================================================================

# ── ذاكرة الجافا لـ baksmali/smali: أخف بكتير من apktool القديم لأنها ──
# بتشتغل على ملف dex واحد في كل مرة (مفيش فك/بناء موارد خالص). بما إن
# العملية تسلسلية، رفعنا الذاكرة من 384 لـ 640 ميجا بأمان - بيقلل عدد
# مرات الـ GC (أسرع)، وParallelGC بيستخدم الـ 2 vCPU في التنضيف.
SMALI_MEM_OPTS = [
    "-Xmx640m",
    "-XX:+UseParallelGC",
    "-XX:ParallelGCThreads=2",
]
# baksmali/smali متعددة الخيوط داخليًا؛ بما إن عملية واحدة بس شغالة في كل
# لحظة (مفيش تزاحم بين عدة ملفات dex)، أقصى استغلال للـ 2 vCPU هنا أمان تام.
SMALI_JOBS = str(max(1, min(os.cpu_count() or 2, 2)))

# ── ذاكرة uber-apk-signer: بتشتغل بعد التجميع مباشرة (مفيش تزاحم مع ──
# baksmali/smali)، فتقدر تاخد نفس النطاق الآمن من الذاكرة.
JAVA_MEM_OPTS = [
    "-Xmx640m",
    "-XX:+UseParallelGC",
    "-XX:ParallelGCThreads=2",
]

# ── إعدادات ذاكرة baksmali/smali: أخف بكتير من apktool لأنها بتشتغل على ──
# ملف dex واحد في كل مرة (مش الـ APK كله دفعة واحدة زي apktool)، ومفيش
# فك/بناء موارد خالص. سقف ذاكرة أقل كفاية جدًا ومريح أكتر على سيرفر 1GB.
SMALI_MEM_OPTS = [
    "-Xmx384m",
    "-XX:+UseSerialGC",
]
# عدد الـ threads بتاعة baksmali/smali (متعدد الخيوط افتراضيًا داخليًا)،
# نثبته عند 2 عشان يتماشى مع الـ 2 vCPU المتاحة من غير تزاحم زيادة.
SMALI_JOBS = str(max(1, min(os.cpu_count() or 2, 2)))

for _d in (TOOLS_DIR, WORKSPACE_DIR, KEYSTORES_DIR, PUBLISH_DRAFTS_DIR):
    os.makedirs(_d, exist_ok=True)

# =============================================================================
# معرّف النسخة (Instance ID) — عشان تقدر تفرّق بين أكتر من نسخة شغالة بالغلط
# =============================================================================
# لو Railway حاطط متغير بيتغير مع كل ديبلوي جديد (زي RAILWAY_DEPLOYMENT_ID أو
# RAILWAY_GIT_COMMIT_SHA) بنستخدمه، وإلا بنولد معرّف عشوائي وقت التشغيل.
_DEPLOY_TAG = (
    os.environ.get("RAILWAY_DEPLOYMENT_ID")
    or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:8]
    or ""
)
BOOT_TIME   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
INSTANCE_ID = f"{_DEPLOY_TAG or 'local'}-{''.join(random.choices(string.ascii_letters + string.digits, k=5))}"
print(f"🆔 Instance ID: {INSTANCE_ID}  |  ⏱ Boot time: {BOOT_TIME}")

DEFAULT_CONFIG = {
    "bot_token"      : "PUT_YOUR_BOT_TOKEN_HERE",
    "admin_ids"      : [],
    "keystores"      : [],
    # ── GitHub ──────────────────────────────────────────────────────────────
    "github_token"   : "",          # GitHub Personal Access Token (repo scope)
    "github_repo"    : "",          # مثال: "myuser/my-app"
    "github_key_path": "",          # مسار ملف .pem لو محتاجه (اختياري)
    "release_counter": 0,           # عداد محلي لرقم الإصدار — بيزيد واحد بعد كل رفع ناجح
    # ── Firebase (نشر التطبيقات على الموقع) ──────────────────────────────────
    # apiKey و projectId دول عموميين أصلاً (موجودين جوه كود صفحة الأدمن نفسها
    # اللي شغالة في المتصفح)، فمفيش مشكلة إنهم يكونوا هنا. اللي لازم تحطه
    # انت بنفسك (ماتبعتهوش هنا في الشات): firebase_admin_email/password —
    # حساب أدمن حقيقي (إيميل + باسورد) متسجل في Firebase Authentication
    # ومضاف في قايمة الأدمن (ALLOWED_ADMINS) وفي قواعد أمان Firestore.
    "firebase_api_key"        : "AIzaSyDqiO6lxMfbsBGbTln1TwDnZP5VpKrbYIw",
    "firebase_project_id"     : "volt-tech-814a5",
    "firebase_admin_email"    : "",
    "firebase_admin_password" : "",
    # User UID بتاع حساب الأدمن في Firebase Authentication (مثلاً:
    # QtoWqaDrgQUc0cuyERXLjXKRlj72) — بيتحفظ في التطبيقات المنشورة كحقل
    # "ownerUid" عشان يتوافق مع قواعد أمان Firestore اللي بتتحقق من
    # request.auth.uid. تقدر تحدّثه من زرار "🆔 تحديث User UID" في القائمة.
    "firebase_owner_uid"      : "",
    # ── نشر إعلان التطبيق في قناة تيليجرام بعد نشره على الموقع ────────────────
    # telegram_channel_id: آيدي أو يوزر القناة اللي البوت أدمن فيها، مثال:
    # "@my_channel" أو "-1001234567890". لازم تضيف البوت أدمن في القناة
    # وتديله صلاحية "نشر رسائل" (Post Messages) عشان يقدر يبعت فيها.
    "telegram_channel_id"     : "",
    # site_app_page_url: رابط صفحة عرض التطبيق على موقعك (بدون الـ query
    # string)، وهيتضاف له "?app=<packageId بعد استبدال النقط بشرطات>"
    # تلقائيًا. مثال: https://volttechcode.github.io/web/app-page.html
    "site_app_page_url"       : "https://volttechcode.github.io/web/app-page.html",
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        cfg = dict(DEFAULT_CONFIG)
    else:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
        except Exception:
            cfg = dict(DEFAULT_CONFIG)

    # ── الأسرار: لو متعرّفة كـ environment variables على السيرفر (زي Railway)،
    # بتاخد الأولوية على قيم config.json. ده بيسمح إنك تسيب config.json
    # فاضي من الأسرار الحقيقية وميبقاش خطر لو اترفع لـ Git بالغلط.
    cfg["bot_token"]    = os.environ.get("BOT_TOKEN", cfg.get("bot_token", ""))
    cfg["github_token"] = os.environ.get("GITHUB_TOKEN", cfg.get("github_token", ""))
    cfg["telegram_channel_id"] = os.environ.get("TELEGRAM_CHANNEL_ID", cfg.get("telegram_channel_id", ""))
    cfg["firebase_admin_email"]    = os.environ.get("FIREBASE_ADMIN_EMAIL", cfg.get("firebase_admin_email", ""))
    cfg["firebase_admin_password"] = os.environ.get("FIREBASE_ADMIN_PASSWORD", cfg.get("firebase_admin_password", ""))
    cfg["firebase_owner_uid"]      = os.environ.get("FIREBASE_OWNER_UID", cfg.get("firebase_owner_uid", ""))

    return cfg


def save_config(cfg):
    # ── لا نكتب أبدًا القيم الفعلية لـ bot_token/github_token اللي كانت جايه
    # من environment variables في وقت التشغيل — عشان نمنع تسرّبها لملف
    # config.json (اللي ممكن يترفع لـ Git بالغلط). لو الملف الأصلي كان فيه
    # قيمة (أو فاضي)، بنحافظ عليها زي ما هي بدل قيمة الـ runtime.
    to_write = dict(cfg)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
            for secret_key in ("bot_token", "github_token", "firebase_admin_email", "firebase_admin_password"):
                if secret_key in existing:
                    to_write[secret_key] = existing[secret_key]
    except Exception:
        pass
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(to_write, f, ensure_ascii=False, indent=2)


CFG = load_config()

USER_STATE = {}


def get_state(uid):
    return USER_STATE.setdefault(uid, {})


def reset_state(uid):
    USER_STATE[uid] = {}


def random_string(n=8):
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def is_admin(uid):
    admins = CFG.get("admin_ids") or []
    return (not admins) or (uid in admins)


def find_keystore(name):
    for k in CFG.get("keystores", []):
        if k["name"] == name:
            return k
    return None


def get_next_release_version_from_github(token: str, repo: str) -> str:
    """
    اسم الدالة اتسابت زي ما هي عشان مكانها في الكود، لكن دلوقتي
    مبتسألش GitHub خالص — بتستخدم عداد محلي متخزّن في config.json
    (release_counter). ده أسرع (مفيش استدعاء API إضافي)، وأثبت
    (مش متأثر بحذف/تعديل releases قديمة على GitHub يدويًا).
    الرقم بيتحفظ فعليًا في config.json بعد نجاح الرفع بس (في
    github_upload_sync)، عشان لو الرفع فشل ميضيعش رقم من العداد.
    """
    return str(CFG.get("release_counter", 0) + 1)


# =============================================================================
# تقدير نسبة التقدم (%) — مفيش أداة زي apktool بتديك % حقيقية، فبنقدّرها
# بالاعتماد على متوسط "ثانية لكل ميجابايت" من عمليات ناجحة سابقة، وبنحدّث
# المتوسط ده تلقائيًا كل ما عملية جديدة تخلص (بيبقى أدق كل ما البوت يشتغل أكتر).
# =============================================================================
STATS_PATH = os.path.join(WORKSPACE_DIR, "build_stats.json")

# قيم افتراضية أولية (ثانية/ميجابايت) لحد ما يتجمع بيانات حقيقية من التشغيل الفعلي
_DEFAULT_RATE_PER_MB = {"decompile": 3.5, "build": 4.5, "sign": 1.5}


def load_stats():
    if not os.path.exists(STATS_PATH):
        return {}
    try:
        with open(STATS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_stats(stats):
    try:
        with open(STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_dir_size(path):
    """يحسب حجم مجلد كامل بالبايت (بيتستخدم لتقدير وقت التجميع build)."""
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except Exception:
                pass
    return total


def estimate_seconds(stage, size_bytes):
    """
    تقدير الوقت المتوقع (بالثواني) لمرحلة معيّنة (decompile/build/sign)
    بناءً على حجم الملف/المشروع، ومتوسط الأداء الفعلي المسجّل من مرات سابقة.
    """
    size_mb = max(size_bytes / (1024 * 1024), 0.1)
    s = load_stats().get(stage, {})
    total_seconds  = s.get("total_seconds", 0)
    total_size_mb  = s.get("total_size_mb", 0)
    if total_size_mb > 0.5:  # فيه بيانات كفاية من مرات سابقة
        rate = total_seconds / total_size_mb
    else:
        rate = _DEFAULT_RATE_PER_MB.get(stage, 3.0)
    return max(5, rate * size_mb)


def record_stage_time(stage, size_bytes, elapsed_seconds):
    """يحدّث متوسط الأداء بعد ما مرحلة تخلص بنجاح، عشان التقدير يبقى أدق مرة بعد مرة."""
    size_mb = max(size_bytes / (1024 * 1024), 0.1)
    stats = load_stats()
    s = stats.setdefault(stage, {"total_seconds": 0.0, "total_size_mb": 0.0, "samples": 0})
    s["total_seconds"] += elapsed_seconds
    s["total_size_mb"] += size_mb
    s["samples"]       += 1
    # نخلي المتوسط بيتبع آخر ~15 عملية بس (decay) عشان يفضل متأقلم لو أداء
    # السيرفر اتغير، مش متجمّد على متوسط عمر البوت كله
    if s["samples"] > 15:
        factor = 15 / s["samples"]
        s["total_seconds"] *= factor
        s["total_size_mb"] *= factor
        s["samples"] = 15
    save_stats(stats)


def format_progress_bar(pct, width=10):
    filled = int(round(pct / 100 * width))
    filled = max(0, min(width, filled))
    return "▓" * filled + "░" * (width - filled)


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} ثانية"
    m, s = divmod(seconds, 60)
    return f"{m} دقيقة و{s} ثانية" if s else f"{m} دقيقة"


async def run_cmd_with_heartbeat(cmd, status_msg, base_text, interval=6, estimated_seconds=None, stage=None, size_bytes=None):
    """
    زي run_cmd بالظبط، لكن بيحدّث الرسالة كل `interval` ثانية.

    لو اتبعتله estimated_seconds، بيعرض progress bar ونسبة % تقديرية (زي
    شريط تحميل تطبيق عادي) بناءً على متوسط أداء العمليات السابقة، وبتفضل
    واقفة عند 99% لحد ما العملية تخلص فعليًا (عشان منديش انطباع كدب إنها
    خلصت وهي لسه شغالة). لو مفيش تقدير متاح، بيرجع للعداد القديم (وقت مرّ بس).

    بيرجع: (ok: bool, log_text: str, elapsed_seconds: float)
    """
    start = time.time()
    task = asyncio.ensure_future(run_cmd(cmd))
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            try:
                if estimated_seconds and estimated_seconds > 0:
                    pct = min(99, int(elapsed / estimated_seconds * 100))
                    bar = format_progress_bar(pct)
                    remaining = max(1, estimated_seconds - elapsed)
                    await status_msg.edit_text(
                        f"{base_text}\n{bar}  {pct}%\n"
                        f"⏱ مضى {format_duration(elapsed)} — تقريبًا متبقي {format_duration(remaining)}"
                    )
                else:
                    await status_msg.edit_text(f"{base_text}\n⏱ لسه شغال... ({int(elapsed)} ثانية)")
            except Exception:
                pass
    ok, log_text = await task
    elapsed_total = time.time() - start
    if ok and stage and size_bytes:
        try:
            record_stage_time(stage, size_bytes, elapsed_total)
        except Exception:
            pass
    return ok, log_text, elapsed_total


# =============================================================================
# تشغيل أوامر خارجية
# =============================================================================
async def run_cmd(cmd):
    def _run():
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1800,
            )
            out = proc.stdout or ""
            if proc.returncode == 0:
                return True, out
            if proc.returncode < 0:
                # العملية اتقفلت بإشارة (signal) من السيستم نفسه — الأشهر -9 = SIGKILL بسبب نفاذ الذاكرة (OOM)
                sig = -proc.returncode
                note = (
                    f"\n\n⚠️ العملية اتقفلت بالقوة بإشارة رقم {sig}"
                    + (" (SIGKILL — الأرجح نفاذ الذاكرة RAM على السيرفر / Out of Memory)." if sig == 9 else ".")
                )
                return False, out + note
            return False, out + f"\n\n⚠️ رمز الخروج (exit code): {proc.returncode}"
        except FileNotFoundError:
            return False, "❌ تعذر تشغيل الأمر. تأكد أن Java مثبت بشكل صحيح على السيرفر."
        except subprocess.TimeoutExpired:
            return False, "❌ انتهت المهلة الزمنية للعملية (timeout)."
        except Exception as e:
            return False, f"❌ خطأ: {e}"

    return await asyncio.to_thread(_run)


def tail_log(text, limit=600):
    text = text or ""
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:]


# =============================================================================
# فحص وتحميل الجافا (زرار "فحص/تحميل Java")
# =============================================================================
JRE_DOWNLOAD_URL = "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jre/hotspot/normal/eclipse"


def check_java_version_sync(java_bin):
    """يتحقق هل أمر الجافا شغال فعلاً ويرجع (ok, output)."""
    try:
        proc = subprocess.run(
            [java_bin, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        )
        return proc.returncode == 0, proc.stdout or ""
    except FileNotFoundError:
        return False, "غير موجود (FileNotFoundError)"
    except Exception as e:
        return False, f"خطأ غير متوقع: {e}"


def download_java_sync():
    """
    ينزل JRE خفيفة من Adoptium ويحطها في tools/jre وقت التشغيل (مش بترفع على GitHub).
    يرجع (success, log_text, elapsed_seconds) — اللوج ده هو "التيمر" اللي تقدر تبعته
    لو حصلت مشكلة عشان نعرف وقفت فين بالظبط.
    """
    start = time.time()
    steps = []

    def log(msg):
        elapsed = time.time() - start
        steps.append(f"[{elapsed:5.1f}s] {msg}")

    tmp_tar = "/tmp/jre_download.tar.gz"
    tmp_extract = "/tmp/jre_extract"

    try:
        log("⬇️ بدء الاتصال بسيرفر Adoptium...")
        r = requests.get(JRE_DOWNLOAD_URL, stream=True, timeout=120, allow_redirects=True)
        r.raise_for_status()
        log("✅ تم الاتصال، جاري سحب الملف...")

        with open(tmp_tar, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

        size_mb = os.path.getsize(tmp_tar) / 1024 / 1024
        log(f"✅ اكتمل التحميل ({size_mb:.1f} ميجا)")

        if os.path.isdir(tmp_extract):
            shutil.rmtree(tmp_extract, ignore_errors=True)
        os.makedirs(tmp_extract, exist_ok=True)

        log("📦 جاري فك الضغط...")
        with tarfile.open(tmp_tar, "r:gz") as tar:
            tar.extractall(tmp_extract)
        log("✅ تم فك الضغط")

        extracted = glob.glob(os.path.join(tmp_extract, "jdk-*")) or glob.glob(os.path.join(tmp_extract, "*jre*"))
        if not extracted:
            log("❌ مفيش فولدر جافا اتلاقى بعد فك الضغط")
            return False, "\n".join(steps), time.time() - start

        jre_dest = os.path.join(TOOLS_DIR, "jre")
        if os.path.isdir(jre_dest):
            shutil.rmtree(jre_dest, ignore_errors=True)
        shutil.move(extracted[0], jre_dest)
        log(f"✅ تم نقل الجافا إلى {jre_dest}")

        try:
            os.remove(tmp_tar)
            shutil.rmtree(tmp_extract, ignore_errors=True)
        except Exception:
            pass

        new_java_bin = os.path.join(jre_dest, "bin", "java")
        ok, out = check_java_version_sync(new_java_bin)
        if ok:
            log("✅ تم التحقق: الجافا شغالة")
            return True, "\n".join(steps) + "\n" + out.strip(), time.time() - start
        else:
            log(f"❌ فشل التحقق بعد التحميل: {out}")
            return False, "\n".join(steps), time.time() - start

    except Exception as e:
        log(f"❌ خطأ: {e}")
        return False, "\n".join(steps), time.time() - start


def verify_jar_file(path):
    """
    يتحقق إن ملف الـ jar موجود وسليم (zip صحيح) قبل ما نشغّله بالجافا.
    بيكشف حالة الملف التالف بسبب رفع خاطئ على GitHub بدل ما ننتظر خطأ
    'Invalid or corrupt jarfile' الغامض من الجافا.
    """
    name = os.path.basename(path)
    if not os.path.isfile(path):
        return False, f"❌ ملف {name} غير موجود في tools/."
    size = os.path.getsize(path)
    if size == 0:
        return False, f"❌ ملف {name} موجود لكن حجمه صفر بايت (ملف فاضي/مقطوع)."
    if not zipfile.is_zipfile(path):
        return False, (
            f"❌ ملف {name} تالف أو مش jar صحيح (حجمه الحالي {size/1024:.0f} كيلوبايت).\n"
            "في الأغلب اتلف وقت الرفع على GitHub (مشكلة binary/line-endings).\n"
            "الحل: ضيف .gitattributes واعمل git rm --cached + إعادة رفع الملف."
        )
    return True, f"✅ {name} سليم ({size/1024:.0f} كيلوبايت)."


# =============================================================================
# فك وتجميع سريع (baksmali/smali) بدل apktool الكامل
# ─────────────────────────────────────────────────────────────────────────
# apktool بيفك حاجتين مع بعض: الأكواد (dex → smali) والموارد (الصور/الألوان/
# الـ manifest → XML قابل للتعديل). البوت مش بيلمس الموارد خالص (البحث
# والاستبدال شغال على .smali بس)، فبنستخدم baksmali/smali اللي بتشتغل على
# الأكواد فقط، وبنسيب الموارد والـ manifest والصور زي ما هي بالظبط جوه
# نسخة من ملف الـ APK الأصلي (zip عادي). النتيجة: ذاكرة أقل بكتير، وسرعة أعلى.
# =============================================================================

def list_dex_entries_sync(apk_path):
    """يرجع أسماء كل ملفات classesN.dex الموجودة جوه الـ APK بترتيبها."""
    with zipfile.ZipFile(apk_path, "r") as zf:
        names = [n for n in zf.namelist() if re.fullmatch(r"classes\d*\.dex", n)]
    def sort_key(n):
        m = re.fullmatch(r"classes(\d*)\.dex", n)
        num = m.group(1)
        return int(num) if num else 1
    return sorted(names, key=sort_key)


def dex_to_smali_folder_name(dex_name):
    """classes.dex → smali | classes2.dex → smali_classes2"""
    m = re.fullmatch(r"classes(\d*)\.dex", dex_name)
    num = m.group(1)
    return "smali" if not num else f"smali_classes{num}"


def smali_folder_to_dex_name(folder_name):
    """smali → classes.dex | smali_classes2 → classes2.dex"""
    if folder_name == "smali":
        return "classes.dex"
    m = re.fullmatch(r"smali_classes(\d+)", folder_name)
    return f"classes{m.group(1)}.dex" if m else None


def decompile_apk_fast_sync(apk_path, project_dir):
    """
    فك سريع وخفيف: بيطلع كل classesN.dex ويحوّله smali بس (baksmali)،
    من غير ما يلمس الموارد أو الـ manifest أو الصور خالص - بيفضلوا
    محفوظين كنسخة كاملة من الـ APK الأصلي جوه project_dir/original.apk
    عشان نستخدمها وقت التجميع تاني.
    """
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)
    os.makedirs(project_dir, exist_ok=True)

    original_backup = os.path.join(project_dir, "original.apk")
    shutil.copy2(apk_path, original_backup)

    dex_entries = list_dex_entries_sync(apk_path)
    if not dex_entries:
        return False, "❌ مفيش أي ملف classes.dex جوه الـ APK ده."

    tmp_dex_dir = os.path.join(project_dir, "_dex_tmp")
    os.makedirs(tmp_dex_dir, exist_ok=True)
    with zipfile.ZipFile(apk_path, "r") as zf:
        for entry in dex_entries:
            zf.extract(entry, tmp_dex_dir)

    full_log = []
    for entry in dex_entries:
        dex_path = os.path.join(tmp_dex_dir, entry)
        out_folder = os.path.join(project_dir, dex_to_smali_folder_name(entry))
        proc = subprocess.run(
            [JAVA_BIN, *SMALI_MEM_OPTS, "-jar", BAKSMALI_JAR, "d",
             dex_path, "-o", out_folder, "-j", SMALI_JOBS],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800,
        )
        full_log.append(f"── {entry} ──\n" + (proc.stdout or ""))
        if proc.returncode != 0:
            shutil.rmtree(tmp_dex_dir, ignore_errors=True)
            return False, "\n".join(full_log)

    shutil.rmtree(tmp_dex_dir, ignore_errors=True)
    return True, "\n".join(full_log)


async def decompile_apk_fast(apk_path, project_dir, status_msg=None, base_text=""):
    return await asyncio.to_thread(decompile_apk_fast_sync, apk_path, project_dir)


def build_apk_fast_sync(project_dir, out_apk_path):
    """
    تجميع سريع: بيجمّع كل مجلد smali_classesX لملف dex تاني (smali)،
    وبعدين بيحط الـ dex الجديد مكان القديم جوه نسخة من الـ APK الأصلي
    (original.apk) - فالموارد والصور والـ manifest بتفضل زي ما هي
    بالظبط من غير أي فك أو إعادة بناء ليهم.
    """
    original_backup = os.path.join(project_dir, "original.apk")
    if not os.path.isfile(original_backup):
        return False, "❌ مفقود original.apk (نسخة النسخ الاحتياطي من المشروع الأصلي)."

    smali_folders = [
        d for d in os.listdir(project_dir)
        if os.path.isdir(os.path.join(project_dir, d))
        and (d == "smali" or re.fullmatch(r"smali_classes\d+", d))
    ]
    if not smali_folders:
        return False, "❌ مفيش أي مجلد smali جوه المشروع."

    tmp_dex_dir = os.path.join(project_dir, "_dex_build_tmp")
    if os.path.isdir(tmp_dex_dir):
        shutil.rmtree(tmp_dex_dir, ignore_errors=True)
    os.makedirs(tmp_dex_dir, exist_ok=True)

    full_log = []
    new_dex_paths = {}
    for folder in smali_folders:
        dex_name = smali_folder_to_dex_name(folder)
        if not dex_name:
            continue
        folder_path = os.path.join(project_dir, folder)
        dex_out = os.path.join(tmp_dex_dir, dex_name)
        proc = subprocess.run(
            [JAVA_BIN, *SMALI_MEM_OPTS, "-jar", SMALI_JAR, "assemble",
             folder_path, "-o", dex_out, "-j", SMALI_JOBS],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800,
        )
        full_log.append(f"── {folder} → {dex_name} ──\n" + (proc.stdout or ""))
        if proc.returncode != 0 or not os.path.isfile(dex_out):
            shutil.rmtree(tmp_dex_dir, ignore_errors=True)
            return False, "\n".join(full_log)
        new_dex_paths[dex_name] = dex_out

    try:
        if os.path.isfile(out_apk_path):
            os.remove(out_apk_path)
        shutil.copy2(original_backup, out_apk_path)

        # نستبدل كل ملف dex قديم بالنسخة الجديدة جوه نفس الـ APK (zip)، من
        # غير ما نلمس أي entry تاني (موارد/صور/manifest بتفضل زي ما هي).
        tmp_zip_path = out_apk_path + ".tmp"
        with zipfile.ZipFile(out_apk_path, "r") as zin, \
             zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zout:
            existing_names = set(zin.namelist())
            for item in zin.infolist():
                if item.filename in new_dex_paths:
                    continue
                zout.writestr(item, zin.read(item.filename))
            for dex_name, dex_path in new_dex_paths.items():
                with open(dex_path, "rb") as f:
                    zout.writestr(dex_name, f.read())
        os.replace(tmp_zip_path, out_apk_path)
    except Exception as e:
        shutil.rmtree(tmp_dex_dir, ignore_errors=True)
        return False, "\n".join(full_log) + f"\n❌ خطأ أثناء إعادة تجميع الـ APK: {e}"

    shutil.rmtree(tmp_dex_dir, ignore_errors=True)
    full_log.append(f"✅ تم إنتاج {out_apk_path}")
    return True, "\n".join(full_log)


async def build_apk_fast(project_dir, out_apk_path):
    return await asyncio.to_thread(build_apk_fast_sync, project_dir, out_apk_path)


async def run_async_with_heartbeat(coro, status_msg, base_text, interval=5):
    """
    زي run_cmd_with_heartbeat، لكن لأي عملية async (مش بس subprocess) -
    بنستخدمها مع decompile_apk_fast/build_apk_fast اللي بتلف على كذا
    ملف dex بدل أمر واحد. بترجع (ok, log_text, elapsed_seconds).
    """
    start = time.time()
    task = asyncio.ensure_future(coro)
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            try:
                await status_msg.edit_text(f"{base_text}\n⏱ لسه شغال... ({int(elapsed)} ثانية)")
            except Exception:
                pass
    ok, log_text = await task
    return ok, log_text, time.time() - start


async def check_java_flow(query):
    """زرار 'فحص/تحميل Java': يتحقق الأول، ولو مش موجودة ينزلها مع تيمر حي."""
    global JAVA_BIN

    msg = await query.edit_message_text("⏳ جاري التحقق من وجود الجافا...")

    ok, out = await asyncio.to_thread(check_java_version_sync, JAVA_BIN)
    if ok:
        version_line = next((l for l in out.strip().splitlines() if l.strip()), "?")
        await msg.edit_text(
            "✅ الجافا موجودة وشغالة بالفعل!\n\n"
            f"📍 المسار: {JAVA_BIN}\n"
            f"ℹ️ {version_line}",
            reply_markup=main_menu_kb(),
        )
        return

    # مش موجودة → هننزلها دلوقتي على السيرفر (مش من GitHub)
    await msg.edit_text(
        "❌ الجافا مش موجودة على السيرفر.\n"
        "⏳ جاري تحميلها وتجهيزها الآن (هتلاقي التيمر بيتحدث كل شوية)..."
    )

    stop_timer = asyncio.Event()
    start_time = time.time()

    async def timer_loop():
        while not stop_timer.is_set():
            try:
                await asyncio.wait_for(stop_timer.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            if stop_timer.is_set():
                break
            elapsed = int(time.time() - start_time)
            try:
                await msg.edit_text(
                    "⏳ جاري تحميل وتجهيز الجافا...\n"
                    f"⏱ التيمر: {elapsed} ثانية"
                )
            except Exception:
                pass

    timer_task = asyncio.create_task(timer_loop())

    success, log_text, elapsed = await asyncio.to_thread(download_java_sync)

    stop_timer.set()
    try:
        await timer_task
    except Exception:
        pass

    if success:
        JAVA_BIN = os.path.join(TOOLS_DIR, "jre", "bin", "java")
        await msg.edit_text(
            "✅ تم تحميل وتجهيز الجافا بنجاح!\n\n"
            f"⏱ المدة الكلية: {elapsed:.1f} ثانية\n"
            f"📍 المسار: {JAVA_BIN}\n\n"
            f"📋 سجل التيمر:\n{tail_log(log_text, 700)}",
            reply_markup=main_menu_kb(),
        )
    else:
        await msg.edit_text(
            "❌ فشل تحميل/تجهيز الجافا.\n\n"
            f"⏱ المدة قبل الفشل: {elapsed:.1f} ثانية\n\n"
            f"📋 سجل التيمر (ابعتهولي وأنا أعرف المشكلة فين):\n{tail_log(log_text, 900)}",
            reply_markup=main_menu_kb(),
        )


# =============================================================================
# فحص وتحميل أدوات APK (apktool.jar + uber-apk-signer.jar)
# نفس فكرة زرار الجافا: تحقق أول، ولو ناقص/تالف نزّله من GitHub Releases مباشرة
# =============================================================================
def download_tool_jar_sync(github_repo, name_contains, dest_path):
    """
    يجيب أحدث إصدار jar من GitHub Releases لمشروع معين ويحفظه في dest_path.
    يرجع (success, log_text, elapsed_seconds) بنفس منطق التيمر بتاع الجافا.
    """
    start = time.time()
    steps = []

    def log(msg):
        elapsed = time.time() - start
        steps.append(f"[{elapsed:5.1f}s] {msg}")

    tmp_path = dest_path + ".part"
    try:
        log(f"🔎 جاري البحث عن أحدث إصدار في {github_repo}...")
        api_url = f"https://api.github.com/repos/{github_repo}/releases/latest"
        r = requests.get(api_url, timeout=30, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        assets = r.json().get("assets", [])

        jar_asset = next(
            (a for a in assets if a.get("name", "").endswith(".jar") and name_contains in a.get("name", "")),
            None,
        )
        if not jar_asset:
            log("❌ مفيش ملف jar مطابق في آخر إصدار على GitHub")
            return False, "\n".join(steps), time.time() - start

        log(f"✅ لقيت: {jar_asset['name']}")
        log("⬇️ جاري التحميل...")
        with requests.get(jar_asset["browser_download_url"], stream=True, timeout=120, allow_redirects=True) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        size_mb = os.path.getsize(tmp_path) / 1024 / 1024
        log(f"✅ اكتمل التحميل ({size_mb:.1f} ميجا)")

        if not zipfile.is_zipfile(tmp_path):
            log("❌ الملف اتحمّل لكنه مش jar/zip صحيح (فشل التحقق)")
            os.remove(tmp_path)
            return False, "\n".join(steps), time.time() - start

        shutil.move(tmp_path, dest_path)
        log(f"✅ تم الحفظ في {dest_path}")
        return True, "\n".join(steps), time.time() - start

    except Exception as e:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        log(f"❌ خطأ: {e}")
        return False, "\n".join(steps), time.time() - start


def download_smali_tool_jar_sync(kind, dest_path):
    """
    تحميل baksmali.jar أو smali.jar من مستودع baksmali/smali الرسمي.
    فلترة دقيقة بـ regex (مش 'in' البسيطة) عشان أسماء الملفات متشابهة
    جدًا (مثلاً smali-baksmali-x.y.z-javadoc.jar) ومينفعش نغلط في الاختيار.
    kind: 'baksmali' أو 'smali'
    """
    start = time.time()
    steps = []

    def log(msg):
        elapsed = time.time() - start
        steps.append(f"[{elapsed:5.1f}s] {msg}")

    tmp_path = dest_path + ".part"
    pattern = re.compile(rf"^{kind}-[\d.]+-fat\.jar$")

    try:
        log(f"🔎 جاري البحث عن أحدث إصدار {kind} في baksmali/smali...")
        api_url = "https://api.github.com/repos/baksmali/smali/releases/latest"
        r = requests.get(api_url, timeout=30, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        assets = r.json().get("assets", [])

        jar_asset = next((a for a in assets if pattern.match(a.get("name", ""))), None)
        if not jar_asset:
            log(f"❌ مفيش ملف {kind}-X.Y.Z-fat.jar مطابق في آخر إصدار على GitHub")
            return False, "\n".join(steps), time.time() - start

        log(f"✅ لقيت: {jar_asset['name']}")
        log("⬇️ جاري التحميل...")
        with requests.get(jar_asset["browser_download_url"], stream=True, timeout=120, allow_redirects=True) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        size_mb = os.path.getsize(tmp_path) / 1024 / 1024
        log(f"✅ اكتمل التحميل ({size_mb:.1f} ميجا)")

        if not zipfile.is_zipfile(tmp_path):
            log("❌ الملف اتحمّل لكنه مش jar/zip صحيح (فشل التحقق)")
            os.remove(tmp_path)
            return False, "\n".join(steps), time.time() - start

        shutil.move(tmp_path, dest_path)
        log(f"✅ تم الحفظ في {dest_path}")
        return True, "\n".join(steps), time.time() - start

    except Exception as e:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        log(f"❌ خطأ: {e}")
        return False, "\n".join(steps), time.time() - start


async def check_tools_flow(query):
    """زرار 'فحص/تحميل أدوات APK': يتحقق من كل الأدوات المطلوبة، وينزل الناقص/التالف منهم."""
    msg = await query.edit_message_text("⏳ جاري التحقق من الأدوات (apktool, uber-apk-signer, baksmali, smali)...")

    ok1, msg1 = verify_jar_file(APKTOOL_JAR)
    ok2, msg2 = verify_jar_file(UBER_SIGNER_JAR)
    ok3, msg3 = verify_jar_file(BAKSMALI_JAR)
    ok4, msg4 = verify_jar_file(SMALI_JAR)

    if ok1 and ok2 and ok3 and ok4:
        await msg.edit_text(
            "✅ كل الأدوات موجودة وسليمة!\n\n" + f"{msg1}\n{msg2}\n{msg3}\n{msg4}",
            reply_markup=main_menu_kb(),
        )
        return

    await msg.edit_text(
        "⚠️ فيه أداة ناقصة أو تالفة:\n\n"
        f"{msg1}\n{msg2}\n{msg3}\n{msg4}\n\n"
        "⏳ جاري تحميل الأداة/الأدوات الناقصة من GitHub Releases (مش من رفعك اليدوي)..."
    )

    stop_timer = asyncio.Event()
    start_time = time.time()

    async def timer_loop():
        while not stop_timer.is_set():
            try:
                await asyncio.wait_for(stop_timer.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            if stop_timer.is_set():
                break
            elapsed = int(time.time() - start_time)
            try:
                await msg.edit_text(
                    "⏳ جاري تحميل الأدوات الناقصة...\n"
                    f"⏱ التيمر: {elapsed} ثانية"
                )
            except Exception:
                pass

    timer_task = asyncio.create_task(timer_loop())

    full_log = []
    if not ok1:
        s, log_text, _ = await asyncio.to_thread(
            download_tool_jar_sync, "iBotPeaches/Apktool", "apktool_", APKTOOL_JAR
        )
        full_log.append("📦 apktool.jar:\n" + log_text)
    if not ok2:
        s, log_text, _ = await asyncio.to_thread(
            download_tool_jar_sync, "patrickfav/uber-apk-signer", "uber-apk-signer-", UBER_SIGNER_JAR
        )
        full_log.append("📦 uber-apk-signer.jar:\n" + log_text)
    if not ok3:
        s, log_text, _ = await asyncio.to_thread(download_smali_tool_jar_sync, "baksmali", BAKSMALI_JAR)
        full_log.append("📦 baksmali.jar:\n" + log_text)
    if not ok4:
        s, log_text, _ = await asyncio.to_thread(download_smali_tool_jar_sync, "smali", SMALI_JAR)
        full_log.append("📦 smali.jar:\n" + log_text)

    stop_timer.set()
    try:
        await timer_task
    except Exception:
        pass

    total_elapsed = time.time() - start_time

    ok1f, msg1f = verify_jar_file(APKTOOL_JAR)
    ok2f, msg2f = verify_jar_file(UBER_SIGNER_JAR)
    ok3f, msg3f = verify_jar_file(BAKSMALI_JAR)
    ok4f, msg4f = verify_jar_file(SMALI_JAR)
    header = "✅ تم تجهيز كل الأدوات بنجاح!" if (ok1f and ok2f and ok3f and ok4f) else "❌ لسه فيه مشكلة في تجهيز الأدوات."

    await msg.edit_text(
        f"{header}\n\n"
        f"⏱ المدة الكلية: {total_elapsed:.1f} ثانية\n\n"
        f"{msg1f}\n{msg2f}\n{msg3f}\n{msg4f}\n\n"
        f"📋 سجل التيمر (ابعتهولي لو فيه مشكلة):\n{tail_log(chr(10).join(full_log), 900)}",
        reply_markup=main_menu_kb(),
    )


async def send_long_text(context, chat_id, text):
    if len(text) <= 3500:
        await context.bot.send_message(chat_id=chat_id, text=text)
        return
    path = os.path.join(WORKSPACE_DIR, f"result_{random_string(6)}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(path, "rb") as f:
        await context.bot.send_document(chat_id=chat_id, document=f, filename="نتيجة_البحث.txt")
    try:
        os.remove(path)
    except Exception:
        pass


# =============================================================================
# GitHub Release Upload
# =============================================================================
def github_upload_sync(apk_path: str, apk_original_name: str) -> tuple[bool, str]:
    """
    يعمل GitHub Release ويرفع عليه الـ APK.
    - tag/version تلقائي (1, 2, 3, ...)
    - اسم الـ release = اسم ملف الـ APK الأصلي
    يرجع (success, message_or_url)
    """
    token   = CFG.get("github_token", "").strip()
    repo    = CFG.get("github_repo", "").strip()

    if not token:
        return False, "❌ github_token غير موجود في config.json."
    if not repo:
        return False, "❌ github_repo غير موجود في config.json."

    tag_name     = get_next_release_version_from_github(token, repo)
    release_name = os.path.splitext(apk_original_name)[0]   # اسم الملف بدون .apk

    headers = {
        "Authorization": f"token {token}",
        "Accept"       : "application/vnd.github.v3+json",
        "Content-Type" : "application/json",
    }

    # 1) إنشاء Release — مع retry تلقائي لو التاج مستخدم بالفعل (العداد
    # المحلي كان متأخر عن آخر رقم فعلي على GitHub لأي سبب)، بنجرب أرقام
    # تالية لحد ما نلاقي واحد فاضي، وبنصحّح العداد المحلي بعدها.
    create_url = f"https://api.github.com/repos/{repo}/releases"
    max_attempts = 20
    release_data = None
    r = None
    for attempt in range(max_attempts):
        payload = {
            "tag_name"  : tag_name,
            "name"      : release_name,
            "body"      : f"🤖 رُفع تلقائياً بواسطة بوت APK\n📦 الملف الأصلي: `{apk_original_name}`",
            "draft"     : False,
            "prerelease": False,
        }
        try:
            r = requests.post(create_url, headers=headers, json=payload, timeout=30)
        except Exception as e:
            return False, f"❌ خطأ في الاتصال بـ GitHub:\n{e}"

        if r.status_code == 401:
            return False, (
                "❌ التوكن (github_token) مرفوض من GitHub (401 Bad credentials).\n"
                "التوكن نفسه منتهي/ملغي/غلط — مش مشكلة في صلاحيات أو في الكود.\n"
                "روح اعمل توكن جديد من GitHub → Settings → Developer settings،\n"
                "وحدّثه من زرار \"🐙 تحديث توكن GitHub\" في القائمة الرئيسية."
            )
        if r.status_code == 404:
            return False, (
                f"❌ الريبو `{repo}` مش موجود أو التوكن مالوش صلاحية وصول عليه.\n"
                "تأكد من اسم الريبو في github_repo وإن التوكن معطي صلاحية Contents: Read and write عليه."
            )
        if r.status_code == 422 and "already_exists" in r.text:
            # التاج ده مستخدم فعلاً على GitHub — جرّب الرقم اللي بعده
            tag_name = str(int(tag_name) + 1)
            continue

        try:
            r.raise_for_status()
            release_data = r.json()
        except requests.HTTPError as e:
            return False, f"❌ فشل إنشاء الـ Release:\n{e}\n{r.text[:300]}"
        break

    if release_data is None:
        return False, "❌ فشل إنشاء الـ Release: مفيش رقم إصدار فاضي بعد عدة محاولات، راجع الـ Releases يدويًا على GitHub."

    # الـ Release اتعمل بنجاح على GitHub بالفعل (التاج ده بقى مستخدم)،
    # فلازم نحفظ العداد دلوقتي حتى لو رفع الملف نفسه فشل بعد كده — عشان
    # المحاولة الجاية تاخد رقم جديد وميحصلش تعارض (tag already exists).
    CFG["release_counter"] = int(tag_name)
    save_config(CFG)

    upload_url  = release_data["upload_url"].split("{")[0]   # إزالة {?name,label}
    release_url = release_data["html_url"]

    # 2) رفع الـ APK
    upload_headers = {
        "Authorization": f"token {token}",
        "Content-Type" : "application/vnd.android.package-archive",
    }
    params = {"name": apk_original_name}
    try:
        with open(apk_path, "rb") as f:
            ru = requests.post(upload_url, headers=upload_headers, params=params, data=f, timeout=300)
            ru.raise_for_status()
    except requests.HTTPError as e:
        return False, f"❌ فشل رفع الملف:\n{e}\n{ru.text[:300]}"
    except Exception as e:
        return False, f"❌ خطأ أثناء الرفع:\n{e}"

    return True, f"✅ تم الرفع على GitHub بنجاح!\n🏷 الإصدار: `{tag_name}`\n📝 الاسم: `{release_name}`\n🔗 {release_url}"


# =============================================================================
# Firebase (نشر تطبيق على الموقع) — عبر REST API، بدون الحاجة لـ Service Account
# ─────────────────────────────────────────────────────────────────────────
# البوت بيسجّل دخول بإيميل/باسورد حساب أدمن حقيقي (عبر Identity Toolkit REST)
# وياخد idToken، وبعدين يستخدمه عشان يكتب مستند جديد في مجموعة "apps" على
# Firestore عبر REST API — بنفس منطق دالة saveApp() في صفحة الأدمن بالظبط.
#
# ملحوظة مهمة: "Authorized domains" في إعدادات Firebase Authentication بتتحكم
# بس في تسجيل الدخول عبر المتصفح (Google/OAuth Redirect)، ومالهاش أي علاقة
# بطلبات REST بيسجل بيها سيرفر بايثون دخول بإيميل/باسورد — فمفيش داعي نلمسها.
# اللي فعلاً بيحدد صلاحية الكتابة هو حساب الأدمن نفسه (لازم يكون الـ UID
# بتاعه مضاف في قواعد أمان Firestore عندك على أساس إنه أدمن).
# =============================================================================
_FB_TOKEN_CACHE = {"id_token": None, "refresh_token": None, "expires_at": 0}


def _firebase_sign_in_sync():
    api_key  = CFG.get("firebase_api_key", "")
    email    = CFG.get("firebase_admin_email", "")
    password = CFG.get("firebase_admin_password", "")
    if not api_key or not email or not password:
        raise RuntimeError(
            "بيانات Firebase ناقصة في config.json — لازم تحط firebase_admin_email "
            "و firebase_admin_password (وfirebase_api_key موجود افتراضيًا)."
        )
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
    r = requests.post(url, json={
        "email": email, "password": password, "returnSecureToken": True,
    }, timeout=30)
    if not r.ok:
        try:
            err = r.json().get("error", {}).get("message", r.text)
        except Exception:
            err = r.text
        raise RuntimeError(f"فشل تسجيل الدخول لـ Firebase: {err}")
    data = r.json()
    _FB_TOKEN_CACHE["id_token"]      = data["idToken"]
    _FB_TOKEN_CACHE["refresh_token"] = data["refreshToken"]
    _FB_TOKEN_CACHE["expires_at"]    = time.time() + int(data.get("expiresIn", "3600")) - 60
    return _FB_TOKEN_CACHE["id_token"]


def _firebase_refresh_sync():
    api_key = CFG.get("firebase_api_key", "")
    rtoken  = _FB_TOKEN_CACHE.get("refresh_token")
    if not rtoken:
        return _firebase_sign_in_sync()
    url = f"https://securetoken.googleapis.com/v1/token?key={api_key}"
    r = requests.post(url, data={
        "grant_type": "refresh_token", "refresh_token": rtoken,
    }, timeout=30)
    if not r.ok:
        # الـ refresh token نفسه ممكن يكون انتهى/اتلغى — نرجع نسجل دخول من الأول
        return _firebase_sign_in_sync()
    data = r.json()
    _FB_TOKEN_CACHE["id_token"]      = data["id_token"]
    _FB_TOKEN_CACHE["refresh_token"] = data["refresh_token"]
    _FB_TOKEN_CACHE["expires_at"]    = time.time() + int(data.get("expires_in", "3600")) - 60
    return _FB_TOKEN_CACHE["id_token"]


def _firebase_get_id_token_sync():
    if _FB_TOKEN_CACHE["id_token"] and time.time() < _FB_TOKEN_CACHE["expires_at"]:
        return _FB_TOKEN_CACHE["id_token"]
    if _FB_TOKEN_CACHE["refresh_token"]:
        return _firebase_refresh_sync()
    return _firebase_sign_in_sync()


def _to_firestore_value(v):
    """يحوّل قيمة بايثون لصيغة Firestore REST API."""
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: _to_firestore_value(v2) for k, v2 in v.items()}}}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_to_firestore_value(x) for x in v]}}
    return {"stringValue": str(v)}


def _from_firestore_value(v):
    """يحوّل قيمة Firestore (REST API) لقيمة بايثون عادية."""
    if not isinstance(v, dict):
        return None
    if "nullValue" in v:
        return None
    if "booleanValue" in v:
        return bool(v["booleanValue"])
    if "integerValue" in v:
        try:
            return int(v["integerValue"])
        except Exception:
            return 0
    if "doubleValue" in v:
        return v["doubleValue"]
    if "stringValue" in v:
        return v["stringValue"]
    if "timestampValue" in v:
        return v["timestampValue"]
    if "mapValue" in v:
        fields = v["mapValue"].get("fields", {}) or {}
        return {k: _from_firestore_value(v2) for k, v2 in fields.items()}
    if "arrayValue" in v:
        values = v["arrayValue"].get("values", []) or []
        return [_from_firestore_value(x) for x in values]
    return None


def firestore_query_app_by_field_sync(field_name: str, field_value: str) -> tuple[dict | None, str]:
    """يدور على أول مستند في مجموعة apps بحيث field_name == field_value، ويرجع
    (بيانات التطبيق أو None، رسالة تشخيص لو حصل خطأ)."""
    try:
        id_token = _firebase_get_id_token_sync()
    except Exception as e:
        return None, f"فشل تسجيل الدخول لـ Firebase: {e}"

    project_id = CFG.get("firebase_project_id", "")
    url = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents:runQuery"
    body = {
        "structuredQuery": {
            "from": [{"collectionId": "apps"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": field_name},
                    "op": "EQUAL",
                    "value": {"stringValue": field_value},
                }
            },
            "limit": 1,
        }
    }
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {id_token}"}, json=body, timeout=30)
        if r.status_code == 401:
            id_token = _firebase_sign_in_sync()
            r = requests.post(url, headers={"Authorization": f"Bearer {id_token}"}, json=body, timeout=30)
    except Exception as e:
        return None, f"خطأ في الاتصال بـ Firestore: {e}"

    if not r.ok:
        try:
            err = r.json()
            if isinstance(err, list) and err:
                err = err[0].get("error", err[0])
            if isinstance(err, dict):
                err = err.get("error", {}).get("message", err)
        except Exception:
            err = r.text
        return None, f"فشل الاستعلام ({field_name}={field_value}) — {r.status_code}: {err}"

    try:
        results = r.json()
    except Exception as e:
        return None, f"رد غير مفهوم من Firestore: {e}"

    for item in results:
        doc = item.get("document")
        if doc:
            fields = doc.get("fields", {}) or {}
            return {k: _from_firestore_value(v) for k, v in fields.items()}, ""
    return None, ""


def find_app_by_site_link_sync(link: str) -> tuple[dict | None, str]:
    """يستخرج قيمة ?app= من لينك صفحة التطبيق، ويدور عليها في Firestore
    أول بحقل seoSlug، وبعدين بمحاولة تحويلها لـ packageId (شرطات لنقط).
    يرجع (بيانات التطبيق أو None، تفاصيل تشخيص للعرض عند الفشل)."""
    try:
        parsed = urllib.parse.urlparse(link)
        qs = urllib.parse.parse_qs(parsed.query)
        slug = (qs.get("app", [""])[0] or "").strip()
    except Exception:
        slug = ""
    if not slug:
        return None, "اللينك ده مفيهوش \"?app=...\" — تأكد إنه لينك صفحة تطبيق صحيح."

    debug_lines = [f"🔎 الـ slug المستخرج من اللينك: `{slug}`"]

    app_data, err = firestore_query_app_by_field_sync("seoSlug", slug)
    debug_lines.append(f"— بحث بحقل seoSlug == {slug}: " + (err if err else ("لقيت تطبيق ✅" if app_data else "مفيش نتيجة")))
    if app_data:
        return app_data, ""
    if err:
        return None, "\n".join(debug_lines)

    guessed_pkg = slug.replace("-", ".")
    app_data, err = firestore_query_app_by_field_sync("packageId", guessed_pkg)
    debug_lines.append(f"— بحث بحقل packageId == {guessed_pkg}: " + (err if err else ("لقيت تطبيق ✅" if app_data else "مفيش نتيجة")))
    if app_data:
        return app_data, ""

    return None, "\n".join(debug_lines)


def firestore_add_document_sync(collection_name: str, data: dict) -> tuple[bool, str]:
    """يضيف مستند جديد لمجموعة معيّنة في Firestore عبر REST API، ويرجع (نجاح, رسالة/آيدي)."""
    try:
        id_token = _firebase_get_id_token_sync()
    except Exception as e:
        return False, f"❌ فشل تسجيل الدخول لـ Firebase:\n{e}"

    project_id = CFG.get("firebase_project_id", "")
    url = (
        f"https://firestore.googleapis.com/v1/projects/{project_id}"
        f"/databases/(default)/documents/{collection_name}"
    )
    fields = {k: _to_firestore_value(v) for k, v in data.items()}
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {id_token}"},
            json={"fields": fields},
            timeout=30,
        )
    except Exception as e:
        return False, f"❌ خطأ في الاتصال بـ Firestore:\n{e}"

    if r.status_code == 401:
        # الـ token ممكن يكون اتلغى فجأة — نجرب مرة واحدة تاني بعد تسجيل دخول جديد
        try:
            id_token = _firebase_sign_in_sync()
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {id_token}"},
                json={"fields": fields},
                timeout=30,
            )
        except Exception as e:
            return False, f"❌ فشل بعد إعادة تسجيل الدخول:\n{e}"

    if not r.ok:
        try:
            err = r.json().get("error", {}).get("message", r.text)
        except Exception:
            err = r.text
        return False, f"❌ فشل الحفظ في Firestore ({r.status_code}):\n{err}"

    doc_name = r.json().get("name", "")
    doc_id   = doc_name.rstrip("/").split("/")[-1] if doc_name else ""
    return True, doc_id


# =============================================================================
# القائمة الرئيسية
# =============================================================================
def main_menu_kb():
    rows = [
        [InlineKeyboardButton("📥 تنزيل APK من رابط", callback_data="menu_download_url")],
        [InlineKeyboardButton("🖊 تجميع وتوقيع", callback_data="menu_build")],
        [InlineKeyboardButton("🔍 بحث واستبدال smali", callback_data="menu_search")],
        [InlineKeyboardButton("📄 استبدال ملف/ملفات بالاسم (تلقائي)", callback_data="menu_search_filename")],
        [InlineKeyboardButton("📍 استبدال ملف بمسار محدد (يدوي)", callback_data="menu_path_replace")],
        [InlineKeyboardButton("📤 نشر تطبيق على الموقع", callback_data="menu_publish_app")],
        [InlineKeyboardButton("📢 نشر تطبيق في قناة تيليجرام (بلينك)", callback_data="menu_channel_publish_link")],
        [InlineKeyboardButton("🆔 تحديث آيدي/يوزر القناة", callback_data="menu_update_channel_id")],
        [InlineKeyboardButton("📦 نقل classes.zip", callback_data="menu_classes")],
        [InlineKeyboardButton("🔑 إدارة شهادات التوقيع", callback_data="menu_keystores")],
        [InlineKeyboardButton("🐙 تحديث توكن GitHub", callback_data="menu_update_github_token")],
        [InlineKeyboardButton("🆔 تحديث User UID", callback_data="menu_update_owner_uid")],
        [InlineKeyboardButton("📧 تحديث بيانات دخول Firebase", callback_data="menu_update_firebase_login")],
        [InlineKeyboardButton("☕ فحص/تحميل Java", callback_data="menu_check_java")],
        [InlineKeyboardButton("🛠 فحص/تحميل أدوات APK", callback_data="menu_check_tools")],
        [InlineKeyboardButton("🗑 حذف المشروع الحالي", callback_data="menu_delete_project")],
        [
            InlineKeyboardButton("♻️ إعادة ستارت", callback_data="menu_soft_restart"),
            InlineKeyboardButton("🔄 ريستارت كامل", callback_data="menu_full_restart"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def project_status_text():
    has_project = os.path.isdir(PROJECT_DIR)
    has_apk     = os.path.isfile(APK_COPY_PATH)
    return (
        f"📂 مشروع حالي: {'✅ موجود' if has_project else '❌ لا يوجد'}\n"
        f"📱 نسخة الـ APK الأصلية: {'✅ موجودة' if has_apk else '❌ غير موجودة'}"
    )


def upload_destination_kb():
    """كيبورد اختيار وجهة الرفع بعد التوقيع"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 إرسال على تيليجرام", callback_data="upload_telegram")],
        [InlineKeyboardButton("🐙 رفع على GitHub",      callback_data="upload_github")],
        [InlineKeyboardButton("🚀 الاتنين معاً",        callback_data="upload_both")],
        [InlineKeyboardButton("⬅️ رجوع",               callback_data="back_main")],
    ])


# =============================================================================
# /start
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔️ غير مسموح لك باستخدام هذا البوت.")
        return

    if not CFG.get("admin_ids"):
        await update.message.reply_text(
            "⚠️ تنبيه أمني: مفيش admin_ids محدد في config.json، يعني أي شخص يقدر يستخدم البوت دلوقتي.\n"
            f"عشان تأمّنه، حط آيدي تيليجرام بتاعك ({uid}) جوه admin_ids في config.json وأعد تشغيل البوت."
        )

    reset_state(uid)
    await update.message.reply_text(
        "👋 أهلاً بيك في بوت فك وتجميع وتوقيع APK.\n\n"
        + project_status_text()
        + f"\n\n🆔 نسخة التشغيل: `{INSTANCE_ID}` (اشتغلت الساعة {BOOT_TIME})\n"
          "(لو دوست /start تاني وطلعلك معرّف مختلف كل مرة بسرعة، معناه فيه أكتر من نسخة بتتقاتل)"
        + "\n\n📥 ابعت ملف APK دلوقتي وأنا هبدأ أفكه تلقائي، أو اختار من القائمة:",
        reply_markup=main_menu_kb(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# =============================================================================
# استقبال الملفات
# =============================================================================
async def download_document_safe(update: Update, doc, dest_path: str) -> bool:
    """بتنزّل ملف تليجرام لمسار معين، وبترجع True لو نجحت. لو الملف أكبر من
    حد الـ Bot API (20MB) بتبعت للمستخدم رسالة واضحة توضح إن ده حد ثابت
    ومش هيتحل بإعادة المحاولة، وبترجع False بدل ما تسيب الاستثناء يتفلت
    لمعالج الأخطاء العام ويتصنّف غلط كـ"تأخير شبكة عابر"."""
    from telegram.error import BadRequest
    try:
        f = await doc.get_file()
        await f.download_to_drive(dest_path)
        return True
    except BadRequest as e:
        if "file is too big" in str(e).lower():
            await update.message.reply_text(
                "🚫 الملف ده أكبر من 20MB، وده أقصى حجم يقدر بوت تليجرام العادي "
                "ينزّله (حد ثابت من تليجرام مش مشكلة شبكة). ابعت ملف أصغر، أو "
                "لو محتاج تنزّل ملفات أكبر كلم محمد يشغّل Local Bot API Server.",
                reply_markup=main_menu_kb(),
            )
        else:
            await update.message.reply_text(
                f"❌ فشل تنزيل الملف: {e}", reply_markup=main_menu_kb(),
            )
        return False


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    doc  = update.message.document
    st   = get_state(uid)
    name = (doc.file_name or "").lower()

    if st.get("await") == "classes_zip_upload":
        if not name.endswith(".zip"):
            await update.message.reply_text("❌ من فضلك ابعت ملف .zip فقط.")
            return
        if not await download_document_safe(update, doc, CLASSES_ZIP_PATH):
            return
        st.pop("await", None)
        await update.message.reply_text("✅ تم استبدال classes.zip بنجاح.", reply_markup=main_menu_kb())
        return

    if st.get("await") == "keystore_file_upload":
        if not (name.endswith(".jks") or name.endswith(".keystore")):
            await update.message.reply_text("❌ من فضلك ابعت ملف keystore بصيغة .jks أو .keystore.")
            return
        ks_name = st.get("new_ks_name") or random_string(6)
        dest    = os.path.join(KEYSTORES_DIR, f"{ks_name}.jks")
        if not await download_document_safe(update, doc, dest):
            return
        st["new_ks_path"] = dest
        st["await"]       = "keystore_alias"
        await update.message.reply_text("✅ تم استلام ملف الـ keystore.\n✍️ دلوقتي اكتب الـ Alias:")
        return

    if st.get("await") == "path_replace_upload":
        target_rel = st.get("path_replace_target")
        st.pop("await", None)
        st.pop("path_replace_target", None)
        if not target_rel or not os.path.isdir(PROJECT_DIR):
            await update.message.reply_text("❌ حصل خطأ (المسار مش متاح دلوقتي)، جرب تاني من القائمة.", reply_markup=main_menu_kb())
            return

        tmp_dir = os.path.join(WORKSPACE_DIR, f"pathreplace_{random_string(6)}")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, doc.file_name or "file")
        if not await download_document_safe(update, doc, tmp_path):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        result = await asyncio.to_thread(replace_file_sync, PROJECT_DIR, target_rel, tmp_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await update.message.reply_text(result, reply_markup=main_menu_kb())
        return

    if st.get("await") == "filename_replace_upload":
        if not os.path.isdir(PROJECT_DIR):
            st.pop("await", None)
            await update.message.reply_text("❌ لا يوجد مشروع مفكوك حالياً.", reply_markup=main_menu_kb())
            return

        tmp_dir = os.path.join(WORKSPACE_DIR, f"fnreplace_{random_string(6)}")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, doc.file_name or "file")
        if not await download_document_safe(update, doc, tmp_path):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        incoming = {}  # اسم الملف → مسار الملف على الديسك
        if name.endswith(".zip"):
            try:
                extract_dir = os.path.join(tmp_dir, "extracted")
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(extract_dir)
                    for member in zf.namelist():
                        if member.endswith("/"):
                            continue
                        fn = os.path.basename(member)
                        if not fn:
                            continue
                        incoming[fn] = os.path.join(extract_dir, member)
            except Exception as e:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                st.pop("await", None)
                await update.message.reply_text(f"❌ فشل فتح ملف الـ ZIP:\n{e}", reply_markup=main_menu_kb())
                return
        else:
            incoming[doc.file_name or "file"] = tmp_path

        st.pop("await", None)

        # ── بحث فقط (من غير أي تعديل) عن مكان كل اسم ملف داخل المشروع ──
        matches = await asyncio.to_thread(find_filename_matches_sync, PROJECT_DIR, incoming)

        report = {"replaced": [], "skipped": []}
        queue = []  # الملفات اللي اسمها موجود في أكتر من مكان، وعايزة تحديد يدوي
        for fname, local_path in incoming.items():
            rel_list = matches.get(fname, [])
            if not rel_list:
                report["skipped"].append((fname, "مفيش ملف بنفس الاسم في المشروع"))
            elif len(rel_list) == 1:
                # مكان واحد بس بنفس الاسم → مفيش لبس، استبدال مباشر
                result = await asyncio.to_thread(replace_file_sync, PROJECT_DIR, rel_list[0], local_path)
                if result.startswith("✅"):
                    report["replaced"].append((fname, rel_list[0]))
                else:
                    report["skipped"].append((fname, "فشل الاستبدال"))
            else:
                # الاسم ده موجود في أكتر من مكان → لازم تحدد إنت أنهي مكان بالظبط
                queue.append({"fname": fname, "local_path": local_path, "rel_paths": rel_list})

        st["fnreplace_report"]  = report
        st["fnreplace_queue"]   = queue
        st["fnreplace_tmp_dir"] = tmp_dir

        if queue:
            item = queue[0]
            st["fnreplace_current"] = item
            await update.message.reply_text(
                format_fnreplace_prompt(item["fname"], item["rel_paths"]),
                reply_markup=build_fnreplace_kb(item["rel_paths"]),
            )
        else:
            await update.message.reply_text(finalize_fnreplace_report(st), reply_markup=main_menu_kb())
        return

    if name.endswith(".apk"):
        ok_jar, jar_msg = verify_jar_file(APKTOOL_JAR)
        if not ok_jar:
            await update.message.reply_text(jar_msg)
            return

        # ── احفظ الاسم الأصلي للملف عشان نستخدمه في GitHub Release ──
        original_apk_name = doc.file_name or "app.apk"
        get_state(uid)["original_apk_name"] = original_apk_name

        msg = await update.message.reply_text("⏳ جاري تحميل ملف الـ APK...")
        if not await download_document_safe(update, doc, APK_COPY_PATH):
            await msg.delete()
            return

        if os.path.isdir(PROJECT_DIR):
            shutil.rmtree(PROJECT_DIR, ignore_errors=True)

        await msg.edit_text("⏳ جاري فك الـ APK (baksmali - أكواد فقط)...")
        apk_size = os.path.getsize(APK_COPY_PATH)
        ok, log_text, elapsed = await run_async_with_heartbeat(
            decompile_apk_fast(APK_COPY_PATH, PROJECT_DIR),
            msg, "⏳ جاري فك الـ APK (baksmali - أكواد فقط)...",
        )
        if ok:
            record_stage_time("decompile", apk_size, elapsed)

        if ok:
            await msg.edit_text(f"✅ تم فك الـ APK بنجاح في {format_duration(elapsed)}.\n" + tail_log(log_text))
            await update.message.reply_text("📋 القائمة الرئيسية:", reply_markup=main_menu_kb())
        else:
            await msg.edit_text("❌ فشلت عملية الفك:\n" + tail_log(log_text, 1500), reply_markup=main_menu_kb())
        return

    await update.message.reply_text(
        "❓ نوع الملف غير معروف هنا دلوقتي.\nابعت ملف APK، أو .zip لو طلبت استبدال classes.zip، "
        "أو .jks لو بتضيف توقيع يدوي."
    )


# =============================================================================
# الرسائل النصية
# =============================================================================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    if not is_admin(uid):
        return

    st       = get_state(uid)
    awaiting = st.get("await")
    text     = update.message.text.strip()

    if awaiting == "path_replace_query":
        await handle_path_replace_query(update, st, text)
        return

    if awaiting == "new_github_token":
        new_token = text
        st.pop("await", None)

        # امسح رسالة المستخدم اللي فيها التوكن فورًا — أمان، عشان
        # التوكن ميفضلش نص ظاهر في تاريخ الشات.
        try:
            await update.message.delete()
        except Exception:
            pass

        status_msg = await context.bot.send_message(
            update.effective_chat.id, "⏳ جاري التحقق من التوكن..."
        )

        # فحص سريع: التوكن شغال ولا لأ (401 = مرفوض من GitHub)
        try:
            check = requests.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {new_token}"},
                timeout=15,
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ فشل الاتصال بـ GitHub للتحقق من التوكن:\n{e}")
            return

        if check.status_code == 401:
            await status_msg.edit_text(
                "❌ التوكن مرفوض من GitHub (401 Bad credentials).\n"
                "اتأكد إنك نسخته كامل من غير مسافات، وإنه لسه صالح ومش ملغي، وحاول تاني.",
                reply_markup=main_menu_kb(),
            )
            return

        # احفظ التوكن مباشرة في config.json (تحديث صريح ومقصود من الأدمن)
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            else:
                existing = dict(DEFAULT_CONFIG)
        except Exception:
            existing = dict(DEFAULT_CONFIG)
        existing["github_token"] = new_token
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        CFG["github_token"] = new_token

        repo = CFG.get("github_repo", "").strip()
        extra_note = ""
        if repo:
            r2 = requests.get(
                f"https://api.github.com/repos/{repo}",
                headers={"Authorization": f"token {new_token}"},
                timeout=15,
            )
            if r2.status_code == 404:
                extra_note = (
                    f"\n⚠️ تحذير: التوكن شغال، لكن مفيش وصول للريبو `{repo}` — "
                    "تأكد إن التوكن ده معطي صلاحية Contents: Read and write على الريبو ده بالتحديد."
                )

        await status_msg.edit_text(
            "✅ تم حفظ توكن GitHub الجديد والتحقق منه بنجاح." + extra_note,
            reply_markup=main_menu_kb(),
        )
        return

    if awaiting == "new_owner_uid":
        new_uid = text.strip()
        st.pop("await", None)

        if not new_uid or " " in new_uid or len(new_uid) < 6:
            await update.message.reply_text(
                "❌ ده مش شكل UID سليم (متوقع نص متصل بدون فراغات، ~28 حرف عادةً). "
                "حاول تاني أو رجّع من القائمة الرئيسية.",
                reply_markup=main_menu_kb(),
            )
            return

        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            else:
                existing = dict(DEFAULT_CONFIG)
        except Exception:
            existing = dict(DEFAULT_CONFIG)
        existing["firebase_owner_uid"] = new_uid
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        CFG["firebase_owner_uid"] = new_uid

        await update.message.reply_text(
            f"✅ تم حفظ User UID بنجاح:\n`{new_uid}`\n\n"
            "هيتضاف تلقائيًا كحقل \"ownerUid\" في أي تطبيق تنشره من دلوقتي.",
            reply_markup=main_menu_kb(),
        )
        return

    if awaiting == "new_channel_id":
        new_channel = text.strip()
        st.pop("await", None)

        if not new_channel or " " in new_channel:
            await update.message.reply_text(
                "❌ ده مش شكل صحيح. ابعت يوزر القناة (مثال: `@my_channel`) أو آيديها الرقمي "
                "(مثال: `-1001234567890`) من غير مسافات.",
                reply_markup=main_menu_kb(),
                parse_mode="Markdown",
            )
            return

        if not (new_channel.startswith("@") or new_channel.lstrip("-").isdigit()):
            await update.message.reply_text(
                "❌ لازم يبدأ بـ @ (لو يوزر) أو يكون رقم آيدي (ممكن يبدأ بـ -). حاول تاني:",
            )
            return

        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            else:
                existing = dict(DEFAULT_CONFIG)
        except Exception:
            existing = dict(DEFAULT_CONFIG)
        existing["telegram_channel_id"] = new_channel
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        CFG["telegram_channel_id"] = new_channel

        await update.message.reply_text(
            f"✅ تم حفظ آيدي/يوزر القناة بنجاح:\n`{new_channel}`\n\n"
            "تأكد إن البوت أدمن في القناة دي ومعاه صلاحية \"نشر رسائل\".",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown",
        )
        return

    if awaiting == "channel_publish_link":
        link = text.strip()
        st.pop("await", None)

        if not link.lower().startswith(("http://", "https://")):
            await update.message.reply_text(
                "❌ ابعت لينك صحيح لصفحة التطبيق على الموقع (يبدأ بـ http/https):",
                reply_markup=main_menu_kb(),
            )
            return

        status_msg = await update.message.reply_text("⏳ جاري البحث عن بيانات التطبيق...")
        app_data, debug_info = await asyncio.to_thread(find_app_by_site_link_sync, link)

        if not app_data:
            await status_msg.edit_text(
                "❌ مقدرتش ألاقي تطبيق منشور على الموقع بهذا اللينك.\n\n"
                + (debug_info or "تأكد إن اللينك فيه \"?app=...\" وإن التطبيق ده منشور فعلًا."),
            )
            await context.bot.send_message(update.effective_chat.id, "📋 القائمة الرئيسية:", reply_markup=main_menu_kb())
            return

        await status_msg.edit_text("⏳ جاري النشر في القناة...")
        ok, msg = await post_app_to_channel(context.bot, app_data, site_url=link)
        await context.bot.send_message(update.effective_chat.id, msg, reply_markup=main_menu_kb())
        return

    if awaiting == "new_firebase_email":
        new_email = text.strip()
        if "@" not in new_email or " " in new_email:
            await update.message.reply_text("❌ ده مش شكل إيميل سليم. ابعت الإيميل تاني:")
            return
        st["_firebase_email_temp"] = new_email
        st["await"] = "new_firebase_password"
        await update.message.reply_text(
            f"📧 الإيميل: `{new_email}`\n\n"
            "دلوقتي ابعت الباسورد بتاع نفس الحساب.\n"
            "⚠️ همسح رسالتك اللي فيها الباسورد فورًا من الشات بعد الحفظ.",
        )
        return

    if awaiting == "new_firebase_password":
        new_password = text
        new_email    = st.get("_firebase_email_temp", "").strip()
        st.pop("await", None)
        st.pop("_firebase_email_temp", None)

        # امسح رسالة المستخدم اللي فيها الباسورد فورًا — أمان.
        try:
            await update.message.delete()
        except Exception:
            pass

        if not new_email:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ حصل خطأ: مفيش إيميل محفوظ من الخطوة اللي فاتت. ابدأ تاني من القائمة.",
                reply_markup=main_menu_kb(),
            )
            return

        status_msg = await context.bot.send_message(
            update.effective_chat.id, "⏳ جاري التحقق من بيانات الدخول..."
        )

        # فحص حقيقي: تسجيل دخول تجريبي بنفس بيانات Identity Toolkit REST
        # المستخدمة فعليًا في نشر التطبيقات، عشان نتأكد إنها شغالة قبل الحفظ.
        api_key = CFG.get("firebase_api_key", "")
        try:
            check = requests.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}",
                json={"email": new_email, "password": new_password, "returnSecureToken": True},
                timeout=20,
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ فشل الاتصال بـ Firebase للتحقق من البيانات:\n{e}")
            return

        if not check.ok:
            try:
                err = check.json().get("error", {}).get("message", check.text)
            except Exception:
                err = check.text
            await status_msg.edit_text(
                f"❌ تسجيل الدخول فشل ({err}).\n"
                "اتأكد إن الإيميل والباسورد صح، وإن الحساب ده متسجّل فعلاً في "
                "Firebase Authentication، وحاول تاني من القائمة.",
                reply_markup=main_menu_kb(),
            )
            return

        # البيانات شغالة فعلاً → احفظها في config.json
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            else:
                existing = dict(DEFAULT_CONFIG)
        except Exception:
            existing = dict(DEFAULT_CONFIG)
        existing["firebase_admin_email"]    = new_email
        existing["firebase_admin_password"] = new_password
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        CFG["firebase_admin_email"]    = new_email
        CFG["firebase_admin_password"] = new_password

        # نفضّي الكاش القديم لأي توكن دخول سابق، عشان أول عملية نشر بعد كده
        # تستخدم الحساب الجديد فورًا بدل ما تفضل شغالة على توكن الحساب القديم.
        _FB_TOKEN_CACHE["id_token"]      = None
        _FB_TOKEN_CACHE["refresh_token"] = None
        _FB_TOKEN_CACHE["expires_at"]    = 0

        await status_msg.edit_text(
            "✅ تم حفظ بيانات دخول Firebase الجديدة والتحقق منها بنجاح.",
            reply_markup=main_menu_kb(),
        )
        return

    if awaiting in ("new_ks_name_for_build", "new_ks_name_standalone"):
        if find_keystore(text):
            await update.message.reply_text("⚠️ الاسم ده مستخدم قبل كده، اكتب اسم تاني.")
            return
        st["new_ks_name"] = text
        st["ks_purpose"]  = "build" if awaiting == "new_ks_name_for_build" else "standalone"
        st["await"]       = "keystore_file_upload"
        await update.message.reply_text("📤 دلوقتي ابعت ملف keystore (.jks أو .keystore):")
        return

    if awaiting == "keystore_alias":
        st["new_ks_alias"] = text
        st["await"]        = "keystore_storepass"
        await update.message.reply_text("🔑 اكتب Store password:")
        return

    if awaiting == "keystore_storepass":
        st["new_ks_storepass"] = text
        st["await"]            = "keystore_keypass"
        await update.message.reply_text("🔑 اكتب Key password (أو اكتب - لاستخدام نفس الـ Store password):")
        return

    if awaiting == "keystore_keypass":
        keypass   = text if text != "-" else st.get("new_ks_storepass", "")
        new_entry = {
            "name"     : st["new_ks_name"],
            "path"     : st["new_ks_path"],
            "alias"    : st.get("new_ks_alias", ""),
            "storepass": st.get("new_ks_storepass", ""),
            "keypass"  : keypass,
        }
        CFG.setdefault("keystores", []).append(new_entry)
        save_config(CFG)
        purpose  = st.get("ks_purpose")
        ks_name  = new_entry["name"]
        await update.message.reply_text(f"✅ تم حفظ التوقيع \"{ks_name}\" بنجاح.")
        reset_state(uid)
        if purpose == "build":
            status_msg = await update.message.reply_text("⏳ جاري التجميع والتوقيع...")
            await do_build_and_sign(context, update.effective_chat.id, status_msg, uid, mode="custom", ks_name=ks_name)
        else:
            await update.message.reply_text("📋 القائمة الرئيسية:", reply_markup=main_menu_kb())
        return

    if awaiting == "download_page_url":
        st["download_page_url"] = text
        st["await"] = "download_direct_url"
        await update.message.reply_text(
            "✅ تمام!\n\n"
            "الخطوة 2️⃣: انسخ رابط التنزيل المباشر بتاع ملف الـ APK نفسه وابعته هنا.\n"
            "(ملاحظة: مش مطلوب منك أي توكن بوت أو GitHub — الرابط ده بس بيحتوي "
            "كلمة token= جواه كجزء من رابط الموقع، ده طبيعي وجزء من الرابط.)"
        )
        return

    if awaiting == "download_direct_url":
        page_url     = st.get("download_page_url", "")
        download_url = text
        st.pop("await", None)
        st.pop("download_page_url", None)
        await download_apk_from_url(context, update.effective_chat.id, uid, page_url, download_url)
        return

    if awaiting == "search_keyword":
        st["search_text"] = text
        st.pop("await", None)
        kb = [
            [InlineKeyboardButton("🔍 بحث فقط (عرض النتائج)", callback_data="search_only")],
            [InlineKeyboardButton("🔁 استبدال النص",           callback_data="search_replace")],
            [InlineKeyboardButton("➕ إضافة سطر تحته",         callback_data="search_insert")],
            [InlineKeyboardButton("🗑 حذف النص من كل الملفات", callback_data="search_delete")],
            [InlineKeyboardButton("⬅️ رجوع",                  callback_data="back_main")],
        ]
        await update.message.reply_text(f"تمام، النص:\n\"{text}\"\n\nعايز تعمل فيه إيه؟", reply_markup=InlineKeyboardMarkup(kb))
        return

    if awaiting == "search_replace_with":
        st["replace_text"] = text
        st.pop("await", None)
        target = st.get("search_target_file")
        target_note = f"في الملف:\n📄 {target}" if target else "في كل الملفات المطابقة"
        kb = [[
            InlineKeyboardButton("✅ تأكيد الاستبدال", callback_data="confirm_replace"),
            InlineKeyboardButton("❌ إلغاء",           callback_data="back_main"),
        ]]
        await update.message.reply_text(
            f"استبدال:\n\"{st.get('search_text','')}\"    \nبـ:\n\"{text}\"\n\n{target_note}. تأكيد؟",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if awaiting == "search_insert_with":
        st["insert_text"] = text
        st.pop("await", None)
        target = st.get("search_target_file")
        target_note = f"في الملف:\n📄 {target}" if target else "في كل الملفات المطابقة"
        kb = [[
            InlineKeyboardButton("✅ تأكيد الإضافة", callback_data="confirm_insert"),
            InlineKeyboardButton("❌ إلغاء",         callback_data="back_main"),
        ]]
        await update.message.reply_text(
            f"عند كل سطر فيه:\n\"{st.get('search_text','')}\"\nهيتم إضافة سطر جديد تحته:\n\"{text}\"\n\n{target_note}. تأكيد؟",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if awaiting == "publish_autofill_url":
        st.pop("await", None)
        url = text.strip()
        if not url.lower().startswith(("http://", "https://")):
            await update.message.reply_text("❌ ده مش رابط سليم، لازم يبدأ بـ http:// أو https://. ابعته تاني:")
            st["await"] = "publish_autofill_url"
            return
        await start_publish_autofill(context, update.effective_chat.id, st, url)
        return

    if awaiting == "publish_field_edit":
        await handle_publish_field_text(update, context, st, text)
        return

    await update.message.reply_text("اختار من القائمة:", reply_markup=main_menu_kb())


# =============================================================================
# منطق التجميع والتوقيع + اختيار الوجهة
# =============================================================================
async def do_build_and_sign(context, chat_id, status_msg, uid, mode, ks_name=None):
    """
    يجمّع المشروع ويوقّعه، ثم يسأل المستخدم:
    تيليجرام / GitHub / الاتنين
    """
    if not os.path.isdir(PROJECT_DIR):
        await status_msg.edit_text("❌ لا يوجد مشروع مفكوك حالياً.")
        return
    problems = check_project_integrity(PROJECT_DIR)
    if problems:
        await status_msg.edit_text(project_integrity_report_text(problems), reply_markup=main_menu_kb())
        return
    ok_jar, jar_msg = verify_jar_file(SMALI_JAR)
    if not ok_jar:
        await status_msg.edit_text(jar_msg)
        return
    ok_jar, jar_msg = verify_jar_file(UBER_SIGNER_JAR)
    if not ok_jar:
        await status_msg.edit_text(jar_msg)
        return

    unsigned_apk = os.path.join(WORKSPACE_DIR, "unsigned_" + random_string(6) + ".apk")
    out_apk      = os.path.join(WORKSPACE_DIR, "signed_" + random_string(6) + ".apk")

    dist_dir = os.path.join(PROJECT_DIR, "dist")
    if os.path.isdir(dist_dir):
        shutil.rmtree(dist_dir, ignore_errors=True)

    project_size = get_dir_size(PROJECT_DIR)
    await status_msg.edit_text("⏳ 1) جاري تجميع المشروع (smali - أكواد فقط)...")
    ok, log_text, build_elapsed = await run_async_with_heartbeat(
        build_apk_fast(PROJECT_DIR, unsigned_apk),
        status_msg, "⏳ 1) جاري تجميع المشروع (smali - أكواد فقط)...",
    )
    if ok:
        record_stage_time("build", project_size, build_elapsed)
    if not ok or not os.path.isfile(unsigned_apk):
        await status_msg.edit_text("❌ فشل التجميع:\n" + tail_log(log_text), reply_markup=main_menu_kb())
        return

    build_done_text = f"✅ 1) تم التجميع في {format_duration(build_elapsed)}."
    await status_msg.edit_text(f"{build_done_text}\n⏳ 2) جاري التوقيع (uber-apk-signer)...")

    tmp_out_dir = os.path.join(WORKSPACE_DIR, "sign_tmp_" + random_string(6))
    os.makedirs(tmp_out_dir, exist_ok=True)
    cmd = [JAVA_BIN, *JAVA_MEM_OPTS, "-jar", UBER_SIGNER_JAR, "--apks", unsigned_apk, "--out", tmp_out_dir, "--allowResign"]

    if mode == "custom":
        ks = find_keystore(ks_name)
        if not ks:
            shutil.rmtree(tmp_out_dir, ignore_errors=True)
            await status_msg.edit_text("❌ التوقيع المحدد غير موجود.", reply_markup=main_menu_kb())
            return
        cmd += [
            "--ks", ks["path"],
            "--ksAlias", ks["alias"],
            "--ksPass", ks["storepass"],
            "--ksKeyPass", ks.get("keypass") or ks["storepass"],
        ]

    unsigned_size = os.path.getsize(unsigned_apk)
    est_sign = estimate_seconds("sign", unsigned_size)
    ok, sign_log, sign_elapsed = await run_cmd_with_heartbeat(
        cmd, status_msg, f"{build_done_text}\n⏳ 2) جاري التوقيع (uber-apk-signer)...",
        estimated_seconds=est_sign, stage="sign", size_bytes=unsigned_size,
    )
    try:
        os.remove(unsigned_apk)
    except Exception:
        pass

    if not ok:
        shutil.rmtree(tmp_out_dir, ignore_errors=True)
        await status_msg.edit_text("❌ فشل التوقيع:\n" + tail_log(sign_log), reply_markup=main_menu_kb())
        return

    apks_found = [f for f in os.listdir(tmp_out_dir) if f.lower().endswith(".apk")]
    if not apks_found:
        shutil.rmtree(tmp_out_dir, ignore_errors=True)
        await status_msg.edit_text("❌ لم يتم إنتاج ملف APK موقّع.", reply_markup=main_menu_kb())
        return

    signed_name = max(apks_found, key=lambda f: os.path.getmtime(os.path.join(tmp_out_dir, f)))
    shutil.move(os.path.join(tmp_out_dir, signed_name), out_apk)
    shutil.rmtree(tmp_out_dir, ignore_errors=True)

    # ── حفظ مسار الـ APK الموقّع في حالة المستخدم ──
    st = get_state(uid)
    st["signed_apk_path"]   = out_apk
    st["original_apk_name"] = st.get("original_apk_name") or "signed_app.apk"

    total_elapsed = build_elapsed + sign_elapsed
    await status_msg.edit_text(
        "✅ تم التجميع والتوقيع بنجاح!\n"
        f"⏱ التجميع: {format_duration(build_elapsed)}  |  التوقيع: {format_duration(sign_elapsed)}  |  الإجمالي: {format_duration(total_elapsed)}\n\n"
        "📤 فين عايز ترفع الـ APK؟",
        reply_markup=upload_destination_kb(),
    )
    st["active_upload_msg_id"] = status_msg.message_id


async def deliver_signed_apk(context, chat_id, uid, destination: str):
    """
    يسلّم الـ APK الموقّع حسب الوجهة المختارة:
    'telegram' | 'github' | 'both'
    """
    st            = get_state(uid)
    out_apk       = st.get("signed_apk_path", "")
    original_name = st.get("original_apk_name", "signed_app.apk")

    if not out_apk or not os.path.isfile(out_apk):
        await context.bot.send_message(chat_id=chat_id, text="❌ مش لاقي ملف الـ APK الموقّع.")
        return

    sent_telegram = False
    sent_github   = False
    msgs          = []

    # ── تيليجرام ──
    if destination in ("telegram", "both"):
        try:
            with open(out_apk, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=original_name,
                    caption="✅ تطبيقك جاهز وموقّع.",
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )
            sent_telegram = True
            msgs.append("📱 تم الإرسال على تيليجرام ✅")
        except Exception as e:
            msgs.append(f"📱 فشل الإرسال على تيليجرام ❌\nالسبب: {e}")

    # ── GitHub ──
    if destination in ("github", "both"):
        ok, gh_msg = await asyncio.to_thread(github_upload_sync, out_apk, original_name)
        msgs.append(gh_msg)
        if ok:
            sent_github = True

    # ── تحديد نجاح الرفع حسب الوجهة ──
    if destination == "telegram":
        all_success = sent_telegram
    elif destination == "github":
        all_success = sent_github
    else:  # both — نجح لو واحد منهم على الأقل نجح
        all_success = sent_telegram or sent_github

    # ── تنظيف الملفات المؤقتة بس لو نجح الرفع (نسيب مجلد المشروع نفسه،
    # وهنسأل المستخدم بعدين يمسحه ولا لأ) ──
    if all_success:
        try:
            os.remove(out_apk)
        except Exception:
            pass
        try:
            os.remove(APK_COPY_PATH)
        except Exception:
            pass
        st.pop("signed_apk_path",  None)
        st.pop("original_apk_name", None)
        cleanup_note = ""
    else:
        # فشل — اعرض زرار لإعادة المحاولة
        cleanup_note = "\n\n⚠️ المشروع والملف الموقّع لسه موجودين، تقدر تحاول تاني."

    summary = "\n\n".join(msgs)

    # زرار إعادة المحاولة لو في فشل
    if not all_success:
        retry_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 حاول تاني", callback_data=f"retry_upload_{destination}")],
            [InlineKeyboardButton("📱 جرب تيليجرام بس", callback_data="upload_telegram")],
            [InlineKeyboardButton("🐙 جرب GitHub بس",   callback_data="upload_github")],
            [InlineKeyboardButton("⬅️ رجوع",            callback_data="back_main")],
        ])
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{summary}{cleanup_note}",
            reply_markup=retry_kb,
        )
        # نحدّث الرسالة "الفعّالة" لآخر رسالة رفع ظهرت - أي زرار رفع من
        # رسالة أقدم هيتم تجاهله تلقائيًا (الحماية اللي فوق في on_callback).
        st["active_upload_msg_id"] = sent.message_id
    else:
        st.pop("active_upload_msg_id", None)
        # ── بعد نجاح الرفع مباشرة: نسأل المستخدم يمسح مجلد المشروع (اللي
        # اتفك) دلوقتي ولا يسيبه — بدل ما كان بيتمسح تلقائي من غير سؤال ──
        if os.path.isdir(PROJECT_DIR):
            del_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 امسح المشروع",  callback_data="delproj_after_upload:yes"),
                InlineKeyboardButton("📂 سيبه موجود",   callback_data="delproj_after_upload:no"),
            ]])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{summary}{cleanup_note}\n\n🗑 تمسح ملفات المشروع اللي اتفك دلوقتي ولا تسيبه؟",
                reply_markup=del_kb,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{summary}{cleanup_note}",
                reply_markup=main_menu_kb(),
            )

    # نظّف حالة المستخدم لو نجح الرفع
    if all_success:
        st.pop("signed_apk_path",   None)
        st.pop("original_apk_name", None)


# =============================================================================
# start_build_flow + show_keystores_for_build
# =============================================================================
def check_project_integrity(project_dir):
    """
    فحص سريع لسلامة المشروع المفكوك قبل محاولة التجميع، عشان لو المستخدم
    عدّل/مسح حاجة يدوي جوه مجلد المشروع (زي حذف/تعديل smali)
    نقدر نقوله بالظبط المشكلة فين على طول، بدل ما ننتظر smali يفشل
    بعد دقايق برسالة تقنية طويلة وغامضة.

    ملحوظة: الفك بقى بطريقة baksmali (أكواد فقط)، فمفيش AndroidManifest.xml
    ولا apktool.yml جوه المشروع خالص - الموارد والـ manifest متخزنين
    كاملين جوه original.apk (نسخة من الـ APK الأصلي) وده اللي بنتأكد
    من وجوده هنا بدلهم.

    بيرجّع list من رسائل المشاكل (فاضية = المشروع سليم ظاهريًا).
    """
    problems = []

    if not os.path.isdir(project_dir):
        return ["❌ مجلد المشروع نفسه مش موجود."]

    original_apk = os.path.join(project_dir, "original.apk")
    if not os.path.isfile(original_apk):
        problems.append(
            "• ملف original.apk (نسخة الاحتياط من الـ APK الأصلي) مش موجود.\n"
            "  ده الملف اللي فيه الموارد والصور والـ manifest، ومحتاجينه وقت التجميع."
        )
    elif os.path.getsize(original_apk) == 0:
        problems.append("• ملف original.apk موجود لكنه فاضي (0 بايت).")
    elif not zipfile.is_zipfile(original_apk):
        problems.append("• ملف original.apk موجود لكنه تالف (مش zip/apk صحيح).")

    smali_dirs = sorted(
        d for d in glob.glob(os.path.join(project_dir, "smali*"))
        if os.path.isdir(d)
    )
    if not smali_dirs:
        problems.append("• مفيش أي مجلد smali جوه المشروع (الكود المفكوك مش موجود أو اتمسح بالكامل).")
    else:
        empty_dirs = [os.path.basename(d) for d in smali_dirs if not any(os.scandir(d))]
        if empty_dirs:
            problems.append(f"• المجلدات دي فاضية بالكامل: {', '.join(empty_dirs)}.")

    return problems


def project_integrity_report_text(problems):
    return (
        "🚫 المشروع فيه مشاكل واضحة قبل ما نحاول نجمّعه:\n\n"
        + "\n".join(problems)
        + "\n\n💡 لو انت عدّلت أو مسحت حاجة يدوي جوه مجلد المشروع، رجّعها زي ما كانت.\n"
          "أو ابعت ملف الـ APK الأصلي تاني وخليه يتفك من جديد."
    )


async def start_build_flow(query, uid):
    if not os.path.isdir(PROJECT_DIR):
        await query.edit_message_text("❌ لا يوجد مشروع مفكوك حالياً. ابعت ملف APK أولاً.", reply_markup=main_menu_kb())
        return
    problems = check_project_integrity(PROJECT_DIR)
    if problems:
        await query.edit_message_text(project_integrity_report_text(problems), reply_markup=main_menu_kb())
        return
    ok_jar, jar_msg = verify_jar_file(UBER_SIGNER_JAR)
    if not ok_jar:
        await query.edit_message_text(jar_msg, reply_markup=main_menu_kb())
        return
    kb = [
        [InlineKeyboardButton("🎲 توقيع عشوائي", callback_data="sign_random")],
        [InlineKeyboardButton("✍️ توقيع يدوي",   callback_data="sign_manual")],
        [InlineKeyboardButton("⬅️ رجوع",         callback_data="back_main")],
    ]
    await query.edit_message_text("اختار طريقة التوقيع:", reply_markup=InlineKeyboardMarkup(kb))


async def show_keystores_for_build(query):
    kslist = CFG.get("keystores", [])
    rows   = [[InlineKeyboardButton(f"🔑 {k['name']}", callback_data=f"ksbuild:{k['name']}")] for k in kslist]
    rows.append([InlineKeyboardButton("➕ توقيع يدوي جديد", callback_data="ksbuild_new")])
    rows.append([InlineKeyboardButton("⬅️ رجوع",          callback_data="back_main")])
    text = "اختار توقيع محفوظ:" if kslist else "لا يوجد توقيعات محفوظة، اضغط لإضافة واحد جديد:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


# =============================================================================
# البحث والاستبدال
# =============================================================================
def get_smali_files(project_dir):
    files = []
    for root_dir, _, filenames in os.walk(project_dir):
        for fn in filenames:
            if fn.endswith(".smali"):
                files.append(os.path.join(root_dir, fn))
    return files


def do_search_only_sync(project_dir, search_text):
    files = get_smali_files(project_dir)
    total, files_count = 0, 0
    out_lines = []
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        matches = [(i, l.strip()) for i, l in enumerate(lines, 1) if search_text in l]
        if matches:
            files_count += 1
            rel = os.path.relpath(fpath, project_dir)
            out_lines.append(f"📄 {rel} ({len(matches)} نتيجة)")
            for ln, content in matches:
                out_lines.append(f"   سطر {ln}: {content}")
            total += len(matches)
    if total == 0:
        return "❌ النص غير موجود في أي ملف smali."
    return f"✅ النص موجود: {total} نتيجة في {files_count} ملف.\n\n" + "\n".join(out_lines)


def find_matching_smali_files(project_dir, search_text):
    """يرجّع قائمة (المسار النسبي, عدد مرات التكرار) لكل ملف smali فيه النص،
    مرتبة تنازليًا حسب عدد مرات التكرار. تُستخدم عشان نسمح للمستخدم يحدد
    ملف بعينه بدل ما التعديل يطبق على كل الملفات المطابقة دفعة واحدة."""
    files = get_smali_files(project_dir)
    results = []
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        count = content.count(search_text)
        if count:
            results.append((os.path.relpath(fpath, project_dir), count))
    results.sort(key=lambda x: -x[1])
    return results


def replace_file_sync(project_dir, target_rel, new_file_path):
    """يستبدل محتوى ملف موجود داخل المشروع بملف جديد، مع الاحتفاظ
    بنسخة احتياطية من الملف القديم قبل الاستبدال."""
    target_path = os.path.join(project_dir, target_rel)
    if not os.path.isfile(target_path):
        return f"❌ الملف مش موجود:\n{target_rel}"
    backup_dir  = os.path.join(project_dir, "_backup_before_filereplace")
    backup_path = os.path.join(backup_dir, target_rel)
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    try:
        shutil.copy2(target_path, backup_path)
    except Exception:
        pass
    shutil.copy2(new_file_path, target_path)
    return f"✅ تم استبدال الملف بنجاح:\n{target_rel}\n💾 نسخة احتياطية من القديم في:\n{backup_dir}"


def do_delete_sync(project_dir, search_text, target_rel=None):
    files = [os.path.join(project_dir, target_rel)] if target_rel else get_smali_files(project_dir)
    total_removed, files_changed = 0, 0
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        new_lines = [l for l in lines if search_text not in l]
        removed   = len(lines) - len(new_lines)
        if removed > 0:
            with open(fpath, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            total_removed += removed
            files_changed += 1
    return f"✅ تم حذف {total_removed} سطر من {files_changed} ملف."


def do_replace_sync(project_dir, search_text, replace_text, target_rel=None):
    files = [os.path.join(project_dir, target_rel)] if target_rel else get_smali_files(project_dir)
    total, files_changed = 0, 0
    backup_dir = os.path.join(project_dir, "_backup_before_replace")
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        count = content.count(search_text)
        if count == 0:
            continue
        rel         = os.path.relpath(fpath, project_dir)
        backup_path = os.path.join(backup_dir, rel)
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content.replace(search_text, replace_text))
        total        += count
        files_changed += 1
    return f"✅ تم تنفيذ {total} استبدال في {files_changed} ملف.\n💾 نسخة احتياطية في:\n{backup_dir}"


def do_insert_after_sync(project_dir, search_text, insert_text, target_rel=None):
    files = [os.path.join(project_dir, target_rel)] if target_rel else get_smali_files(project_dir)
    total, files_changed = 0, 0
    backup_dir = os.path.join(project_dir, "_backup_before_insert")
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        new_lines, added = [], 0
        for line in lines:
            new_lines.append(line)
            if search_text in line:
                indent = len(line) - len(line.lstrip())
                new_lines.append(" " * indent + insert_text + "\n")
                added += 1
        if added:
            rel         = os.path.relpath(fpath, project_dir)
            backup_path = os.path.join(backup_dir, rel)
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            try:
                with open(backup_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except Exception:
                pass
            with open(fpath, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            total        += added
            files_changed += 1
    return f"✅ تم إضافة {total} سطر في {files_changed} ملف.\n💾 نسخة احتياطية في:\n{backup_dir}"


async def start_search_flow(query, st):
    if not os.path.isdir(PROJECT_DIR):
        await query.edit_message_text("❌ لا يوجد مشروع مفكوك حالياً.", reply_markup=main_menu_kb())
        return
    st["await"] = "search_keyword"
    await query.edit_message_text("🔍 اكتب الكلمة/النص اللي عايز تبحث بيه في ملفات smali:")


async def handle_search_action(query, context, st, action):
    if not os.path.isdir(PROJECT_DIR):
        await query.edit_message_text("❌ لا يوجد مشروع حالياً.", reply_markup=main_menu_kb())
        return
    search_text = st.get("search_text")
    if not search_text:
        await query.edit_message_text("❌ حصل خطأ، جرب تاني من القائمة.", reply_markup=main_menu_kb())
        return

    if action == "search_only":
        await query.edit_message_text("⏳ جاري البحث...")
        result = await asyncio.to_thread(do_search_only_sync, PROJECT_DIR, search_text)
        await send_long_text(context, query.message.chat_id, result)
        await context.bot.send_message(query.message.chat_id, "📋 القائمة الرئيسية:", reply_markup=main_menu_kb())
        return

    # للحذف/الاستبدال/الإضافة: لو النص ده ظاهر في أكتر من ملف smali، نوقف
    # ونسأل المستخدم يحدد ملف بعينه بدل ما التعديل يطبق على كل الملفات
    # المطابقة دفعة واحدة (ده اللي كان بيحصل قبل كده).
    st.pop("search_target_file", None)
    await query.edit_message_text("⏳ جاري تحديد الملفات المطابقة...")
    matches = await asyncio.to_thread(find_matching_smali_files, PROJECT_DIR, search_text)
    if not matches:
        await query.edit_message_text("❌ النص غير موجود في أي ملف smali.", reply_markup=main_menu_kb())
        return

    if len(matches) == 1:
        st["search_target_file"] = matches[0][0]
        await proceed_search_action(query, st, action)
        return

    st["file_matches"] = matches
    rows = []
    for idx, (rel, count) in enumerate(matches[:30]):
        rows.append([InlineKeyboardButton(f"📄 {rel} ({count})", callback_data=f"fmpick:{action}:{idx}")])
    rows.append([InlineKeyboardButton("📁 كل الملفات المطابقة", callback_data=f"fmpick:{action}:all")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="back_main")])
    extra = "" if len(matches) <= 30 else f"\n(بيتم عرض أول 30 من {len(matches)} ملف)"
    await query.edit_message_text(
        f"🔎 النص:\n\"{search_text}\"\nموجود في {len(matches)} ملف.{extra}\n\n"
        "اختار الملف اللي عايز تشتغل عليه، أو اختار كل الملفات:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def proceed_search_action(query, st, action):
    """يكمّل خطوة الحذف/الاستبدال/الإضافة بعد ما اتحدد ملف معيّن (أو كل الملفات)."""
    search_text = st.get("search_text", "")
    target      = st.get("search_target_file")
    target_note = f"\n📄 في الملف: {target}" if target else "\n📁 في كل الملفات المطابقة"

    if action == "search_delete":
        kb = [[
            InlineKeyboardButton("✅ تأكيد الحذف", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ إلغاء",       callback_data="back_main"),
        ]]
        await query.edit_message_text(
            f"⚠️ هيتم حذف كل سطر فيه:\n\"{search_text}\"{target_note}\nتأكيد؟",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if action == "search_replace":
        st["await"] = "search_replace_with"
        await query.edit_message_text(
            f"النص المطلوب استبداله:\n\"{search_text}\"{target_note}\n\n✍️ اكتب النص البديل:"
        )
        return

    if action == "search_insert":
        st["await"] = "search_insert_with"
        await query.edit_message_text(
            f"عند كل سطر فيه:\n\"{search_text}\"{target_note}\n\n✍️ اكتب النص اللي عايز تضيفه تحته:"
        )
        return


# =============================================================================
# استبدال ملف/ملفات بمطابقة الاسم بالظبط (تلقائي، من غير كتابة اسم)
# ─────────────────────────────────────────────────────────────────────────
# المستخدم بيبعت ملف واحد أو ملف ZIP فيه أكتر من ملف، والبوت بيدور تلقائي
# جوه المشروع على أي ملف بنفس الاسم بالظبط (case-sensitive) ويستبدله. أي
# ملف من اللي اتبعت ومالوش نظير بنفس الاسم في المشروع بيتخطى وبيتقال
# للمستخدم في الآخر مع لستة اللي اتبدلوا فعلاً.
# =============================================================================
async def start_search_filename_flow(query, st):
    if not os.path.isdir(PROJECT_DIR):
        await query.edit_message_text("❌ لا يوجد مشروع مفكوك حالياً.", reply_markup=main_menu_kb())
        return
    st["await"] = "filename_replace_upload"
    await query.edit_message_text(
        "📄 ابعت الملف اللي عايز تستبدله دلوقتي، أو ابعت ملف ZIP فيه أكتر من ملف دفعة واحدة.\n\n"
        "هدور تلقائي جوه المشروع على أي ملف بنفس الاسم بالظبط (مهما كان مكانه)، "
        "وأستبدله. أي ملف اسمه مش موجود في المشروع هتخطاه وأقولك عليه في الآخر."
    )


def replace_files_by_name_sync(project_dir, incoming_paths: dict):
    """incoming_paths: dict {اسم الملف: مسار الملف على الديسك}. بيدور جوه
    المشروع على أي ملف بنفس الاسم (المطابقة هنا case-insensitive، يعني لو
    بعت ملف اسمه "pI" هيلاقي ويستبدل ملف اسمه "pi" في المشروع من غير ما
    يشتكي)، ولو لقاه يستبدله محتفظًا باسم الملف الأصلي جوه المشروع زي ما هو
    بالظبط (الكابيتال/السمول بتاعت الملف اللي في المشروع مبتتغيرش، بس
    المحتوى بس اللي بيتستبدل) — مع نسخة احتياطية من القديم. لو مالقاش
    يتخطاه. بيرجّع (replaced, skipped)."""
    name_to_paths = {}
    for root_dir, _, filenames in os.walk(project_dir):
        for fn in filenames:
            name_to_paths.setdefault(fn.lower(), []).append(
                os.path.relpath(os.path.join(root_dir, fn), project_dir)
            )

    backup_dir = os.path.join(project_dir, "_backup_before_filereplace")
    replaced = []  # [(filename, [rel_paths])]
    skipped  = []  # [filename]

    for fname, local_path in incoming_paths.items():
        rel_list = name_to_paths.get(fname.lower())
        if not rel_list:
            skipped.append(fname)
            continue
        for rel in rel_list:
            target_path = os.path.join(project_dir, rel)
            backup_path = os.path.join(backup_dir, rel)
            try:
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                shutil.copy2(target_path, backup_path)
            except Exception:
                pass
            try:
                shutil.copy2(local_path, target_path)
            except Exception:
                pass
        replaced.append((fname, rel_list))

    return replaced, skipped


def find_filename_matches_sync(project_dir, incoming_paths: dict):
    """بحث فقط (من غير أي استبدال أو تعديل فعلي) — بيدور جوه المشروع على
    كل اسم ملف من incoming_paths (مطابقة case-insensitive) وبيرجّع
    dict {اسم الملف: [كل الأماكن (مسارات نسبية) اللي لقاها بنفس الاسم]}.
    لو الاسم مش موجود خالص، القيمة بتكون [] (لستة فاضية)."""
    name_to_paths = {}
    for root_dir, _, filenames in os.walk(project_dir):
        for fn in filenames:
            name_to_paths.setdefault(fn.lower(), []).append(
                os.path.relpath(os.path.join(root_dir, fn), project_dir)
            )
    return {fname: name_to_paths.get(fname.lower(), []) for fname in incoming_paths}


def build_fnreplace_kb(rel_paths):
    """أزرار مرقّمة لكل مكان لقينا فيه الاسم، عشان المستخدم يحدد المكان
    المطلوب بالظبط بضغطة واحدة (1، 2، 3، ...)، بدون ما نستبدل أي حاجة
    تانية غير المكان ده."""
    rows = []
    for i, rel in enumerate(rel_paths):
        label = f"{i + 1}️⃣ {rel}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"fnpick:{i}")])
    rows.append([InlineKeyboardButton("⏭ تخطي هذا الملف (بدون استبدال)", callback_data="fnpick:skip")])
    return InlineKeyboardMarkup(rows)


def format_fnreplace_prompt(fname, rel_paths):
    lines = [f"📄 الملف \"{fname}\" موجود في {len(rel_paths)} مكان مختلف داخل المشروع:\n"]
    for i, rel in enumerate(rel_paths):
        lines.append(f"{i + 1}️⃣ {rel}")
    lines.append("\n👇 اختار رقم المكان اللي عايز تستبدل الملف فيه بالظبط — هيتم الاستبدال في هذا المكان فقط، ومفيش أي تعديل تاني.")
    return "\n".join(lines)


# =============================================================================
# استبدال ملف بمسار محدد (يدوي) — بديل عن البحث بالاسم لما يكون فيه كتير
# ─────────────────────────────────────────────────────────────────────────
# ملفات بنفس الاسم في المشروع. المستخدم بيكتب مسار أو جزء منه (مثلاً
# "bykvm_short06/b")، البوت بيدور جوه المشروع على أي مسار نسبي يحتوي على
# النص ده، ولو النتايج قليلة يسيبه يحدد المكان بالظبط، وبعدين يبعت الملف
# فيتم وضعه في هذا المكان بالضبط. الفرق عن "استبدال بالاسم" إن البحث هنا
# بيتم بالمسار (الفولدر + الاسم) مش بالاسم لوحده، فبيقلل عدد النتائج
# جدًا ويمنع مشكلة كتر الأزرار اللي كانت بتسبب فشل/انقطاع من تليجرام.
# =============================================================================
PATH_REPLACE_MAX_RESULTS = 25  # سقف عدد الأزرار المسموح عرضها دفعة واحدة


def find_path_matches_sync(project_dir, query_text):
    """يرجّع لستة كل المسارات النسبية داخل المشروع اللي بتحتوي على
    query_text (مطابقة جزئية، case-insensitive)، بترتيب أبجدي."""
    q = query_text.strip().lower().replace("\\", "/")
    results = []
    for root_dir, _, filenames in os.walk(project_dir):
        for fn in filenames:
            full = os.path.join(root_dir, fn)
            rel = os.path.relpath(full, project_dir).replace("\\", "/")
            if q in rel.lower():
                results.append(rel)
    results.sort()
    return results


def build_pathpick_kb(rel_paths):
    rows = []
    for i, rel in enumerate(rel_paths):
        label = f"{i + 1}️⃣ {rel}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"pathpick:{i}")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


async def start_path_replace_flow(query, st):
    if not os.path.isdir(PROJECT_DIR):
        await query.edit_message_text("❌ لا يوجد مشروع مفكوك حالياً.", reply_markup=main_menu_kb())
        return
    st["await"] = "path_replace_query"
    await query.edit_message_text(
        "📍 اكتب المسار (أو جزء منه) بتاع الملف اللي عايز تستبدله جوه المشروع.\n\n"
        "مثال: bykvm_short06/b\n\n"
        "هدور على كل مسار فيه النص ده، ولو المسار محدد بالظبط ومفيهوش لبس هبدأ "
        "أطلب منك الملف على طول."
    )


async def handle_path_replace_query(update, st, text):
    """بعد ما المستخدم يكتب المسار/جزء منه، بندور جوه المشروع ونحدد الخطوة
    اللي بعدها حسب عدد النتائج."""
    if not os.path.isdir(PROJECT_DIR):
        st.pop("await", None)
        await update.message.reply_text("❌ لا يوجد مشروع مفكوك حالياً.", reply_markup=main_menu_kb())
        return

    matches = await asyncio.to_thread(find_path_matches_sync, PROJECT_DIR, text)

    if not matches:
        await update.message.reply_text(
            "❌ مفيش أي ملف بمسار فيه النص ده. جرب تاكتب مسار تاني أو جزء أدق منه:"
        )
        return  # سيبه على awaiting == path_replace_query عشان يجرب تاني

    if len(matches) == 1:
        st["path_replace_target"] = matches[0]
        st["await"] = "path_replace_upload"
        await update.message.reply_text(
            f"✅ لقيت مسار واحد بالظبط:\n📄 {matches[0]}\n\n"
            "دلوقتي ابعت الملف اللي عايز تستبدله بيه:"
        )
        return

    if len(matches) > PATH_REPLACE_MAX_RESULTS:
        await update.message.reply_text(
            f"⚠️ النص ده موجود في {len(matches)} مكان، ده كتير عشان أعرضهولك في أزرار.\n"
            "اكتب مسار أكثر تحديدًا (مثلاً ضيف اسم الفولدر كامل):"
        )
        return  # سيبه على awaiting == path_replace_query عشان يضيّق البحث

    st.pop("await", None)
    st["path_replace_candidates"] = matches
    lines = [f"🔎 النص ده موجود في {len(matches)} مكان:\n"]
    for i, rel in enumerate(matches):
        lines.append(f"{i + 1}️⃣ {rel}")
    lines.append("\n👇 اختار المكان بالظبط:")
    await update.message.reply_text("\n".join(lines), reply_markup=build_pathpick_kb(matches))


def finalize_fnreplace_report(st):
    """يبني نص التقرير النهائي بعد ما يخلص كل الملفات (المباشرة تلقائيًا +
    اللي اتحددت يدويًا)، وينظف مجلد التحميل المؤقت وحالة الطابور."""
    report = st.pop("fnreplace_report", {"replaced": [], "skipped": []})
    tmp_dir = st.pop("fnreplace_tmp_dir", None)
    st.pop("fnreplace_queue", None)
    st.pop("fnreplace_current", None)
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    lines = []
    replaced = report.get("replaced", [])
    skipped  = report.get("skipped", [])
    if replaced:
        lines.append(f"✅ اتبدل ({len(replaced)}):")
        for fname, rel in replaced:
            lines.append(f"• {fname} ← {rel}")
    if skipped:
        lines.append(f"\n⏭ اتخطى ({len(skipped)}):")
        for fname, reason in skipped:
            lines.append(f"• {fname} ({reason})")
    if not lines:
        lines.append("⚠️ الملف ده مكانش فيه أي حاجة قدرت أستبدلها.")
    return tail_log("\n".join(lines), 3500)


# =============================================================================
# نشر تطبيق على الموقع (نفس حقول فورم "إضافة تطبيق" في صفحة الأدمن بالظبط)
# =============================================================================
# كل خطوة عبارة عن: key (اسم الحقل في Firestore) - prompt (السؤال) -
# type (طريقة الإدخال) - required (إجباري ولا لأ). الأنواع المتاحة:
#   text           → نص عادي (ابعت "-" للتخطي لو مش إجباري)
#   float / int    → رقم
#   choice         → أزرار اختيار من قايمة
#   bool           → أزرار نعم/لا
#   multiline_list → كل سطر عنصر في قايمة (لقطات الشاشة)
#   list_loop      → رسالة لكل عنصر، وابعت "تم" لما تخلص (شروط التشغيل)
#   json           → نص بصيغة JSON (تقييمات المستخدمين)
PUBLISH_FIELDS = [
    {"key": "name",       "short": "✍️ الاسم",     "prompt": "✍️ اسم التطبيق:",                              "type": "text",  "required": True},
    {"key": "developer",  "short": "✍️ المطور",    "prompt": "✍️ اسم المطور:",                                "type": "text",  "required": True},
    {"key": "packageId",  "short": "📦 Package ID", "prompt": "📦 Package ID (مثال: com.whatsapp):",           "type": "text",  "required": False},
    {"key": "category",   "short": "📂 التصنيف",   "prompt": "📂 التصنيف:", "type": "choice", "required": True, "choices": [
        ("games", "🎮 ألعاب"), ("social", "💬 تواصل اجتماعي"), ("tools", "🔧 أدوات"),
        ("education", "📚 تعليم"), ("health", "💪 صحة ولياقة"), ("finance", "💰 مالية"),
    ]},
    {"key": "icon",        "short": "🖼 الأيقونة",  "prompt": "🖼 رابط الأيقونة (URL):",                       "type": "text",  "required": False},
    {"key": "directUrl",   "short": "🔗 رابط التنزيل", "prompt": "🔗 رابط التنزيل المباشر:",                  "type": "text",  "required": True},
    {"key": "rating",      "short": "⭐ التقييم",   "prompt": "⭐ التقييم (من 1 لـ 5، مثال: 4.3):",           "type": "float", "required": False},
    {"key": "ratingCount", "short": "🔢 عدد التقييمات", "prompt": "🔢 عدد التقييمات (مثال: 5000000):",        "type": "int",   "required": False},
    {"key": "size",        "short": "📦 الحجم",     "prompt": "📦 حجم التطبيق (مثال: 55 MB):",                "type": "text",  "required": False},
    {"key": "installs",    "short": "📥 التثبيتات", "prompt": "📥 عدد التثبيتات (مثال: 1B+):",                "type": "text",  "required": False},
    {"key": "description", "short": "📝 الوصف",     "prompt": "📝 وصف التطبيق:",                              "type": "text",  "required": False},
    {"key": "headerImage", "short": "🖼 صورة الترويسة", "prompt": "🖼 رابط صورة الترويسة (Header Image):",    "type": "text",  "required": False},
    {"key": "screenshots", "short": "📸 لقطات الشاشة", "prompt": "📸 روابط لقطات الشاشة — كل رابط في سطر (حتى 8 روابط):", "type": "multiline_list", "required": False},
    {"key": "version",        "short": "🔢 الإصدار",       "prompt": "🔢 رقم الإصدار (مثال: 2.24.1):",            "type": "text", "required": False},
    {"key": "androidVersion", "short": "🤖 إصدار أندرويد", "prompt": "🤖 أقل إصدار أندرويد مطلوب (مثال: 5.0):",   "type": "text", "required": False},
    {"key": "contentRating", "short": "🔞 التصنيف العمري", "prompt": "🔞 التصنيف العمري:", "type": "choice", "required": False, "choices": [
        ("", "بدون"), ("Everyone", "Everyone (للجميع)"), ("Everyone 10+", "Everyone 10+"),
        ("Teen", "Teen (13+)"), ("Mature 17+", "Mature 17+"),
    ]},
    {"key": "releaseDate",     "short": "📅 تاريخ الإصدار", "prompt": "📅 تاريخ الإصدار الأصلي (YYYY-MM-DD):",     "type": "text", "required": False},
    {"key": "adSupported",     "short": "📢 إعلانات؟",      "prompt": "📢 يحتوي إعلانات؟",                         "type": "bool", "required": False},
    {"key": "inAppPurchases",  "short": "💳 مشتريات داخلية؟", "prompt": "💳 فيه مشتريات داخلية؟",                  "type": "bool", "required": False},
    {"key": "conditions", "short": "⚠️ شروط التشغيل", "prompt": "⚠️ شروط التشغيل (تظهر للمستخدم قبل التنزيل):",   "type": "list_loop", "required": False},
    {"key": "topReviews", "short": "💬 تقييمات المستخدمين", "prompt": "💬 تقييمات المستخدمين بصيغة JSON، مثال:\n[{\"author\":\"أحمد\",\"rating\":5,\"text\":\"تطبيق رائع!\",\"date\":\"2024-03\"}]", "type": "json", "required": False},
    {"key": "badge", "short": "🏷 الشارة", "prompt": "🏷 الشارة:", "type": "choice", "required": False, "choices": [
        ("", "بدون شارة"), ("new", "🆕 جديد"), ("hot", "🔥 رائج"),
    ]},
    {"key": "status", "short": "📊 الحالة", "prompt": "📊 حالة التطبيق:", "type": "choice", "required": False, "choices": [
        ("active", "✅ نشط"), ("hidden", "🔒 مخفي"),
    ]},
    {"key": "featured",       "short": "⭐ مميز؟",         "prompt": "⭐ يظهر كتطبيق مميز؟",                       "type": "bool", "required": False},
    {"key": "seoTitle",       "short": "🔍 عنوان SEO",     "prompt": "🔍 عنوان الصفحة في جوجل (SEO Title):",       "type": "text", "required": False},
    {"key": "seoDescription", "short": "🔍 وصف SEO",       "prompt": "🔍 وصف الصفحة في جوجل (Meta Description، 120-160 حرف مثالي):", "type": "text", "required": False},
    {"key": "seoSlug",        "short": "🔗 SEO Slug",      "prompt": "🔗 رابط الصفحة (SEO Slug)، مثال: whatsapp-download:", "type": "text", "required": False},
]


def _publish_default_icon(cat):
    m = {"games": "🎮", "social": "💬", "tools": "🔧", "education": "📚", "health": "💪", "finance": "💰"}
    return m.get(cat, "📱")


def _publish_slugify(text_):
    text_ = (text_ or "").strip().lower()
    text_ = re.sub(r"\s+", "-", text_)
    text_ = re.sub(r"[^a-z0-9\-]", "", text_)
    return text_


# =============================================================================
# تعبئة بيانات التطبيق أوتوماتيك من رابط صفحة تطبيق (زي Uptodown وشبهها)
# ─────────────────────────────────────────────────────────────────────────
# استخراج "بأفضل جهد ممكن" (best-effort): بنعتمد على meta tags (بتبقى شبه
# ثابتة على أي صفحة تطبيق) لأشياء زي الاسم/الوصف/الأيقونة/المطور، وبنحاول
# regex إضافي للحقول الأصعب (تقييم/حجم/تصنيف...). أي حقل مانقدرش نستخرجه
# ببساطة بنسيبه فاضي، وهيتسأل عادي زي أي حقل يدوي في الخطوات اللي بعد كده.
# =============================================================================
import html as _html_lib

try:
    from google_play_scraper import app as _gplay_app_fetch
    _GPLAY_AVAILABLE = True
except ImportError:
    _gplay_app_fetch = None
    _GPLAY_AVAILABLE = False

_AR_MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "مايو": 5,
    "يونيو": 6, "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9,
    "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_PUBLISH_CATEGORY_KEYWORDS = {
    "games": "games", "game": "games", "العاب": "games", "ألعاب": "games",
    "social": "social", "communication": "social", "تواصل اجتماعي": "social", "التواصل": "social",
    "education": "education", "تعليم": "education",
    "health": "health", "fitness": "health", "lifestyle": "health", "صحة": "health", "لياقة": "health",
    "finance": "finance", "مالية": "finance", "finances": "finance",
    "tools": "tools", "productivity": "tools", "الأدوات": "tools", "أدوات": "tools",
}


def _meta_tag(html_text: str, prop: str) -> str:
    pat1 = rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']'
    pat2 = rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(prop)}["\']'
    m = re.search(pat1, html_text, re.I) or re.search(pat2, html_text, re.I)
    return _html_lib.unescape(m.group(1)).strip() if m else ""


def _label_link_value(html_text: str, *labels: str) -> str:
    """يدور على أقرب <a>...</a> بعد أي label من اللي جايين، زي صف جدول 'المطور: <a>اسم</a>'."""
    for label in labels:
        m = re.search(rf'{re.escape(label)}[^<]{{0,40}}<a[^>]*>([^<]{{1,120}}?)</a>', html_text, re.I)
        if m:
            return _html_lib.unescape(m.group(1)).strip()
    return ""


def _label_text_value(plain_text: str, pattern: str) -> str:
    m = re.search(pattern, plain_text, re.I)
    return _html_lib.unescape(m.group(1)).strip() if m else ""


def _parse_arabic_or_english_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = re.match(r"(\d{1,2})\s+([^\d,]+?)\s+(\d{4})", raw)
    if not m:
        return ""
    day, month_word, year = m.group(1), m.group(2).strip().lower(), m.group(3)
    month_num = _AR_MONTHS.get(month_word) or _EN_MONTHS.get(month_word[:3])
    if not month_num:
        return ""
    try:
        return f"{int(year):04d}-{month_num:02d}-{int(day):02d}"
    except Exception:
        return ""


def _guess_category(raw_category_text: str) -> str:
    low = (raw_category_text or "").strip().lower()
    for kw, slug in _PUBLISH_CATEGORY_KEYWORDS.items():
        if kw in low:
            return slug
    return ""


def _boost_play_image_quality(img_url: str) -> str:
    """
    صور Google Play (أيقونة/ترويسة/لقطات شاشة) بتتخزن على play-lh.googleusercontent.com
    وبييجي في آخر الرابط جزء بيحدد الحجم زي "=w526-h296-rw" وده بيرجع نسخة
    مضغوطة صغيرة. لو شلنا الجزء ده وحطينا بدل منه "=s0" جوجل بترجع الصورة
    بأعلى دقة متاحة (الحجم الأصلي)، وده بيشتغل مع أي صورة على googleusercontent.com.
    """
    if not img_url:
        return img_url
    base = img_url.split("=")[0]
    return f"{base}=s0"


def scrape_app_info_from_google_play(url: str) -> dict:
    """
    يجيب بيانات تطبيق مباشرة من Google Play Store (مش سكرابينج HTML يدوي —
    بنستخدم مكتبة google-play-scraper اللي بتتعامل مع الـ API الداخلي بتاع
    المتجر، فالبيانات بتطلع دقيقة ومنظمة، ولقطات الشاشة/الأيقونة بترجع
    بأعلى جودة ممكنة عن طريق _boost_play_image_quality).
    """
    out = {}

    m = re.search(r"[?&]id=([a-zA-Z0-9_.]+)", url)
    if not m:
        return {"_error": (
            "❌ الرابط ده مش رابط تطبيق سليم من Google Play.\n"
            "لازم يكون بالشكل ده ويحتوي على ?id=...، مثال:\n"
            "https://play.google.com/store/apps/details?id=com.whatsapp"
        )}
    app_id = m.group(1)

    lang_m = re.search(r"[?&]hl=([a-zA-Z\-]+)", url)
    lang = lang_m.group(1) if lang_m else "ar"
    gl_m = re.search(r"[?&]gl=([a-zA-Z]+)", url)
    country = gl_m.group(1) if gl_m else "eg"

    if not _GPLAY_AVAILABLE:
        return {"_error": (
            "❌ مكتبة google-play-scraper مش متثبتة على السيرفر.\n"
            "ثبّتها بالأمر:\npip install google-play-scraper\n"
            "وبعدين شغّل البوت تاني."
        )}

    try:
        data = _gplay_app_fetch(app_id, lang=lang, country=country)
    except Exception as e:
        return {"_error": f"فشل جلب بيانات التطبيق من Google Play:\n{e}"}

    # ── الاسم والمطور ──
    if data.get("title"):
        out["name"] = data["title"].strip()
    if data.get("developer"):
        out["developer"] = data["developer"].strip()

    # ── Package ID ──
    out["packageId"] = data.get("appId") or app_id

    # ── الأيقونة وصورة الترويسة (بأعلى جودة) ──
    if data.get("icon"):
        out["icon"] = _boost_play_image_quality(data["icon"])
    if data.get("headerImage"):
        out["headerImage"] = _boost_play_image_quality(data["headerImage"])

    # ── لقطات الشاشة (بأعلى جودة، لحد 8) ──
    shots = data.get("screenshots") or []
    if shots:
        out["screenshots"] = [_boost_play_image_quality(s) for s in shots[:8]]

    # ── الوصف ──
    if data.get("description"):
        out["description"] = data["description"].strip()

    # ── التصنيف ──
    mapped_cat = _guess_category(data.get("genre", ""))
    if mapped_cat:
        out["category"] = mapped_cat

    # ── التقييم وعدد التقييمات ──
    if data.get("score") is not None:
        try:
            out["rating"] = round(float(data["score"]), 1)
        except (TypeError, ValueError):
            pass
    if data.get("ratings") is not None:
        try:
            out["ratingCount"] = int(data["ratings"])
        except (TypeError, ValueError):
            pass

    # ── عدد التنزيلات ──
    if data.get("installs"):
        out["installs"] = data["installs"].strip()

    # ── الحجم ──
    if data.get("size"):
        out["size"] = data["size"].strip()

    # ── رقم الإصدار ──
    if data.get("version") and data["version"].lower() != "varies with device":
        out["version"] = data["version"].strip()

    # ── أقل إصدار أندرويد ──
    andv = data.get("androidVersion") or ""
    andv_m = re.search(r"(\d+(?:\.\d+)?)", andv)
    if andv_m:
        out["androidVersion"] = andv_m.group(1)

    # ── التصنيف العمري ──
    if data.get("contentRating"):
        out["contentRating"] = data["contentRating"].strip()

    # ── تاريخ الإصدار الأصلي ──
    release_date = _parse_arabic_or_english_date(data.get("released") or "")
    if release_date:
        out["releaseDate"] = release_date

    # ── إعلانات ──
    if data.get("adSupported") is not None:
        out["adSupported"] = bool(data["adSupported"])

    return out


def scrape_app_info_sync(url: str) -> dict:
    """
    يجيب بيانات تطبيق من رابط صفحته. لو الرابط من Google Play Store بيستخدم
    google-play-scraper (أدق وأعلى جودة صور)، ولو من أي موقع تاني (زي
    Uptodown) بيرجع لطريقة meta tags القديمة.
    """
    if "play.google.com" in url.lower():
        return scrape_app_info_from_google_play(url)
    out = {}
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "ar,en;q=0.8",
            },
            timeout=25,
        )
        r.raise_for_status()
    except Exception as e:
        return {"_error": f"فشل تحميل الصفحة:\n{e}"}

    html_text = r.text
    plain_text = re.sub(r"<[^>]+>", " ", html_text)
    plain_text = re.sub(r"\s+", " ", _html_lib.unescape(plain_text)).strip()

    # ── الاسم ──
    title = _meta_tag(html_text, "og:title")
    name = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
    if name:
        out["name"] = name

    # ── الوصف (بناخد الأطول بين og:description و meta description) ──
    desc_og   = _meta_tag(html_text, "og:description")
    desc_meta = _meta_tag(html_text, "description")
    description = desc_meta if len(desc_meta) > len(desc_og) else desc_og
    if description:
        out["description"] = description

    # ── الأيقونة ──
    # ملحوظة: og:image على Uptodown بيرجع الأيقونة نفسها (مربعة)، ومش صورة
    # ترويسة (banner) حقيقية — Uptodown أصلاً مافيهوش صورة ترويسة منفصلة.
    # فبنملى بيها حقل "icon" بس، وبنسيب "headerImage" فاضي عشان تتملى يدوي
    # لو عندك صورة ترويسة فعلية (مش هنحطلها الأيقونة غلط).
    icon = _meta_tag(html_text, "og:image")
    if icon:
        out["icon"] = icon

    # ── لقطات الشاشة ──
    # Uptodown بيستضيف لقطات الشاشة على img.utdstc.com/screen/... (بعكس
    # الأيقونة اللي بتكون على img.utdstc.com/icon/...)، سواء كانت متحطة في
    # src أو data-src أو أي attribute تاني (lazy loading)، فبندور على الرابط
    # نفسه في أي مكان في الصفحة بدل ما نعتمد على اسم الـ attribute.
    raw_shots = re.findall(
        r'https?://[a-zA-Z0-9.\-]*utdstc\.com/screen/[^\s"\'<>)]+',
        html_text,
    )
    screenshots = []
    for shot in raw_shots:
        shot = shot.rstrip(").,;")
        if shot not in screenshots:
            screenshots.append(shot)
    if screenshots:
        out["screenshots"] = screenshots[:8]

    # ── المطور (twitter:data2 غالبًا بيبقى اسم الناشر/المطور على Uptodown) ──
    developer = _meta_tag(html_text, "twitter:data2")
    if not developer:
        developer = _label_link_value(html_text, "المُطوِّر", "المطور", "Developer")
    if developer:
        out["developer"] = developer

    # ── رقم الإصدار (من عنوان الصفحة، بيبقى فيه رقم زي "18.5.0") ──
    version_source = _meta_tag(html_text, "twitter:title") or title
    m = re.search(r"\b(\d+(?:\.\d+){1,3})\b", version_source)
    if m:
        out["version"] = m.group(1)

    # ── التصنيف (نحاول نطابقه مع تصنيفاتنا المحدودة، لو مافيش تطابق بنسيبه) ──
    category_raw = _label_link_value(html_text, "الفئة", "Category")
    mapped_cat = _guess_category(category_raw)
    if mapped_cat:
        out["category"] = mapped_cat

    # ── التقييم وعدد التقييمات ──
    m = re.search(r"(\d+(?:\.\d+)?)\s+([\d,]{2,})\s*(?:التعليقات|reviews|ratings)", plain_text, re.I)
    if m:
        try:
            out["rating"] = float(m.group(1))
            out["ratingCount"] = int(m.group(2).replace(",", ""))
        except ValueError:
            pass

    # ── عدد التنزيلات (كنص زي "159.1 M") ──
    installs = _label_text_value(plain_text, r"([\d.,]+\s?[KMB]\+?)\s*(?:عدد مرات التنزيل|downloads)")
    if not installs:
        installs = _label_text_value(plain_text, r"(?:عدد مرات التنزيل|downloads)\s*[:\|]?\s*([\d.,]+\s?[KMB]\+?)")
    if installs:
        out["installs"] = installs

    # ── حجم التطبيق ──
    size = _label_text_value(plain_text, r"(?:الحجم|Size)\s*[:\|]?\s*([\d.,]+\s?[KMG]B)")
    if size:
        out["size"] = size

    # ── تاريخ آخر تحديث ──
    date_raw = _label_text_value(plain_text, r"(?:التاريخ|Date)\s*[:\|]?\s*(\d{1,2}\s+[^\d,]{2,15}\s+\d{4})")
    release_date = _parse_arabic_or_english_date(date_raw)
    if release_date:
        out["releaseDate"] = release_date

    # ── Package ID ──
    pkg = _label_text_value(
        plain_text,
        r"(?:اسم حزمة العرض|Package Name|Package ID)\s*[:\|]?\s*([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+){1,8})",
    )
    if pkg:
        out["packageId"] = pkg

    # ── أقل إصدار أندرويد مطلوب (تخمين مبدئي، مش مضمون 100%) ──
    andv = _label_text_value(plain_text, r"Android\s*\+?\s*([\d]+\.[\d]+)")
    if andv:
        out["androidVersion"] = andv

    return out


def _publish_draft_path(chat_id) -> str:
    return os.path.join(PUBLISH_DRAFTS_DIR, f"{chat_id}.json")


def save_publish_draft(chat_id, data: dict) -> None:
    """بتحفظ تفاصيل التطبيق الكاملة (الوصف الطويل، لقطات الشاشة...) في ملف
    JSON على السيرفر، بدل ما تتحط خام جوه رسالة تيليجرام. ده اللي بيمنع
    أي احتمال لتجاوز حد طول الرسالة (Message_too_long) مهما كانت البيانات
    كبيرة، لأن الرسالة بقت بتعرض ملخص قصير بس مش القيم الخام."""
    try:
        with open(_publish_draft_path(chat_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        log.warning(f"⚠️ فشل حفظ مسودة النشر في ملف (chat_id={chat_id})", exc_info=True)


def delete_publish_draft(chat_id) -> None:
    """بتمسح ملف المسودة بعد النشر بنجاح أو الإلغاء، عشان تنضّف كل حاجة."""
    try:
        os.remove(_publish_draft_path(chat_id))
    except Exception:
        pass


def _build_short_autofill_summary(data: dict) -> str:
    """بترجع ملخص قصير جدًا (سطر لكل حقل، مقتطف مش القيمة الخام) للحقول
    اللي اتلقطت أوتوماتيك. البيانات الكاملة (الوصف الطويل، لقطات الشاشة)
    بتتحفظ في ملف على السيرفر مش في الرسالة، فمستحيل الرسالة دي تتجاوز حد
    تليجرام مهما كانت البيانات كبيرة."""
    known = {s["key"]: s for s in PUBLISH_FIELDS}
    lines = []
    for k, v in data.items():
        if v in (None, "", []) or (isinstance(v, str) and not v.strip()):
            continue
        step = known.get(k, {"type": "text"})
        preview = _publish_field_preview(step, v)
        label = known.get(k, {}).get("short", k)
        lines.append(f"• {label}: {preview}")
    return "\n".join(lines) if lines else "—"


async def start_publish_flow(query, context, st, prefill=None):
    st["publish_data"] = dict(prefill or {})
    st.pop("publish_step", None)
    st.pop("publish_list_temp", None)
    st.pop("publish_edit_idx", None)
    st.pop("await", None)
    chat_id = query.message.chat_id
    save_publish_draft(chat_id, st["publish_data"])
    intro = (
        "📤 نشر تطبيق جديد على الموقع\n\n"
        "هتلاقي تحت كل الحقول مرة واحدة كأزرار. دوس على أي حقل عشان تملاه "
        "أو تعدله، ولما تخلص دوس \"📤 نشر الآن\".\n"
        "الحقول اللي عليها ❗ لازم تتملى قبل النشر."
    )
    if prefill:
        intro = (
            "✅ الحقول اللي اتلقطت أوتوماتيك من الرابط (التفاصيل الكاملة "
            "محفوظة في ملف على السيرفر):\n"
            f"{_build_short_autofill_summary(prefill)}\n\n"
            "دوس على أي حقل تحت عشان تعدله أو تملي الباقي، ولما تخلص دوس "
            "\"📤 نشر الآن\"."
        )
    await query.edit_message_text(intro)
    await send_publish_review(context.bot, chat_id, st)


async def start_publish_autofill(context, chat_id, st, url):
    status_msg = await context.bot.send_message(chat_id, "⏳ جاري قراءة صفحة التطبيق واستخراج البيانات...")
    scraped = await asyncio.to_thread(scrape_app_info_sync, url)

    if scraped.get("_error"):
        await status_msg.edit_text(
            f"{scraped['_error']}\n\nهنكمل تعبئة البيانات يدوي بدل كده.",
        )
        st["publish_data"] = {}
        st.pop("publish_step", None)
        st.pop("publish_list_temp", None)
        st.pop("publish_edit_idx", None)
        await send_publish_review(context.bot, chat_id, st)
        return

    st["publish_data"] = {k: v for k, v in scraped.items() if not k.startswith("_")}
    st.pop("publish_step", None)
    st.pop("publish_list_temp", None)
    st.pop("publish_edit_idx", None)
    save_publish_draft(chat_id, st["publish_data"])

    if st["publish_data"]:
        await status_msg.edit_text(
            "✅ اللي اتلقط أوتوماتيك من الرابط (التفاصيل الكاملة محفوظة في "
            "ملف على السيرفر):\n"
            f"{_build_short_autofill_summary(st['publish_data'])}\n\n"
            "دوس على أي حقل تحت عشان تعدله أو تملي الباقي (رابط التنزيل "
            "المباشر لازم تكتبه انت)."
        )
    else:
        await status_msg.edit_text("⚠️ مقدرتش ألاقي حاجة في الصفحة دي، هسألك على كل البيانات يدوي.")

    await send_publish_review(context.bot, chat_id, st)


def _publish_field_preview(step, value):
    stype = step["type"]
    if stype == "bool":
        if value is None or value == "":
            return "—"
        return "✅ نعم" if value else "❌ لا"
    if stype == "choice":
        for cval, disp in step["choices"]:
            if cval == value:
                return disp if cval else "—"
        return "—"
    if stype in ("multiline_list", "list_loop"):
        return f"{len(value)} عنصر" if value else "—"
    if stype == "json":
        return "✅ محفوظ" if value else "—"
    text_ = str(value).strip() if value is not None else ""
    if not text_:
        return "—"
    return text_ if len(text_) <= 28 else text_[:27] + "…"


def build_publish_review_kb(data):
    rows = []
    for idx, step in enumerate(PUBLISH_FIELDS):
        value = data.get(step["key"])
        empty = value in (None, "", [])
        marker = "❗" if step["required"] and empty else ("▫️" if empty else "✅")
        preview = _publish_field_preview(step, value)
        label = f"{marker} {step.get('short', step['key'])}: {preview}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"pubf:{idx}")])
    rows.append([InlineKeyboardButton("📤 نشر الآن", callback_data="pubpublish")])
    rows.append([InlineKeyboardButton("❌ إلغاء النشر", callback_data="pubcancel")])
    return InlineKeyboardMarkup(rows)


async def send_publish_review(bot, chat_id, st):
    data = st.setdefault("publish_data", {})
    st.pop("await", None)
    st.pop("publish_edit_idx", None)
    st.pop("publish_list_temp", None)
    save_publish_draft(chat_id, data)
    await bot.send_message(
        chat_id,
        "📋 بيانات التطبيق دلوقتي — دوس على أي حقل تعدله، وبعدين \"📤 نشر الآن\":",
        reply_markup=build_publish_review_kb(data),
    )


async def edit_publish_review(query, st):
    data = st.setdefault("publish_data", {})
    st.pop("await", None)
    st.pop("publish_edit_idx", None)
    st.pop("publish_list_temp", None)
    save_publish_draft(query.message.chat_id, data)
    await query.edit_message_text(
        "📋 بيانات التطبيق دلوقتي — دوس على أي حقل تعدله، وبعدين \"📤 نشر الآن\":",
        reply_markup=build_publish_review_kb(data),
    )


async def handle_publish_field_tap(query, context, st, idx):
    if idx < 0 or idx >= len(PUBLISH_FIELDS):
        await edit_publish_review(query, st)
        return
    step  = PUBLISH_FIELDS[idx]
    stype = step["type"]
    st["publish_edit_idx"] = idx

    back_row = [InlineKeyboardButton("◀️ رجوع من غير تعديل", callback_data="pubfback")]

    if stype == "choice":
        rows = [[InlineKeyboardButton(disp, callback_data=f"pubfchoice:{idx}:{cidx}")]
                for cidx, (_, disp) in enumerate(step["choices"])]
        rows.append(back_row)
        st.pop("await", None)
        await query.edit_message_text(step["prompt"], reply_markup=InlineKeyboardMarkup(rows))
        return

    if stype == "bool":
        rows = [[
            InlineKeyboardButton("✅ نعم", callback_data=f"pubfbool:{idx}:1"),
            InlineKeyboardButton("❌ لا",  callback_data=f"pubfbool:{idx}:0"),
        ], back_row]
        st.pop("await", None)
        await query.edit_message_text(step["prompt"], reply_markup=InlineKeyboardMarkup(rows))
        return

    if stype == "list_loop":
        st["publish_list_temp"] = list(st["publish_data"].get(step["key"], []) or [])
        st["await"] = "publish_field_edit"
        rows = [[InlineKeyboardButton("🗑 امسح الكل", callback_data=f"pubfclear:{idx}")], back_row]
        await query.edit_message_text(
            f"{step['prompt']}\n(ابعت كل شرط في رسالة لوحده، وابعت \"تم\" لما تخلص)",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    st["await"] = "publish_field_edit"
    rows = [[InlineKeyboardButton("🗑 امسح القيمة", callback_data=f"pubfclear:{idx}")], back_row]
    await query.edit_message_text(step["prompt"], reply_markup=InlineKeyboardMarkup(rows))


async def handle_publish_field_text(update, context, st, text):
    idx = st.get("publish_edit_idx")
    chat_id = update.effective_chat.id
    if idx is None or idx < 0 or idx >= len(PUBLISH_FIELDS):
        st.pop("await", None)
        await send_publish_review(context.bot, chat_id, st)
        return
    step  = PUBLISH_FIELDS[idx]
    stype = step["type"]

    if stype == "list_loop":
        if text.strip() == "تم":
            st["publish_data"][step["key"]] = st.pop("publish_list_temp", [])
            await send_publish_review(context.bot, chat_id, st)
            return
        st.setdefault("publish_list_temp", []).append(text)
        await update.message.reply_text(f"✅ اتضاف ({len(st['publish_list_temp'])}). ابعت شرط تاني أو اكتب \"تم\":")
        return

    if stype == "float":
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ اكتب رقم صحيح (مثال: 4.3):")
            return
    elif stype == "int":
        try:
            value = int(re.sub(r"[^\d]", "", text) or "0")
        except ValueError:
            await update.message.reply_text("❌ اكتب رقم صحيح:")
            return
    elif stype == "multiline_list":
        value = [l.strip() for l in text.split("\n") if l.strip()][:8]
    elif stype == "json":
        try:
            value = json.loads(text)
        except Exception as e:
            await update.message.reply_text(f"❌ صيغة JSON غلط:\n{e}\nحاول تاني:")
            return
    else:
        value = text.strip()

    st["publish_data"][step["key"]] = value
    await send_publish_review(context.bot, chat_id, st)


async def handle_publish_clear_callback(query, context, st, idx):
    step = PUBLISH_FIELDS[idx]
    if step["type"] in ("multiline_list", "list_loop"):
        st["publish_data"][step["key"]] = []
    elif step["type"] == "bool":
        st["publish_data"][step["key"]] = False
    elif step["type"] == "json":
        st["publish_data"].pop(step["key"], None)
    else:
        st["publish_data"][step["key"]] = ""
    await edit_publish_review(query, st)


async def handle_publish_choice_callback(query, context, st, idx, val_raw):
    step = PUBLISH_FIELDS[idx]
    if step["type"] == "bool":
        value = (val_raw == "1")
    else:
        cidx = int(val_raw)
        value = step["choices"][cidx][0]
    st["publish_data"][step["key"]] = value
    await edit_publish_review(query, st)


async def handle_publish_finalize(query, context, st):
    data = st.get("publish_data", {})
    missing = [f.get("short", f["key"]) for f in PUBLISH_FIELDS
               if f["required"] and not str(data.get(f["key"], "")).strip()]
    if missing:
        await query.answer("❗ لسه ناقص: " + "، ".join(missing), show_alert=True)
        return
    await query.edit_message_text("⏳ جاري نشر التطبيق...")
    await finalize_publish(context.bot, query.message.chat_id, st)


async def finalize_publish(bot, chat_id, st):
    d   = st.get("publish_data", {})
    name = d.get("name", "").strip()
    dev  = d.get("developer", "").strip()
    url_ = d.get("directUrl", "").strip()
    cat  = d.get("category", "")

    if not (name and dev and url_ and cat):
        await bot.send_message(chat_id, "❌ حصل خطأ: في حقول إجبارية ناقصة. جرب تاني من القائمة.", reply_markup=main_menu_kb())
        st.pop("publish_data", None)
        st.pop("publish_step", None)
        return

    pkg = d.get("packageId", "").strip()
    seo_slug = _publish_slugify(d.get("seoSlug", ""))
    if not seo_slug:
        seo_slug = pkg.replace(".", "-") if pkg else ""

    # ── نفس فكرة اسم التطبيق بالإنجليزي والعربي في أول رسالة القناة،
    # لكن هنا من غير ما نقصّر باقي الوصف (وصف الموقع بيتعرض كامل) ──────────
    name_en, name_ar = await fetch_bilingual_app_name(pkg)
    if name_en and name_ar:
        bilingual_name = f"{name_en} - {name_ar}"
    else:
        bilingual_name = name_en or name_ar or ""
    full_description = d.get("description", "").strip()
    if bilingual_name and not full_description.startswith(bilingual_name):
        full_description = f"{bilingual_name}\n\n{full_description}" if full_description else bilingual_name

    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    app_data = {
        "name": name,
        "developer": dev,
        "packageId": pkg,
        "icon": d.get("icon", "").strip() or _publish_default_icon(cat),
        "screenshots": d.get("screenshots", []) or [],
        "directUrl": url_,
        "downloadUrl": f"https://play.google.com/store/apps/details?id={pkg}" if pkg else "",
        "category": cat,
        "rating": d.get("rating", 0) or 0,
        "ratingCount": d.get("ratingCount", 0) or 0,
        "size": d.get("size", "").strip(),
        "installs": d.get("installs", "").strip(),
        "description": full_description,
        "conditions": d.get("conditions", []) or [],
        "badge": d.get("badge", ""),
        "status": d.get("status") or "active",
        "featured": bool(d.get("featured", False)),
        "nameSearch": name.lower().split(),
        "version": d.get("version", "").strip(),
        "androidVersion": d.get("androidVersion", "").strip(),
        "contentRating": d.get("contentRating", ""),
        "releaseDate": d.get("releaseDate", "").strip(),
        "adSupported": bool(d.get("adSupported", False)),
        "inAppPurchases": bool(d.get("inAppPurchases", False)),
        "headerImage": d.get("headerImage", "").strip(),
        "seoTitle": d.get("seoTitle", "").strip(),
        "seoDescription": d.get("seoDescription", "").strip(),
        "seoSlug": seo_slug,
        "downloads": 0,
        "addedAt": {"__timestamp__": now_iso},
        "updatedAt": {"__timestamp__": now_iso},
    }
    if d.get("topReviews"):
        app_data["topReviews"] = d["topReviews"]

    owner_uid = CFG.get("firebase_owner_uid", "").strip()
    if owner_uid:
        app_data["ownerUid"] = owner_uid

    # نحوّل علامة الـ timestamp لصيغة Firestore الفعلية (بديل عن serverTimestamp
    # اللي متاح بس في الـ SDK مش في REST create مباشرة)
    def _fix_timestamps(obj):
        if isinstance(obj, dict) and "__timestamp__" in obj:
            return {"timestampValue": obj["__timestamp__"]}
        return None

    await bot.send_message(chat_id, "⏳ جاري حفظ التطبيق على الموقع...")

    fields = {}
    for k, v in app_data.items():
        ts = _fix_timestamps(v) if isinstance(v, dict) else None
        fields[k] = ts if ts else _to_firestore_value(v)

    ok, result = await asyncio.to_thread(_publish_write_sync, fields)

    st.pop("publish_data", None)
    st.pop("publish_step", None)
    st.pop("publish_list_temp", None)
    delete_publish_draft(chat_id)

    if ok:
        await bot.send_message(
            chat_id,
            f"✅ تم نشر التطبيق بنجاح على الموقع!\n📄 اسم: {name}\n🆔 معرّف المستند: `{result}`",
        )
        # نحفظ بيانات التطبيق ده عشان لو الأدمن قرر ينشره في قناة تيليجرام كمان
        st["last_published_app"] = dict(app_data)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 انشر في القناة", callback_data="chpub_yes")],
            [InlineKeyboardButton("❌ لأ، خلاص كفاية", callback_data="chpub_no")],
        ])
        await bot.send_message(
            chat_id,
            "📢 عايز تنشر التطبيق ده في قناة تيليجرام كمان؟",
            reply_markup=kb,
        )
    else:
        await bot.send_message(chat_id, result, reply_markup=main_menu_kb())


def _publish_write_sync(fields: dict) -> tuple[bool, str]:
    """مثل firestore_add_document_sync لكن بياخد fields جاهزة بصيغة Firestore
    (عشان نقدر نستبدل الـ timestamp بشكلها الصحيح قبل الإرسال)."""
    try:
        id_token = _firebase_get_id_token_sync()
    except Exception as e:
        return False, f"❌ فشل تسجيل الدخول لـ Firebase:\n{e}"

    project_id = CFG.get("firebase_project_id", "")
    url = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents/apps"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {id_token}"}, json={"fields": fields}, timeout=30)
        if r.status_code == 401:
            id_token = _firebase_sign_in_sync()
            r = requests.post(url, headers={"Authorization": f"Bearer {id_token}"}, json={"fields": fields}, timeout=30)
    except Exception as e:
        return False, f"❌ خطأ في الاتصال بـ Firestore:\n{e}"

    if not r.ok:
        try:
            err = r.json().get("error", {}).get("message", r.text)
        except Exception:
            err = r.text
        return False, f"❌ فشل الحفظ في Firestore ({r.status_code}):\n{err}"

    doc_name = r.json().get("name", "")
    doc_id   = doc_name.rstrip("/").split("/")[-1] if doc_name else ""
    return True, doc_id


# =============================================================================
# نشر إعلان التطبيق في قناة تيليجرام (بعد ما يتنشر على الموقع)
# =============================================================================
def _CATEGORY_LABELS():
    return {c[0]: c[1] for step in PUBLISH_FIELDS if step["key"] == "category" for c in step["choices"]}


def build_site_app_url(app_data: dict) -> str:
    """يبني رابط صفحة التطبيق على الموقع من packageId (بعد استبدال النقط
    بشرطات)، وإن ملقيهوش يرجع لـ seoSlug كبديل."""
    base = (CFG.get("site_app_page_url") or "").strip().rstrip("?")
    pkg = (app_data.get("packageId") or "").strip()
    slug = pkg.replace(".", "-") if pkg else (app_data.get("seoSlug") or "").strip()
    if not base or not slug:
        return ""
    return f"{base}?app={slug}"


def _short_description(desc: str, limit: int = 90) -> str:
    """بترجع مقتطف صغير جدًا من الوصف (كلمات مفتاحية بس، مش فقرة كاملة)."""
    original = (desc or "").strip()
    if not original:
        return ""
    truncated = original
    was_cut = False
    m = re.search(r"[.!؟]\s", original)
    if m and m.start() <= limit:
        truncated = original[:m.start()]
    elif len(original) > limit:
        truncated = original[:limit].rstrip()
        was_cut = True
    truncated = truncated.strip().rstrip(".،,")
    return truncated + ("…" if was_cut else "")


async def fetch_bilingual_app_name(package_id: str) -> tuple[str, str]:
    """بيجيب اسم التطبيق بالإنجليزي والعربي من Google Play (لو متاح)، عشان
    يتحط في أول رسالة القناة بالشكل: "ChatGPT - شات جي بي تي". لو مقدرش
    يجيب واحد منهم أو كانوا متطابقين، بيرجع الفاضي في المكان المناسب."""
    if not (_GPLAY_AVAILABLE and package_id):
        return "", ""

    def _fetch(lang: str, country: str) -> str:
        try:
            data = _gplay_app_fetch(package_id, lang=lang, country=country)
            return (data.get("title") or "").strip()
        except Exception:
            return ""

    name_en = await asyncio.to_thread(_fetch, "en", "us")
    name_ar = await asyncio.to_thread(_fetch, "ar", "eg")
    if name_en and name_ar and name_en.strip().lower() == name_ar.strip().lower():
        name_ar = ""  # نفس الاسم بالظبط، مفيش داعي نكرره
    return name_en, name_ar


def build_channel_message(app_data: dict, site_url: str, name_en: str = "", name_ar: str = "") -> str:
    name  = app_data.get("name", "").strip()
    dev   = app_data.get("developer", "").strip()
    cat   = _CATEGORY_LABELS().get(app_data.get("category", ""), app_data.get("category", ""))
    desc  = _short_description(app_data.get("description") or "", limit=50)
    rating = app_data.get("rating") or 0
    rcount = app_data.get("ratingCount") or 0
    size   = (app_data.get("size") or "").strip()
    installs = (app_data.get("installs") or "").strip()
    version  = (app_data.get("version") or "").strip()

    lines = [f"📱 <b>{_html_lib.escape(name)}</b>"]
    if dev:
        lines.append(f"👨‍💻 المطور: {_html_lib.escape(dev)}")
    if cat:
        lines.append(f"📂 التصنيف: {_html_lib.escape(str(cat))}")
    extra = []
    if rating:
        extra.append(f"⭐ {rating}" + (f" ({rcount:,})" if rcount else ""))
    if size:
        extra.append(f"📦 {size}")
    if installs:
        extra.append(f"📥 {installs}")
    if version:
        extra.append(f"🔢 v{version}")
    if extra:
        lines.append(" | ".join(extra))

    # ── بلوك الوصف: أول سطر الاسم بالإنجليزي والعربي (كلمة مفتاحية)،
    # وبعده جملة قصيرة جدًا من الوصف الأصلي. ده البلوك الوحيد اللي بنقصره
    # لو الرسالة كلها طلعت أطول من اللازم (تحت). ─────────────────────────
    if name_en and name_ar:
        bilingual_name = f"{name_en} - {name_ar}"
    else:
        bilingual_name = name_en or name_ar or ""

    desc_lines = []
    if bilingual_name:
        desc_lines.append(_html_lib.escape(bilingual_name))
    if desc:
        desc_lines.append(_html_lib.escape(desc))
    desc_block = "\n".join(desc_lines)

    if desc_block:
        lines.append("")
        lines.append(desc_block)
    lines.append("")
    lines.append(f"⬇️ للتحميل: {site_url}")
    text = "\n".join(lines)

    # ── شبكة أمان نهائية: أقصى حد كابشن الصورة في تليجرام 1024 حرف (وده
    # أضيق حد بنستخدمه، لأن الرسالة ممكن تتبعت كابشن تحت صورة). لو الرسالة
    # طلعت أطول من كده لأي سبب (اسم مطور طويل جدًا، تصنيف غريب...)، بنقص
    # بلوك الوصف الأول، وبعدين بنقص أي حاجة زيادة من آخر النص لو لسه طويل.
    # كده مستحيل تحصل "Message_too_long" تاني من الرسالة دي. ──────────────
    SAFE_LIMIT = 1000
    if len(text) > SAFE_LIMIT and desc_block:
        overflow = len(text) - SAFE_LIMIT
        keep = max(0, len(desc_block) - overflow - 1)
        new_desc_block = (desc_block[:keep].rstrip() + "…") if keep > 0 else ""
        text = text.replace(desc_block, new_desc_block, 1)
    if len(text) > SAFE_LIMIT:
        text = text[:SAFE_LIMIT - 1].rstrip() + "…"
    return text


async def post_app_to_channel(bot, app_data: dict, site_url: str = "") -> tuple[bool, str]:
    channel_id = (CFG.get("telegram_channel_id") or "").strip()
    if not channel_id:
        return False, "❌ آيدي قناة التيليجرام مش متظبط في الإعدادات (telegram_channel_id). حدّثها من زرار \"🆔 تحديث آيدي/يوزر القناة\" في القائمة الرئيسية."

    site_url = (site_url or "").strip() or build_site_app_url(app_data)
    if not site_url:
        return False, "❌ مقدرتش أبني رابط صفحة التطبيق على الموقع (تأكد من site_app_page_url و Package ID)."

    name_en, name_ar = await fetch_bilingual_app_name((app_data.get("packageId") or "").strip())
    caption = build_channel_message(app_data, site_url, name_en=name_en, name_ar=name_ar)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ تحميل التطبيق", url=site_url)]])

    photo_url = (app_data.get("headerImage") or "").strip() or (app_data.get("icon") or "").strip()
    if not photo_url.lower().startswith(("http://", "https://")):
        photo_url = ""

    try:
        if photo_url:
            try:
                await bot.send_photo(
                    chat_id=channel_id, photo=photo_url, caption=caption,
                    parse_mode="HTML", reply_markup=kb,
                )
            except Exception:
                # لو الصورة فشلت (رابط باظ مثلًا) نبعت رسالة نصية عادية بدالها
                await bot.send_message(
                    chat_id=channel_id, text=caption,
                    parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False,
                )
        else:
            await bot.send_message(
                chat_id=channel_id, text=caption,
                parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False,
            )
    except Exception as e:
        return False, f"❌ فشل النشر في القناة: {e}"

    return True, "✅ اتنشر في القناة بنجاح!"


# =============================================================================
# تنزيل APK من رابط
# =============================================================================
async def download_apk_from_url(context, chat_id, uid, page_url: str, download_url: str):
    """
    يفتح صفحة التنزيل عشان يجيب الـ session والـ cookies،
    ثم ينزّل الـ APK من الرابط المباشر.
    """
    from urllib.parse import urlparse, unquote

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept"    : "text/html,application/xhtml+xml,*/*",
        "Referer"   : page_url,
    }

    status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ جاري فتح صفحة التنزيل...")

    def _download():
        session = requests.Session()
        session.get(page_url, headers=headers, timeout=30)

        headers["Referer"] = page_url
        for attempt in range(5):
            r = session.get(download_url, headers=headers, stream=True, timeout=60)
            if r.status_code == 200:
                return True, r, None
            elif r.status_code == 429:
                import time
                time.sleep(10)
            else:
                return False, None, f"❌ فشل التنزيل: {r.status_code}\n{r.text[:200]}"
        return False, None, "❌ السيرفر مشغول، حاول بعدين."

    ok, response, err = await asyncio.to_thread(_download)

    if not ok:
        await status_msg.edit_text(err, reply_markup=main_menu_kb())
        return

    # استخراج اسم الملف من الرابط
    path     = urlparse(download_url).path
    filename = unquote(path.split("/")[-1])
    if not filename.lower().endswith(".apk"):
        filename = filename + ".apk"

    save_path = os.path.join(WORKSPACE_DIR, filename)
    total     = int(response.headers.get("content-length", 0))
    downloaded = 0

    await status_msg.edit_text(f"⏳ جاري تنزيل:\n{filename}")

    def _save():
        nonlocal downloaded
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

    await asyncio.to_thread(_save)

    size_mb = downloaded / (1024 * 1024)
    await status_msg.edit_text(
        f"✅ تم التنزيل بنجاح!\n📦 {filename}\n📏 الحجم: {size_mb:.1f} MB\n\n📤 دلوقتي اختار إيه اللي عايز تعمله:"
    )

    # حفظ في حالة المستخدم واسأل: رفع تيليجرام أو فك APK
    st = get_state(uid)
    st["downloaded_apk_path"] = save_path
    st["original_apk_name"]   = filename

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 إرسال على تيليجرام", callback_data="dl_send_telegram")],
        [InlineKeyboardButton("🔧 فك الـ APK ومعالجته",  callback_data="dl_decompile")],
        [InlineKeyboardButton("⬅️ رجوع",               callback_data="back_main")],
    ])
    await context.bot.send_message(chat_id=chat_id, text="اختار:", reply_markup=kb)



def get_next_smali_num(project_dir):
    nums = []
    for name in os.listdir(project_dir):
        full = os.path.join(project_dir, name)
        if not os.path.isdir(full):
            continue
        if name == "smali":
            nums.append(1)
        else:
            m = re.match(r"^smali_classes(\d+)$", name)
            if m:
                nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else None


def inject_classes_sync(project_dir, zip_path):
    next_num = get_next_smali_num(project_dir)
    if next_num is None:
        return False, "❌ لا يوجد أي مجلد smali في المشروع."
    new_folder = os.path.join(project_dir, f"smali_classes{next_num}")
    os.makedirs(new_folder, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            count = len(zf.namelist())
            zf.extractall(new_folder)
        return True, f"✅ تم فك {count} ملف داخل:\nsmali_classes{next_num}"
    except zipfile.BadZipFile:
        return False, "❌ الملف ليس zip صحيح."
    except Exception as e:
        return False, f"❌ خطأ: {e}"


async def start_classes_flow(query):
    if not os.path.isdir(PROJECT_DIR):
        await query.edit_message_text("❌ لا يوجد مشروع مفكوك حالياً.", reply_markup=main_menu_kb())
        return
    has_zip = os.path.isfile(CLASSES_ZIP_PATH)
    kb = [
        [InlineKeyboardButton("📦 إضافة كـ smali_classes جديد", callback_data="classes_inject")],
        [InlineKeyboardButton("🔁 استبدال ملف classes.zip",     callback_data="classes_replace_zip")],
        [InlineKeyboardButton("⬅️ رجوع",                       callback_data="back_main")],
    ]
    txt = f"ملف classes.zip الحالي: {'✅ موجود' if has_zip else '❌ غير موجود'}\nاختار:"
    await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))


async def do_classes_inject(query):
    if not os.path.isfile(CLASSES_ZIP_PATH):
        await query.edit_message_text(
            f"❌ ملف classes.zip غير موجود في:\n{CLASSES_ZIP_PATH}\nابعت ملف جديد أولاً عبر «استبدال ملف classes.zip».",
            reply_markup=main_menu_kb(),
        )
        return
    await query.edit_message_text("⏳ جاري الإضافة...")
    ok, msg = await asyncio.to_thread(inject_classes_sync, PROJECT_DIR, CLASSES_ZIP_PATH)
    await query.edit_message_text(msg, reply_markup=main_menu_kb())


# =============================================================================
# إدارة التوقيعات
# =============================================================================
async def show_keystores_management(query):
    kslist = CFG.get("keystores", [])
    rows   = [[InlineKeyboardButton(f"🗑 حذف: {k['name']}", callback_data=f"ksdel:{k['name']}")] for k in kslist]
    rows.append([InlineKeyboardButton("➕ إضافة توقيع جديد", callback_data="ksnew_standalone")])
    rows.append([InlineKeyboardButton("⬅️ رجوع",            callback_data="back_main")])
    names = "\n".join(f"• {k['name']}" for k in kslist) or "لا يوجد توقيعات محفوظة."
    await query.edit_message_text(f"🔑 التوقيعات المحفوظة:\n{names}", reply_markup=InlineKeyboardMarkup(rows))


async def delete_keystore(query, name):
    k = find_keystore(name)
    if k:
        try:
            os.remove(k["path"])
        except Exception:
            pass
        CFG["keystores"] = [x for x in CFG["keystores"] if x["name"] != name]
        save_config(CFG)
        await query.edit_message_text(f"✅ تم حذف التوقيع \"{name}\".", reply_markup=main_menu_kb())
    else:
        await query.edit_message_text("❌ التوقيع غير موجود.", reply_markup=main_menu_kb())


# =============================================================================
# حذف / ريستارت
# =============================================================================
async def delete_project(query):
    # مسح مجلد workspace بالكامل (المشروع + APK + أي ملفات مؤقتة + التوقيعات)
    shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)
    os.makedirs(KEYSTORES_DIR, exist_ok=True)
    # التوقيعات (keystores) كانت متخزنة فيزيائياً داخل workspace، فبعد مسحها
    # لازم نفضي القائمة في config.json علشان البوت ما يفتكرش توقيعات بقت مش موجودة
    CFG["keystores"] = []
    save_config(CFG)
    await query.edit_message_text(
        "🗑 تم مسح ملف الـ workspace بالكامل (المشروع + الـ APK + الملفات المؤقتة + التوقيعات).",
        reply_markup=main_menu_kb(),
    )


async def soft_restart(query):
    await query.edit_message_text("♻️ جاري إعادة تشغيل البوت (بدون حذف المشروع أو نسخة الـ APK)...")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def full_restart(query):
    await query.edit_message_text("🔄 جاري مسح الـ workspace بالكامل وإعادة تشغيل البوت...")
    shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)
    os.makedirs(KEYSTORES_DIR, exist_ok=True)
    CFG["keystores"] = []
    save_config(CFG)
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# =============================================================================
# مُوزّع الأزرار
# =============================================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    if not is_admin(uid):
        await query.answer("⛔️ غير مسموح.", show_alert=True)
        return

    data = query.data
    st   = get_state(uid)

    # ── قفل ضد الضغط المزدوج / السريع على عمليات تقيلة بتلمس ملفات المشروع ──
    # (فك APK، تجميع وتوقيع، تنفيذ بحث/استبدال/حذف/إضافة) عشان نمنع تشغيل
    # نفس العملية مرتين في نفس الوقت وتسبب حذف/تلف لمجلد المشروع وهو لسه شغال.
    HEAVY_ACTIONS = {
        "dl_decompile", "sign_random", "menu_build",
        "confirm_delete", "confirm_replace", "confirm_insert",
    }
    is_heavy = data in HEAVY_ACTIONS or data.startswith("ksbuild:")
    if is_heavy and st.get("busy"):
        await query.answer("⏳ في عملية شغالة بالفعل، استنى تخلص الأول.", show_alert=True)
        return
    if is_heavy:
        st["busy"] = True

    # ── حماية ضد ضغط زرار رفع (تيليجرام/GitHub/الاتنين) من رسالة قديمة ──
    # كل مرة يحصل فشل في الرفع، بنبعت رسالة جديدة فيها 3 أزرار. لو فشلت
    # أكتر من مرة، بيتكوّن كذا رسالة فيها أزرار بنفس الأسماء - فلو ضغط
    # المستخدم زرار من رسالة قديمة، لازم نتجاهله بدل ما ننفذ أمر غلط
    # (زي ما كان بيحصل: "GitHub بس" من رسالة قديمة كانت فعليًا "الاتنين").
    UPLOAD_RELATED = {"upload_telegram", "upload_github", "upload_both"} 
    is_upload_action = data in UPLOAD_RELATED or data.startswith("retry_upload_")
    if is_upload_action:
        active_msg_id = st.get("active_upload_msg_id")
        if active_msg_id is not None and query.message.message_id != active_msg_id:
            await query.answer(
                "⚠️ الرسالة دي قديمة. استخدم آخر رسالة رفع ظهرت، أو رجّع من القائمة الرئيسية.",
                show_alert=True,
            )
            return

    await query.answer()

    try:
        await _handle_callback(query, context, uid, data, st)
    finally:
        if is_heavy:
            st["busy"] = False


async def _handle_callback(query, context, uid, data, st):
    # ── رجوع ──
    if data == "back_main":
        reset_state(uid)
        await query.edit_message_text("📋 القائمة الرئيسية:", reply_markup=main_menu_kb())

    # ── بناء وتوقيع ──
    elif data == "menu_build":
        await start_build_flow(query, uid)
    elif data == "sign_random":
        status_msg = await query.edit_message_text("⏳ جاري التجميع والتوقيع...")
        await do_build_and_sign(context, query.message.chat_id, status_msg, uid, mode="random")
    elif data == "sign_manual":
        await show_keystores_for_build(query)
    elif data.startswith("ksbuild:"):
        ks_name    = data.split(":", 1)[1]
        status_msg = await query.edit_message_text("⏳ جاري التجميع والتوقيع...")
        await do_build_and_sign(context, query.message.chat_id, status_msg, uid, mode="custom", ks_name=ks_name)
    elif data == "ksbuild_new":
        st["await"] = "new_ks_name_for_build"
        await query.edit_message_text("✍️ اكتب اسم/تسمية لهذا التوقيع الجديد:")

    # ── اختيار وجهة الرفع ──
    elif data in ("upload_telegram", "upload_github", "upload_both"):
        dest_map = {"upload_telegram": "telegram", "upload_github": "github", "upload_both": "both"}
        await query.edit_message_text("⏳ جاري الرفع...")
        await deliver_signed_apk(context, query.message.chat_id, uid, dest_map[data])

    elif data.startswith("retry_upload_"):
        destination = data.replace("retry_upload_", "")
        await query.edit_message_text("⏳ جاري إعادة المحاولة...")
        await deliver_signed_apk(context, query.message.chat_id, uid, destination)

    # ── مسح المشروع بعد نجاح الرفع (سؤال) ──
    elif data.startswith("delproj_after_upload:"):
        choice = data.split(":", 1)[1]
        if choice == "yes":
            shutil.rmtree(PROJECT_DIR, ignore_errors=True)
            await query.edit_message_text("🧹 تم حذف مجلد المشروع.", reply_markup=main_menu_kb())
        else:
            await query.edit_message_text(
                "📂 تمام، المشروع لسه موجود، تقدر تشتغل عليه تاني (بحث/استبدال، تجميع وتوقيع...) من القائمة.",
                reply_markup=main_menu_kb(),
            )

    # ── بحث واستبدال ──
    elif data == "menu_search":
        await start_search_flow(query, st)
    elif data in ("search_replace", "search_insert", "search_delete", "search_only"):
        await handle_search_action(query, context, st, data)
    elif data.startswith("fmpick:"):
        _, action, sel = data.split(":", 2)
        matches = st.get("file_matches", [])
        if sel == "all":
            st["search_target_file"] = None
        else:
            idx = int(sel)
            if idx >= len(matches):
                await query.edit_message_text("❌ حصل خطأ، جرب تاني من القائمة.", reply_markup=main_menu_kb())
                return
            st["search_target_file"] = matches[idx][0]
        await proceed_search_action(query, st, action)
    elif data == "confirm_delete":
        await query.edit_message_text("⏳ جاري الحذف...")
        result = await asyncio.to_thread(
            do_delete_sync, PROJECT_DIR, st.get("search_text", ""), st.get("search_target_file")
        )
        await query.edit_message_text(result, reply_markup=main_menu_kb())
    elif data == "confirm_replace":
        await query.edit_message_text("⏳ جاري الاستبدال...")
        result = await asyncio.to_thread(
            do_replace_sync, PROJECT_DIR, st.get("search_text", ""), st.get("replace_text", ""),
            st.get("search_target_file"),
        )
        await query.edit_message_text(result, reply_markup=main_menu_kb())

    # ── استبدال ملف/ملفات بمطابقة الاسم تلقائي ──
    elif data == "menu_search_filename":
        await start_search_filename_flow(query, st)

    # ── استبدال ملف بمسار محدد (يدوي) ──
    elif data == "menu_path_replace":
        await start_path_replace_flow(query, st)

    # ── تحديد المكان بالظبط من نتايج البحث بالمسار ──
    elif data.startswith("pathpick:"):
        candidates = st.get("path_replace_candidates")
        if not candidates:
            await query.edit_message_text("❌ حصل خطأ (الطلب ده مش متاح دلوقتي)، جرب تاني من القائمة.", reply_markup=main_menu_kb())
            return
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        if idx < 0 or idx >= len(candidates):
            await query.edit_message_text("❌ حصل خطأ، جرب تاني من القائمة.", reply_markup=main_menu_kb())
            return
        st.pop("path_replace_candidates", None)
        st["path_replace_target"] = candidates[idx]
        st["await"] = "path_replace_upload"
        await query.edit_message_text(
            f"✅ تم تحديد المسار:\n📄 {candidates[idx]}\n\n"
            "دلوقتي ابعت الملف اللي عايز تستبدله بيه:"
        )

    # ── تحديد المكان بالظبط لما اسم الملف يكون موجود في أكتر من فولدر ──
    elif data.startswith("fnpick:"):
        sel = data.split(":", 1)[1]
        item = st.get("fnreplace_current")
        queue = st.get("fnreplace_queue", [])
        if not item or not queue or queue[0] is not item:
            await query.edit_message_text("❌ حصل خطأ (الطلب ده مش متاح دلوقتي)، جرب تاني من القائمة.", reply_markup=main_menu_kb())
            return

        report = st.get("fnreplace_report", {"replaced": [], "skipped": []})
        if sel == "skip":
            report["skipped"].append((item["fname"], "تخطي يدوي"))
        else:
            try:
                idx = int(sel)
            except ValueError:
                idx = -1
            rel_paths = item["rel_paths"]
            if idx < 0 or idx >= len(rel_paths):
                await query.edit_message_text("❌ حصل خطأ، جرب تاني من القائمة.", reply_markup=main_menu_kb())
                return
            target_rel = rel_paths[idx]
            result = await asyncio.to_thread(replace_file_sync, PROJECT_DIR, target_rel, item["local_path"])
            if result.startswith("✅"):
                report["replaced"].append((item["fname"], target_rel))
            else:
                report["skipped"].append((item["fname"], "فشل الاستبدال"))

        st["fnreplace_report"] = report
        queue.pop(0)
        st["fnreplace_queue"] = queue

        if queue:
            next_item = queue[0]
            st["fnreplace_current"] = next_item
            await query.edit_message_text(
                format_fnreplace_prompt(next_item["fname"], next_item["rel_paths"]),
                reply_markup=build_fnreplace_kb(next_item["rel_paths"]),
            )
        else:
            await query.edit_message_text(finalize_fnreplace_report(st), reply_markup=main_menu_kb())

    # ── نشر تطبيق على الموقع ──
    elif data == "menu_publish_app":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 آه، من رابط صفحة تطبيق", callback_data="pubauto_yes")],
            [InlineKeyboardButton("✍️ لأ، هدخل البيانات يدوي", callback_data="pubauto_no")],
        ])
        await query.edit_message_text(
            "📤 نشر تطبيق جديد على الموقع\n\n"
            "عايز تملي الحقول أوتوماتيك من رابط صفحة التطبيق على Google Play، "
            "ولا هتكتب البيانات بنفسك؟",
            reply_markup=kb,
        )
    elif data == "pubauto_yes":
        st["await"] = "publish_autofill_url"
        await query.edit_message_text(
            "🔗 ابعت رابط صفحة التطبيق على Google Play دلوقتي، مثال:\n"
            "`https://play.google.com/store/apps/details?id=com.zhiliaoapp.musically&hl=ar`\n\n"
            "هحاول أطلع منها كل حاجة أقدر عليها (الاسم، المطور، الأيقونة، الوصف، "
            "التقييم، الحجم، لقطات الشاشة بأعلى جودة...إلخ)، وأي حاجة مش لاقيها "
            "هسألك عليها عادي — ورابط التنزيل المباشر هتكتبه انت دايمًا."
        )
    elif data == "pubauto_no":
        await start_publish_flow(query, context, st)
    elif data.startswith("pubfchoice:"):
        _, idx_s, cidx_s = data.split(":", 2)
        await handle_publish_choice_callback(query, context, st, int(idx_s), cidx_s)
    elif data.startswith("pubfbool:"):
        _, idx_s, val_s = data.split(":", 2)
        await handle_publish_choice_callback(query, context, st, int(idx_s), val_s)
    elif data.startswith("pubfclear:"):
        idx = int(data.split(":", 1)[1])
        await handle_publish_clear_callback(query, context, st, idx)
    elif data == "pubfback":
        await edit_publish_review(query, st)
    elif data == "pubpublish":
        await handle_publish_finalize(query, context, st)
    elif data == "pubcancel":
        st.pop("publish_data", None)
        st.pop("publish_step", None)
        st.pop("publish_list_temp", None)
        st.pop("publish_edit_idx", None)
        st.pop("await", None)
        delete_publish_draft(query.message.chat_id)
        await query.edit_message_text("❌ اتلغى النشر.")
        await context.bot.send_message(query.message.chat_id, "📋 القائمة الرئيسية:", reply_markup=main_menu_kb())
    elif data.startswith("pubf:"):
        idx = int(data.split(":", 1)[1])
        await handle_publish_field_tap(query, context, st, idx)

    # ── نشر التطبيق (اللي اتنشر لسه على الموقع) في قناة تيليجرام ──
    elif data == "chpub_yes":
        app_data = st.get("last_published_app")
        if not app_data:
            await query.edit_message_text("❌ مفيش تطبيق محفوظ حاليًا عشان أنشره في القناة.", reply_markup=main_menu_kb())
            return
        await query.edit_message_text("⏳ جاري النشر في القناة...")
        ok, msg = await post_app_to_channel(context.bot, app_data)
        await context.bot.send_message(query.message.chat_id, msg, reply_markup=main_menu_kb())
        if ok:
            st.pop("last_published_app", None)
    elif data == "chpub_no":
        st.pop("last_published_app", None)
        await query.edit_message_text("👌 تمام، مش هينشر في القناة.")
        await context.bot.send_message(query.message.chat_id, "📋 القائمة الرئيسية:", reply_markup=main_menu_kb())
    elif data == "confirm_insert":
        await query.edit_message_text("⏳ جاري الإضافة...")
        result = await asyncio.to_thread(
            do_insert_after_sync, PROJECT_DIR, st.get("search_text", ""), st.get("insert_text", ""),
            st.get("search_target_file"),
        )
        await query.edit_message_text(result, reply_markup=main_menu_kb())

    # ── classes.zip ──
    elif data == "menu_classes":
        await start_classes_flow(query)
    elif data == "classes_inject":
        await do_classes_inject(query)
    elif data == "classes_replace_zip":
        st["await"] = "classes_zip_upload"
        await query.edit_message_text("📤 ابعت ملف classes.zip الجديد دلوقتي.")

    # ── فحص/تحميل Java ──
    elif data == "menu_check_java":
        await check_java_flow(query)

    # ── فحص/تحميل أدوات APK ──
    elif data == "menu_check_tools":
        await check_tools_flow(query)

    # ── إدارة التوقيعات ──
    elif data == "menu_keystores":
        await show_keystores_management(query)
    elif data.startswith("ksdel:"):
        await delete_keystore(query, data.split(":", 1)[1])
    elif data == "ksnew_standalone":
        st["await"] = "new_ks_name_standalone"
        await query.edit_message_text("✍️ اكتب اسم لهذا التوقيع الجديد:")

    # ── تحديث توكن GitHub ──
    elif data == "menu_update_github_token":
        st["await"] = "new_github_token"
        current_repo = CFG.get("github_repo", "غير محدد")
        await query.edit_message_text(
            "🐙 تحديث توكن GitHub\n\n"
            f"الريبو الحالي: `{current_repo}`\n\n"
            "ابعت التوكن الجديد (Personal Access Token) دلوقتي كرسالة واحدة.\n"
            "⚠️ همسح رسالتك اللي فيها التوكن فورًا من الشات بعد الحفظ، "
            "عشان ميفضلش ظاهر في تاريخ المحادثة."
        )

    # ── نشر تطبيق في قناة تيليجرام مباشرة عن طريق لينك صفحته على الموقع ──
    elif data == "menu_channel_publish_link":
        st["await"] = "channel_publish_link"
        await query.edit_message_text(
            "📢 نشر تطبيق في قناة تيليجرام\n\n"
            "ابعت لينك صفحة التطبيق على الموقع (اللي فيه \"?app=...\")، "
            "وهجيب بياناته (الاسم، الوصف، الصورة، الحجم...) من الموقع وأنشرها في القناة تلقائيًا."
        )

    # ── تحديث آيدي/يوزر قناة تيليجرام ──
    elif data == "menu_update_channel_id":
        st["await"] = "new_channel_id"
        current_channel = CFG.get("telegram_channel_id", "").strip() or "غير محدد"
        await query.edit_message_text(
            "🆔 تحديث آيدي/يوزر قناة تيليجرام\n\n"
            f"القيمة الحالية: `{current_channel}`\n\n"
            "ابعت يوزر القناة (مثال: `@my_channel`) أو آيديها الرقمي (مثال: `-1001234567890`).\n"
            "لازم البوت يكون أدمن في القناة دي بصلاحية \"نشر رسائل\"."
        )

    # ── تحديث User UID (Firebase) ──
    elif data == "menu_update_owner_uid":
        st["await"] = "new_owner_uid"
        current_uid = CFG.get("firebase_owner_uid", "").strip() or "غير محدد"
        await query.edit_message_text(
            "🆔 تحديث User UID\n\n"
            f"القيمة الحالية: `{current_uid}`\n\n"
            "ابعت الـ User UID بتاع حساب الأدمن في Firebase Authentication "
            "(هتلاقيه في Firebase Console → Authentication → Users)، مثال:\n"
            "`QtoWqaDrgQUc0cuyERXLjXKRlj72`\n\n"
            "ده هيتحفظ ويُستخدم تلقائيًا كحقل \"ownerUid\" في كل تطبيق تنشره."
        )

    # ── تحديث بيانات دخول Firebase (إيميل + باسورد) ──
    elif data == "menu_update_firebase_login":
        st["await"] = "new_firebase_email"
        st.pop("_firebase_email_temp", None)
        current_email = CFG.get("firebase_admin_email", "").strip() or "غير محدد"
        await query.edit_message_text(
            "📧 تحديث بيانات دخول Firebase\n\n"
            f"الإيميل الحالي: `{current_email}`\n\n"
            "ابعت إيميل حساب الأدمن في Firebase Authentication دلوقتي "
            "(نفس الحساب اللي مضاف كأدمن في قواعد أمان Firestore عندك)."
        )

    # ── تنزيل من رابط ──
    elif data == "menu_download_url":
        st["await"] = "download_page_url"
        await query.edit_message_text(
            "📥 تنزيل APK من رابط\n\n"
            "الخطوة 1️⃣: أرسل رابط صفحة التنزيل\n"
            "(مثال: https://liteapks.com/download/xxx/1)"
        )

    elif data == "dl_send_telegram":
        apk_path = get_state(uid).get("downloaded_apk_path", "")
        apk_name = get_state(uid).get("original_apk_name", "app.apk")
        if not apk_path or not os.path.isfile(apk_path):
            await query.edit_message_text("❌ الملف مش موجود.", reply_markup=main_menu_kb())
            return
        await query.edit_message_text("⏳ جاري الإرسال على تيليجرام...")
        try:
            with open(apk_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=f,
                    filename=apk_name,
                    caption="✅ الملف جاهز!",
                )
            os.remove(apk_path)
            get_state(uid).pop("downloaded_apk_path", None)
            await context.bot.send_message(query.message.chat_id, "✅ تم الإرسال!", reply_markup=main_menu_kb())
        except Exception as e:
            await context.bot.send_message(query.message.chat_id, f"❌ فشل الإرسال: {e}", reply_markup=main_menu_kb())

    elif data == "dl_decompile":
        apk_path = get_state(uid).get("downloaded_apk_path", "")
        if not apk_path or not os.path.isfile(apk_path):
            await query.edit_message_text("❌ الملف مش موجود.", reply_markup=main_menu_kb())
            return
        ok_jar, jar_msg = verify_jar_file(BAKSMALI_JAR)
        if not ok_jar:
            await query.edit_message_text(jar_msg, reply_markup=main_menu_kb())
            return
        # انسخ الملف لمسار الـ APK الأساسي وافكه
        shutil.copy2(apk_path, APK_COPY_PATH)
        if os.path.isdir(PROJECT_DIR):
            shutil.rmtree(PROJECT_DIR, ignore_errors=True)
        msg = await query.edit_message_text("⏳ جاري فك الـ APK (baksmali - أكواد فقط)...")
        apk_size = os.path.getsize(APK_COPY_PATH)
        ok, log_text, elapsed = await run_async_with_heartbeat(
            decompile_apk_fast(APK_COPY_PATH, PROJECT_DIR),
            msg, "⏳ جاري فك الـ APK (baksmali - أكواد فقط)...",
        )
        if ok:
            record_stage_time("decompile", apk_size, elapsed)
        try:
            os.remove(apk_path)
        except Exception:
            pass
        get_state(uid).pop("downloaded_apk_path", None)
        if ok:
            await msg.edit_text(f"✅ تم فك الـ APK بنجاح في {format_duration(elapsed)}.\n" + tail_log(log_text))
            await context.bot.send_message(query.message.chat_id, "📋 القائمة الرئيسية:", reply_markup=main_menu_kb())
        else:
            await msg.edit_text("❌ فشلت عملية الفك:\n" + tail_log(log_text, 1500), reply_markup=main_menu_kb())

    # ── حذف / ريستارت ──
    elif data == "menu_delete_project":
        await delete_project(query)
    elif data == "menu_soft_restart":
        await soft_restart(query)
    elif data == "menu_full_restart":
        await full_restart(query)


# =============================================================================
# معالج أخطاء عام — بدل ما أي استثناء يتبلع بصمت جوه المكتبة
# =============================================================================
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    from telegram.error import BadRequest

    # ── تنبيه: BadRequest هو subclass من NetworkError في المكتبة دي، فلازم
    # نتعامل معاه قبل شرط NetworkError العام، وإلا هيقع في التصنيف الغلط
    # ("تأخير عابر، هيتحل لوحده") مع إنه في الحقيقة خطأ دائم (حجم ملف/طول
    # رسالة) مش هيتحل بإعادة المحاولة أبدًا. ──────────────────────────────
    if isinstance(context.error, BadRequest):
        err_text = str(context.error)
        low = err_text.lower()
        if "file is too big" in low:
            explain = (
                "🚫 الملف اللي البوت بيحاول ينزّله من تليجرام أكبر من الحد المسموح به "
                "عن طريق Bot API العادي (20MB أقصى تحميل). ده حد ثابت من تليجرام، "
                "مش مشكلة شبكة، ومش هيتحل بإعادة المحاولة.\n"
                "الحل: إما تبعت/تحوّل ملف أصغر من 20MB، أو تشغّل Local Bot API Server "
                "بتاعك (بيرفع الحد لـ 2000MB)."
            )
        elif "message is too long" in low or "message_too_long" in low:
            explain = (
                "🚫 النص اللي البوت حاول يبعته أطول من حد تليجرام (4096 حرف للرسائل "
                "العادية، 1024 حرف لو كابشن تحت صورة). ده حد ثابت، مش مشكلة شبكة، "
                "ومش هيتحل بإعادة المحاولة.\n"
                "الحل: تقصير النص (مثلاً وصف التطبيق) قبل الإرسال أو تقسيمه لأكتر من رسالة."
            )
        else:
            explain = f"⚠️ طلب اترفض من تليجرام (Bad Request) — خطأ دائم مش عابر: {err_text}"

        log.warning(f"🚫 BadRequest دائم (مش تأخير شبكة): {err_text}")
        admins = CFG.get("admin_ids") or []
        for admin_id in admins:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"{explain}\n(Instance: {INSTANCE_ID})",
                )
            except Exception:
                pass
        return

    # ── TimedOut / NetworkError عادةً مجرد تهنيقة شبكة عابرة (خصوصًا على
    # Railway)، مش باج فعلي في الكود — مكتبة python-telegram-bot بتعيد
    # المحاولة تلقائيًا لحالات كتير منها. فبدل ما نبعت تراسباك طويل مرعب
    # للأدمن في كل مرة، نبعت تنبيه قصير ونكمل عادي.
    if isinstance(context.error, (TimedOut, NetworkError)):
        log.warning(f"⏳ تأخير/انقطاع شبكة عابر: {context.error}")
        admins = CFG.get("admin_ids") or []
        for admin_id in admins:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"⏳ تأخير شبكة عابر مع تليجرام (Instance: {INSTANCE_ID}): "
                        f"{context.error}\nالبوت بيحاول تاني تلقائيًا، مش لازم تعمل حاجة."
                    ),
                )
            except Exception:
                pass
        return

    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    log.error(f"🔥 استثناء غير متوقع:\n{tb}")

    short_tb = tail_log(tb, 1200)
    admins = CFG.get("admin_ids") or []
    for admin_id in admins:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🔥 حصل خطأ غير متوقع في البوت (Instance: {INSTANCE_ID}):\n\n{short_tb}",
            )
        except Exception:
            pass

    # لو الخطأ جه من زرار، رجّع للمستخدم رد بدل ما يتعلق منتظر
    try:
        if update and getattr(update, "callback_query", None):
            st = get_state(update.callback_query.from_user.id)
            st["busy"] = False
            await update.callback_query.message.reply_text(
                "❌ حصل خطأ غير متوقع أثناء تنفيذ العملية. اتبعتلك التفاصيل لو انت أدمن.",
                reply_markup=main_menu_kb(),
            )
    except Exception:
        pass


# =============================================================================
# main
# =============================================================================
def main():
    token = CFG.get("bot_token", "")
    if not token or token == "PUT_YOUR_BOT_TOKEN_HERE":
        print("❌ من فضلك ضع التوكن في config.json (bot_token) قبل التشغيل.")
        sys.exit(1)

    # ── مهلات شبكة أطول من الافتراضي (5 ثواني بس) ──────────────────────────
    # ده اللي كان بيسبب أخطاء "TimedOut" بشكل متكرر على سيرفرات زي Railway،
    # لأن أي تأخير بسيط في الشبكة (أو استجابة تليجرام) أكبر من 5 ثواني كان
    # كافي يفشّل الطلب. رفعنا المهلات هنا لكل الطلبات العادية (رسائل/أزرار)،
    # ومهلة أطول شوية لـ get_updates (طول polling) عشان تستحمل استخدام
    # long-polling العادي بدون timeouts وهمية.
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=40.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info(f"🚀 البوت شغال... (Instance: {INSTANCE_ID})")
    # drop_pending_updates=True: يمسح أي رسائل/كولباك متراكمة من نسخة قديمة عالقة،
    # وده كمان بيستحوذ فورًا على جلسة الـ long-polling ويطرد أي نسخة تانية شغالة
    # بنفس التوكن (تليجرام مش بيسمح إلا بمستهلك getUpdates واحد بس في نفس الوقت).
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        is_conflict = "Conflict" in str(e) or "terminated by other getUpdates" in str(e)
        if is_conflict:
            # فيه نسخة تانية استحوذت على الـ polling بعدنا (يعني هي الأحدث غالبًا).
            # بدل ما نموت ونخلي Railway يعيد تشغيلنا فورًا فنتخانق تاني على طول
            # (وده اللي بيسبب فلاشينج القوائم واختفاء زرارين وظهورهم بالتبادل)،
            # ننتظر فترة عشوائية طويلة نسبيًا الأول، عشان نسيب فرصة حقيقية
            # للنسخة التانية إنها تستقر، وبعدين نخرج بكود خطأ يخلي Railway
            # يعرف إن التشغيل ده فشل عمدًا.
            backoff = random.randint(45, 90)
            log.error(
                f"🚨 تعارض: في نسخة تانية من البوت شغالة بنفس التوكن دلوقتي! (Instance: {INSTANCE_ID})\n"
                f"⏳ هستنى {backoff} ثانية قبل ما أطلع، عشان منتخانقش مع النسخة التانية.\n"
                "لحل المشكلة نهائيًا: روح Railway → Deployments وتأكد إن مفيش أكتر من\n"
                "deployment/replica شغال في نفس الوقت (احذف/أوقف أي نسخة قديمة يدويًا)،\n"
                "أو إنك مش مشغّل نسخة تانية محليًا على جهازك في نفس الوقت."
            )
            time.sleep(backoff)
        raise


if __name__ == "__main__":
    main()
