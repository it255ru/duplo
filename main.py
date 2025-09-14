import os
import argparse
import hashlib
import time
import pickle
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Словарь для классификации файлов по расширениям
FILE_CATEGORIES = {
    'images': {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.raw', '.heic', '.svg', '.ico', '.jpe', '.tif'},
    'videos': {'.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp', '.3gpp', '.m2ts', '.mts', '.ts', '.vob'},
    'audio': {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a', '.amr', '.mka', '.opus'},
    'documents': {'.pdf', '.doc', '.docx', '.txt', '.rtf', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp', '.md', '.tex'},
    'archives': {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.tbz2'},
    'executables': {'.exe', '.msi', '.bat', '.cmd', '.sh', '.bin', '.app', '.apk', '.deb', '.rpm'},
    'scripts': {'.py', '.js', '.java', '.c', '.cpp', '.html', '.css', '.php', '.rb', '.pl', '.sh', '.bash', '.ps1', '.vbs'},
    'data': {'.db', '.csv', '.json', '.xml', '.sql', '.sqlite', '.sqlite3', '.mdb', '.accdb', '.ini', '.cfg'},
    'system': {'.dll', '.sys', '.inf', '.cat', '.drv', '.ocx', '.cpl'},
    'fonts': {'.ttf', '.otf', '.woff', '.woff2', '.eot', '.fon'},
    'design': {'.psd', '.ai', '.sketch', '.fig', '.xd', '.indd'},
    'other': set()
}

def get_file_category(extension):
    """Определяет категорию файла по его расширению."""
    for category, extensions in FILE_CATEGORIES.items():
        if extension.lower() in extensions:
            return category
    return 'other'

def get_file_hash(filepath, block_size=65536):
    """Вычисляет MD5 хеш файла блоками для экономии памяти."""
    hash_func = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            for block in iter(lambda: f.read(block_size), b''):
                hash_func.update(block)
        return hash_func.hexdigest()
    except (IOError, OSError):
        return None

def format_size(size_bytes):
    """Конвертирует размер в байтах в человеко-читаемый формат."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0

def scan_directory(directory):
    """Рекурсивно сканирует директорию и собирает статистику."""
    all_files = []
    stats = {
        'total_files': 0,
        'total_size': 0,
        'by_extension': defaultdict(lambda: {'count': 0, 'size': 0}),
        'by_directory': defaultdict(lambda: {'count': 0, 'size': 0}),
        'by_category': defaultdict(lambda: {'count': 0, 'size': 0})
    }

    for root, dirs, files in os.walk(directory):
        for file in files:
            full_path = os.path.join(root, file)
            try:
                file_size = os.path.getsize(full_path)
                mtime = os.path.getmtime(full_path)
            except OSError:
                continue

            stats['total_files'] += 1
            stats['total_size'] += file_size

            ext = os.path.splitext(file)[1].lower()
            stats['by_extension'][ext]['count'] += 1
            stats['by_extension'][ext]['size'] += file_size
            
            # Классификация файлов по категориям
            category = get_file_category(ext)
            stats['by_category'][category]['count'] += 1
            stats['by_category'][category]['size'] += file_size

            stats['by_directory'][root]['count'] += 1
            stats['by_directory'][root]['size'] += file_size

            all_files.append((full_path, file_size, mtime))

    return all_files, stats

class HashCache:
    """Класс для кэширования вычисленных хешей."""
    def __init__(self, cache_file="hash_cache.pkl"):
        self.cache_file = cache_file
        self.cache = self.load_cache()
        
    def load_cache(self):
        """Загружает кэш из файла."""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    return pickle.load(f)
            except:
                return {}
        return {}
    
    def save_cache(self):
        """Сохраняет кэш в файл."""
        with open(self.cache_file, 'wb') as f:
            pickle.dump(self.cache, f)
    
    def get(self, file_path, file_size, mtime):
        """Получает хеш из кэша, если он актуален."""
        if file_path in self.cache:
            cached_data = self.cache[file_path]
            if (cached_data['size'] == file_size and 
                cached_data['mtime'] == mtime):
                return cached_data['hash']
        return None
    
    def set(self, file_path, file_size, mtime, file_hash):
        """Сохраняет хеш в кэш."""
        self.cache[file_path] = {
            'size': file_size,
            'mtime': mtime,
            'hash': file_hash
        }

def find_duplicates_parallel(file_list, max_workers=8, cache_file=None):
    """Находит дубликаты используя многопоточность и кэширование."""
    print("\n[+] Группировка файлов по размеру...")
    
    # Инициализируем кэш
    cache = HashCache(cache_file) if cache_file else None
    
    # Группируем файлы по размеру (файлы разного размера не могут быть дубликатами)
    size_groups = defaultdict(list)
    for file_path, file_size, mtime in file_list:
        size_groups[file_size].append((file_path, mtime))
    
    # Оставляем только группы с потенциальными дубликатами (2+ файла одинакового размера)
    candidate_groups = {size: paths for size, paths in size_groups.items() if len(paths) > 1}
    total_candidates = sum(len(paths) for paths in candidate_groups.values())
    
    print(f"[+] Найдено {len(candidate_groups)} групп кандидатов в дубликаты ({total_candidates} файлов)")
    print("[+] Вычисление хешей для поиска дубликатов...")
    
    hashes_map = defaultdict(list)
    processed = 0
    from_cache = 0
    start_time = time.time()
    
    # Обрабатываем каждую группу кандидатов
    for size, file_list in candidate_groups.items():
        for file_path, mtime in file_list:
            file_size = size
            file_hash = None
            
            # Пытаемся получить хеш из кэша
            if cache:
                file_hash = cache.get(file_path, file_size, mtime)
                if file_hash:
                    from_cache += 1
                    hashes_map[file_hash].append(file_path)
                    processed += 1
                    continue
            
            # Если нет в кэше, вычисляем
            if file_hash is None:
                file_hash = get_file_hash(file_path)
                if file_hash is not None:
                    # Сохраняем в кэш
                    if cache:
                        cache.set(file_path, file_size, mtime, file_hash)
                    hashes_map[file_hash].append(file_path)
            
            processed += 1
            if processed % 100 == 0:  # Выводим прогресс каждые 100 файлов
                elapsed = time.time() - start_time
                files_per_sec = processed / elapsed if elapsed > 0 else 0
                print(f"Обработано: {processed}/{total_candidates} файлов ({files_per_sec:.1f} файл/сек)", end="\r")
    
    # Сохраняем кэш
    if cache:
        cache.save_cache()
        print(f"\n[+] Использовано хешей из кэша: {from_cache}")
    
    print(f"\n[+] Обработка завершена за {time.time() - start_time:.1f} секунд")
    
    # Оставляем только настоящие дубликаты (2+ файла с одинаковым хешем)
    duplicates = {h: paths for h, paths in hashes_map.items() if len(paths) > 1}
    return duplicates

def find_identical_directories(statistics, duplicates):
    """
    Находит каталоги, которые содержат одинаковые наборы файлов (дубликаты) в том же количестве.
    
    Args:
        statistics: Статистика, собранная функцией scan_directory
        duplicates: Словарь с найденными дубликатами файлов
        
    Returns:
        Список групп идентичных каталогов
    """
    print("\n[+] Поиск идентичных каталогов...")
    
    # Создаем обратный индекс: для каждого каталога определяем его "подпись"
    # Подпись каталога - это отсортированный список хешей файлов в нем
    dir_signatures = {}
    
    # Сначала создаем словарь для хранения хешей файлов в каждом каталоге
    dir_hashes = defaultdict(list)
    
    # Заполняем dir_hashes на основе информации о дубликатах
    for file_hash, file_paths in duplicates.items():
        for file_path in file_paths:
            dir_path = os.path.dirname(file_path)
            dir_hashes[dir_path].append(file_hash)
    
    # Создаем подписи для каждого каталога
    for dir_path, hashes in dir_hashes.items():
        # Сортируем хеши для создания уникальной подписи каталога
        dir_signatures[dir_path] = tuple(sorted(hashes))
    
    # Группируем каталоги по их подписям
    signature_groups = defaultdict(list)
    for dir_path, signature in dir_signatures.items():
        signature_groups[signature].append(dir_path)
    
    # Оставляем только группы с более чем одним каталогом
    identical_dirs = [dirs for dirs in signature_groups.values() if len(dirs) > 1]
    
    return identical_dirs

def print_section(title):
    """Печатает заголовок секции с разделителями."""
    print("\n" + "="*60)
    print(title)
    print("="*60)

def print_duplicates_by_category(duplicates):
    """Группирует и выводит дубликаты по категориям."""
    if not duplicates:
        return
    
    # Группируем дубликаты по категориям
    duplicates_by_category = defaultdict(list)
    for file_hash, file_paths in duplicates.items():
        # Определяем категорию первого файла в группе (все файлы в группе одинаковые)
        first_file = file_paths[0]
        ext = os.path.splitext(first_file)[1].lower()
        category = get_file_category(ext)
        duplicates_by_category[category].append((file_hash, file_paths))
    
    # Выводим дубликаты по категориям
    for category, dup_list in duplicates_by_category.items():
        print_section(f"ДУБЛИКАТЫ В КАТЕГОРИИ: {category.upper()}")
        
        total_size = 0
        for i, (file_hash, file_paths) in enumerate(dup_list, 1):
            size = os.path.getsize(file_paths[0])
            total_size += size * (len(file_paths) - 1)

            print(f"\nГруппа {i} (Хеш: {file_hash[:8]}...), Размер: {format_size(size)}")
            for path in file_paths:
                print(f"  -> {path}")
        
        print(f"\nОбщий объем дубликатов в категории {category}: {format_size(total_size)}")

def interactive_selection(duplicates, identical_dirs, statistics, auto_select_first=False):
    """
    Интерактивный режим для выбора файлов и каталогов для удаления.
    Если auto_select_first=True, автоматически выбирает "оставить первую копию" для всех групп.
    """
    files_to_delete = []
    dirs_to_delete = []
    
    print("\n" + "="*60)
    print("ИНТЕРАКТИВНЫЙ РЕЖИМ УПРАВЛЕНИЯ ДУБЛИКАТАМИ")
    print("="*60)
    
    # Обработка дубликатов файлов
    if duplicates:
        print("\n[+] ОБРАБОТКА ДУБЛИКАТОВ ФАЙЛОВ")
        for i, (file_hash, file_paths) in enumerate(duplicates.items(), 1):
            size = os.path.getsize(file_paths[0])
            ext = os.path.splitext(file_paths[0])[1].lower()
            category = get_file_category(ext)
            
            print(f"\nГруппа {i} (Хеш: {file_hash[:8]}...), Размер: {format_size(size)}, Категория: {category}")
            
            for j, path in enumerate(file_paths, 1):
                print(f"  [{j}] {path}")
            
            if auto_select_first:
                # Автоматически выбираем "оставить первую копию"
                files_to_delete.extend(file_paths[1:])
                print("  Автоматически выбрано: удалить все копии, кроме первой")
                continue
            
            print("  [s] Пропустить эту группу")
            print("  [a] Удалить все копии, кроме первой")
            print("  [b] Удалить все копии, кроме последней")
            print("  [m] Выбрать вручную")
            print("  [A] Применить 'удалить все копии, кроме первой' для всех оставшихся групп")
            
            choice = input("\nВаш выбор: ").strip().lower()
            
            if choice == 's':
                continue
            elif choice == 'a':
                # Оставляем первую копию, остальные удаляем
                files_to_delete.extend(file_paths[1:])
                print(f"Добавлено для удаления: {len(file_paths) - 1} файлов")
            elif choice == 'b':
                # Оставляем последнюю копию, остальные удаляем
                files_to_delete.extend(file_paths[:-1])
                print(f"Добавлено для удаления: {len(file_paths) - 1} файлов")
            elif choice == 'm':
                # Ручной выбор
                keep = input("Введите номера файлов, которые нужно сохранить (через пробел): ").split()
                keep_indices = [int(idx) - 1 for idx in keep if idx.isdigit()]
                
                for idx, path in enumerate(file_paths):
                    if idx not in keep_indices:
                        files_to_delete.append(path)
                print(f"Добавлено для удаления: {len(file_paths) - len(keep_indices)} файлов")
            elif choice == 'a':
                # Применить правило для всех оставшихся групп
                files_to_delete.extend(file_paths[1:])
                print(f"Добавлено для удаления: {len(file_paths) - 1} файлов")
                print("Применяется правило 'удалить все копии, кроме первой' для всех оставшихся групп")
                auto_select_first = True
            else:
                print("Неверный выбор, пропускаем группу")
    
    # Обработка идентичных каталогов
    if identical_dirs:
        print("\n[+] ОБРАБОТКА ИДЕНТИЧНЫХ КАТАЛОГОВ")
        for i, dir_group in enumerate(identical_dirs, 1):
            print(f"\nГруппа идентичных каталогов #{i}:")
            for j, dir_path in enumerate(dir_group, 1):
                dir_stats = statistics['by_directory'][dir_path]
                print(f"  [{j}] {dir_path} ({dir_stats['count']} файлов, {format_size(dir_stats['size'])})")
            
            print("  [s] Пропустить эту группу")
            print("  [a] Удалить все каталоги, кроме первого")
            print("  [b] Удалить все каталоги, кроме последнего")
            print("  [m] Выбрать вручную")
            
            choice = input("\nВаш выбор: ").strip().lower()
            
            if choice == 's':
                continue
            elif choice == 'a':
                # Оставляем первый каталог, остальные удаляем
                dirs_to_delete.extend(dir_group[1:])
                print(f"Добавлено для удаления: {len(dir_group) - 1} каталогов")
            elif choice == 'b':
                # Оставляем последний каталог, остальные удаляем
                dirs_to_delete.extend(dir_group[:-1])
                print(f"Добавлено для удаления: {len(dir_group) - 1} каталогов")
            elif choice == 'm':
                # Ручной выбор
                keep = input("Введите номера каталогов, которые нужно сохранить (через пробел): ").split()
                keep_indices = [int(idx) - 1 for idx in keep if idx.isdigit()]
                
                for idx, dir_path in enumerate(dir_group):
                    if idx not in keep_indices:
                        dirs_to_delete.append(dir_path)
                print(f"Добавлено для удаления: {len(dir_group) - len(keep_indices)} каталогов")
            else:
                print("Неверный выбор, пропускаем группу")
    
    return files_to_delete, dirs_to_delete

def auto_select_first_copy(duplicates, identical_dirs):
    """
    Автоматически выбирает правило 'удалить все копии, кроме первой' для всех групп.
    """
    files_to_delete = []
    dirs_to_delete = []
    
    # Обработка дубликатов файлов
    for file_hash, file_paths in duplicates.items():
        # Оставляем первую копию, остальные удаляем
        files_to_delete.extend(file_paths[1:])
    
    # Обработка идентичных каталогов
    for dir_group in identical_dirs:
        # Оставляем первый каталог, остальные удаляем
        dirs_to_delete.extend(dir_group[1:])
    
    return files_to_delete, dirs_to_delete

def preview_deletion(files_to_delete, dirs_to_delete, statistics):
    """Показывает предварительный просмотр того, что будет удалено."""
    print("\n" + "="*60)
    print("ПРЕДВАРИТЕЛЬНЫЙ ПРОСМОТР УДАЛЕНИЯ")
    print("="*60)
    
    total_size_saved = 0
    
    if files_to_delete:
        print(f"\nФайлы для удаления ({len(files_to_delete)}):")
        total_file_size = sum(os.path.getsize(f) for f in files_to_delete)
        total_size_saved += total_file_size
        
        for file in files_to_delete[:10]:  # Показываем только первые 10
            print(f"  - {file}")
        if len(files_to_delete) > 10:
            print(f"  ... и еще {len(files_to_delete) - 10} файлов")
        print(f"Общий объем файлов для удаления: {format_size(total_file_size)}")
    
    if dirs_to_delete:
        print(f"\nКаталоги для удаления ({len(dirs_to_delete)}):")
        total_dir_size = 0
        for dir_path in dirs_to_delete:
            if dir_path in statistics['by_directory']:
                dir_stats = statistics['by_directory'][dir_path]
                total_dir_size += dir_stats['size']
                print(f"  - {dir_path} ({dir_stats['count']} файлов, {format_size(dir_stats['size'])})")
            else:
                print(f"  - {dir_path} (статистика недоступна)")
        
        total_size_saved += total_dir_size
        print(f"Общий объем каталогов для удаления: {format_size(total_dir_size)}")
    
    print(f"\nОбщий объем, который будет освобожден: {format_size(total_size_saved)}")
    
    if not files_to_delete and not dirs_to_delete:
        print("Нет объектов для удаления.")
        return False
    
    return True

def execute_deletion(files_to_delete, dirs_to_delete, dry_run=False):
    """Выполняет или имитирует удаление выбранных файлов и каталогов."""
    print("\n" + "="*60)
    print("ВЫПОЛНЕНИЕ УДАЛЕНИЯ" if not dry_run else "ТЕСТОВЫЙ РЕЖИМ УДАЛЕНИЯ")
    print("="*60)
    
    deleted_files = 0
    deleted_dirs = 0
    freed_space = 0
    
    # Удаляем файлы
    for file_path in files_to_delete:
        try:
            file_size = os.path.getsize(file_path)
            if not dry_run:
                os.remove(file_path)
                print(f"Удален файл: {file_path} ({format_size(file_size)})")
            else:
                print(f"[ТЕСТ] Будет удален файл: {file_path} ({format_size(file_size)})")
            deleted_files += 1
            freed_space += file_size
        except Exception as e:
            print(f"Ошибка при удалении файла {file_path}: {e}")
    
    # Удаляем каталоги (только пустые или после удаления файлов)
    for dir_path in dirs_to_delete:
        try:
            # Сначала пытаемся получить размер каталога
            dir_size = 0
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    try:
                        dir_size += os.path.getsize(os.path.join(root, file))
                    except:
                        pass
            
            if not dry_run:
                shutil.rmtree(dir_path)
                print(f"Удален каталог: {dir_path} ({format_size(dir_size)})")
            else:
                print(f"[ТЕСТ] Будет удален каталог: {dir_path} ({format_size(dir_size)})")
            deleted_dirs += 1
            freed_space += dir_size
        except Exception as e:
            print(f"Ошибка при удалении каталога {dir_path}: {e}")
    
    print(f"\nИтого удалено: {deleted_files} файлов и {deleted_dirs} каталогов")
    print(f"Освобождено места: {format_size(freed_space)}")

def main():
    parser = argparse.ArgumentParser(description='Инструмент для анализа и поиска дубликатов медиафайлов.')
    parser.add_argument('source_dir', help='Путь к корневой директории для анализа')
    parser.add_argument('--workers', type=int, default=8, help='Количество потоков для обработки (по умолчанию: 8)')
    parser.add_argument('--cache-file', default='hash_cache.pkl', help='Файл для кэширования хешей (по умолчанию: hash_cache.pkl)')
    parser.add_argument('--group-by-category', action='store_true', help='Группировать дубликаты по категориям')
    parser.add_argument('--find-identical-dirs', action='store_true', help='Найти идентичные каталоги')
    parser.add_argument('--interactive', action='store_true', help='Запустить интерактивный режим выбора для удаления')
    parser.add_argument('--dry-run', action='store_true', help='Тестовый режим (показать что будет удалено, но не удалять)')
    parser.add_argument('--auto-first', action='store_true', help='Автоматически применить "удалить все копии, кроме первой" для всех групп')
    
    args = parser.parse_args()

    if not os.path.isdir(args.source_dir):
        print(f"Ошибка: Директория '{args.source_dir}' не существует или недоступна")
        return

    # Шаг 1: Сканирование и сбор статистики
    print(f"\n[+] Начинаем сканирование директории: {args.source_dir}")
    all_files, statistics = scan_directory(args.source_dir)

    # Шаг 2: Вывод сводной информации
    print_section("СВОДНАя СТАТИСТИКА")
    print(f"Общее количество файлов: {statistics['total_files']}")
    print(f"Общий объем данных: {format_size(statistics['total_size'])}")

    print_section("РАСПРЕДЕЛЕНИЕ ПО КАТЕГОРИЯМ")
    for category, data in sorted(statistics['by_category'].items()):
        percentage = (data['count'] / statistics['total_files']) * 100 if statistics['total_files'] > 0 else 0
        print(f"* {category.upper():<12}: {data['count']:>6} файлов ({percentage:.1f}%), {format_size(data['size']):>10}")

    print_section("ПО ФОРМАТАМ ФАЙЛОВ (ТОП-15)")
    extensions_sorted = sorted(statistics['by_extension'].items(), 
                              key=lambda x: x[1]['size'], reverse=True)[:15]
    for ext, data in extensions_sorted:
        print(f"* {ext if ext else 'No Extension':<8}: {data['count']:>6} файлов, {format_size(data['size']):>10}")

    print_section("ПО КАТАЛОГАМ (ТОП-10 ПО РАЗМЕРУ)")
    sorted_dirs = sorted(statistics['by_directory'].items(), 
                        key=lambda x: x[1]['size'], reverse=True)[:10]
    for dir_path, data in sorted_dirs:
        print(f"* {dir_path}: {data['count']} файлов, {format_size(data['size'])}")

    # Шаг 3: Поиск дубликатов с использованием многопоточности и кэширования
    duplicates = find_duplicates_parallel(all_files, args.workers, args.cache_file)
    print(f"\n[+] Найдено групп дубликатов: {len(duplicates)}")

    # Шаг 4: Поиск идентичных каталогов
    identical_dirs = []
    if args.find_identical_dirs and duplicates:
        identical_dirs = find_identical_directories(statistics, duplicates)
        print(f"[+] Найдено групп идентичных каталогов: {len(identical_dirs)}")

    # Шаг 5: Вывод информации о дубликатах
    if duplicates:
        total_duplicate_size = 0
        for file_hash, file_paths in duplicates.items():
            size = os.path.getsize(file_paths[0])
            total_duplicate_size += size * (len(file_paths) - 1)

        print_section("ОБЩАЯ СТАТИСТИКА ДУБЛИКАТОВ")
        print(f"Общий объем, занимаемый дубликатами: {format_size(total_duplicate_size)}")
        
        # Вывод дубликатов с группировкой по категориям или без
        if args.group_by_category:
            print_duplicates_by_category(duplicates)
        else:
            print_section("ВСЕ НАЙДЕННЫЕ ДУБЛИКАТЫ")
            for i, (file_hash, file_paths) in enumerate(duplicates.items(), 1):
                size = os.path.getsize(file_paths[0])
                ext = os.path.splitext(file_paths[0])[1].lower()
                category = get_file_category(ext)
                
                print(f"\nГруппа {i} (Хеш: {file_hash[:8]}...), Размер: {format_size(size)}, Категория: {category}")
                for path in file_paths:
                    print(f"  -> {path}")
    else:
        print("\n[+] Дубликаты не найдены.")

    # Шаг 6: Интерактивный режим или автоматическое удаление
    if (args.interactive or args.auto_first) and (duplicates or identical_dirs):
        if args.auto_first:
            # Автоматический режим - применяем правило для всех групп
            files_to_delete, dirs_to_delete = auto_select_first_copy(duplicates, identical_dirs)
            print("\n[+] Автоматически применено правило 'удалить все копии, кроме первой' для всех групп")
        else:
            # Интерактивный режим
            files_to_delete, dirs_to_delete = interactive_selection(duplicates, identical_dirs, statistics)
        
        if preview_deletion(files_to_delete, dirs_to_delete, statistics):
            if args.dry_run:
                execute_deletion(files_to_delete, dirs_to_delete, True)
            else:
                confirm = input("\nПодтвердите удаление (y/n): ").strip().lower()
                if confirm == 'y':
                    execute_deletion(files_to_delete, dirs_to_delete, False)
                    print("\n[+] Удаление завершено!")
                else:
                    print("Удаление отменено.")
    elif duplicates or identical_dirs:
        # Если не используется интерактивный режим, просто выводим рекомендации
        if duplicates:
            print(f"\nЗапустите с ключом --interactive для управления удалением дубликатов")
            print(f"Или используйте --auto-first для автоматического применения правила 'удалить все копии, кроме первой'")
        if identical_dirs:
            print(f"Запустите с ключом --interactive для управления удалением идентичных каталогов")

    print("\n[+] Анализ завершен.")

if __name__ == '__main__':
    main()
