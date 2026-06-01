from pathlib import Path
from datetime import datetime

# ============================================
# НАСТРОЙКИ
# ============================================

ROOT_DIR = Path(__file__).parent.resolve()
OUTPUT_FILE = ROOT_DIR / "app-export.txt"

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
    ".idea",
    ".vscode"
}

EXCLUDE_FILES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "app-export.txt"
}

BINARY_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".dll",
    ".so",
    ".bin",
    ".pyc"
}

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# ============================================
# СОЗДАЕМ ФАЙЛ
# ============================================

with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
    out.write("=== ByteWizard App Export ===\n")
    out.write(
        f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
    )
    out.write("=============================================\n\n")

processed = 0
skipped = 0

# ============================================
# ОБХОД ФАЙЛОВ
# ============================================

for file_path in ROOT_DIR.rglob("*"):

    # Только файлы
    if not file_path.is_file():
        continue

    # Пропуск директорий
    if any(part in EXCLUDE_DIRS for part in file_path.parts):
        skipped += 1
        continue

    # Пропуск файлов
    if file_path.name in EXCLUDE_FILES:
        skipped += 1
        continue

    # Пропуск бинарников
    if file_path.suffix.lower() in BINARY_EXTENSIONS:
        skipped += 1
        continue

    # Пропуск больших файлов
    try:
        size = file_path.stat().st_size

        if size > MAX_FILE_SIZE:
            print(f"[SKIP BIG] {file_path}")
            skipped += 1
            continue

    except Exception:
        skipped += 1
        continue

    # Относительный путь
    relative_path = file_path.relative_to(ROOT_DIR)

    # Чтение файла
    try:
        content = file_path.read_text(
            encoding="utf-8",
            errors="ignore"
        )
    except Exception as e:
        print(f"[ERROR] {relative_path}: {e}")
        skipped += 1
        continue

    # Если файл пустой
    if not content.strip():
        continue

    # Запись в общий txt
    with open(OUTPUT_FILE, "a", encoding="utf-8") as out:
        out.write("\n")
        out.write("─────────────────────────────────────────\n")
        out.write(f"FILE: {relative_path}\n")
        out.write("─────────────────────────────────────────\n\n")

        out.write(content)
        out.write("\n\n")

    processed += 1
    print(f"[OK] {relative_path}")

# ============================================
# ФИНАЛ
# ============================================

final_size = OUTPUT_FILE.stat().st_size

print("\n===================================")
print(f"Готово!")
print(f"Файл: {OUTPUT_FILE.name}")
print(f"Обработано: {processed}")
print(f"Пропущено: {skipped}")
print(f"Размер: {final_size:,} байт")