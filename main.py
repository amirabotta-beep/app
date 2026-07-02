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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

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

for _d in (TOOLS_DIR, WORKSPACE_DIR, KEYSTORES_DIR):
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
            for secret_key in ("bot_token", "github_token"):
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
# القائمة الرئيسية
# =============================================================================
def main_menu_kb():
    rows = [
        [InlineKeyboardButton("📥 تنزيل APK من رابط", callback_data="menu_download_url")],
        [InlineKeyboardButton("🖊 تجميع وتوقيع", callback_data="menu_build")],
        [InlineKeyboardButton("🔍 بحث واستبدال smali", callback_data="menu_search")],
        [InlineKeyboardButton("📄 بحث باسم ملف واستبداله", callback_data="menu_search_filename")],
        [InlineKeyboardButton("📦 نقل classes.zip", callback_data="menu_classes")],
        [InlineKeyboardButton("🔑 إدارة شهادات التوقيع", callback_data="menu_keystores")],
        [InlineKeyboardButton("🐙 تحديث توكن GitHub", callback_data="menu_update_github_token")],
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
        f = await doc.get_file()
        await f.download_to_drive(CLASSES_ZIP_PATH)
        st.pop("await", None)
        await update.message.reply_text("✅ تم استبدال classes.zip بنجاح.", reply_markup=main_menu_kb())
        return

    if st.get("await") == "keystore_file_upload":
        if not (name.endswith(".jks") or name.endswith(".keystore")):
            await update.message.reply_text("❌ من فضلك ابعت ملف keystore بصيغة .jks أو .keystore.")
            return
        ks_name = st.get("new_ks_name") or random_string(6)
        dest    = os.path.join(KEYSTORES_DIR, f"{ks_name}.jks")
        f       = await doc.get_file()
        await f.download_to_drive(dest)
        st["new_ks_path"] = dest
        st["await"]       = "keystore_alias"
        await update.message.reply_text("✅ تم استلام ملف الـ keystore.\n✍️ دلوقتي اكتب الـ Alias:")
        return

    if st.get("await") == "filename_replace_upload":
        target_rel = st.get("filename_replace_target")
        if not target_rel:
            st.pop("await", None)
            await update.message.reply_text("❌ حصل خطأ، جرب تاني من القائمة.", reply_markup=main_menu_kb())
            return
        tmp_path = os.path.join(WORKSPACE_DIR, f"upload_{random_string(6)}_{doc.file_name or 'file'}")
        f = await doc.get_file()
        await f.download_to_drive(tmp_path)
        result = await asyncio.to_thread(replace_file_sync, PROJECT_DIR, target_rel, tmp_path)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        st.pop("await", None)
        st.pop("filename_replace_target", None)
        st.pop("filename_matches", None)
        await update.message.reply_text(result, reply_markup=main_menu_kb())
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
        f   = await doc.get_file()
        await f.download_to_drive(APK_COPY_PATH)

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

    if awaiting == "search_filename_keyword":
        st.pop("await", None)
        await handle_filename_search_result(update, st, text)
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

    # ── تنظيف بس لو نجح كل المطلوب ──
    if all_success:
        try:
            os.remove(out_apk)
        except Exception:
            pass
        shutil.rmtree(PROJECT_DIR, ignore_errors=True)
        try:
            os.remove(APK_COPY_PATH)
        except Exception:
            pass
        st.pop("signed_apk_path",  None)
        st.pop("original_apk_name", None)
        cleanup_note = "\n\n🧹 تم حذف ملفات المشروع تلقائياً."
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


def find_files_by_name(project_dir, name_query):
    """يبحث عن كل الملفات (أي نوع) اللي اسمها يحتوي على name_query
    (بدون حساسية لحالة الأحرف)، ويرجّع المسارات النسبية مرتبة."""
    q = (name_query or "").strip().lower()
    if not q:
        return []
    matches = []
    for root_dir, _, filenames in os.walk(project_dir):
        for fn in filenames:
            if q in fn.lower():
                matches.append(os.path.relpath(os.path.join(root_dir, fn), project_dir))
    matches.sort()
    return matches


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
# البحث باسم الملف واستبداله
# =============================================================================
async def start_search_filename_flow(query, st):
    if not os.path.isdir(PROJECT_DIR):
        await query.edit_message_text("❌ لا يوجد مشروع مفكوك حالياً.", reply_markup=main_menu_kb())
        return
    st["await"] = "search_filename_keyword"
    await query.edit_message_text(
        "📄 اكتب اسم الملف أو جزء منه اللي عايز تدور عليه في كل ملفات المشروع (مثلاً: pk.smali):"
    )


async def handle_filename_search_result(update, st, name_query):
    matches = await asyncio.to_thread(find_files_by_name, PROJECT_DIR, name_query)
    if not matches:
        await update.message.reply_text(
            f"❌ مفيش أي ملف اسمه بيحتوي على:\n\"{name_query}\"",
            reply_markup=main_menu_kb(),
        )
        return

    st["filename_matches"] = matches
    rows = []
    for idx, rel in enumerate(matches[:30]):
        rows.append([InlineKeyboardButton(f"📄 {rel}", callback_data=f"fnpick:{idx}")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="back_main")])
    extra = "" if len(matches) <= 30 else f"\n(بيتم عرض أول 30 من {len(matches)} نتيجة)"
    await update.message.reply_text(
        f"🔍 لقيت {len(matches)} ملف بيطابق:\n\"{name_query}\"{extra}\n\n"
        "اختار الملف اللي عايز تستبدله:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


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

    # ── بحث باسم ملف واستبداله ──
    elif data == "menu_search_filename":
        await start_search_filename_flow(query, st)
    elif data.startswith("fnpick:"):
        idx = int(data.split(":", 1)[1])
        matches = st.get("filename_matches", [])
        if idx >= len(matches):
            await query.edit_message_text("❌ حصل خطأ، جرب تاني من القائمة.", reply_markup=main_menu_kb())
            return
        target_rel = matches[idx]
        st["filename_replace_target"] = target_rel
        st["await"] = "filename_replace_upload"
        await query.edit_message_text(f"📄 هيتم استبدال:\n{target_rel}\n\n📤 ابعت الملف البديل دلوقتي.")
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

    app = Application.builder().token(token).build()
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
