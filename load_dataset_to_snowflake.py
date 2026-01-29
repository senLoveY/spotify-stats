#!/usr/bin/env python3
"""
Скрипт для загрузки данных из CSV датасета в Snowflake.
Заполняет недостающие данные через Spotify API.
"""

import csv
import requests
import base64
import time
import random
import snowflake.connector
from typing import Dict, Optional, List
import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# Конфигурация Spotify API
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE_URL = "https://api.spotify.com/v1/"

# Конфигурация Snowflake
SNOWFLAKE_CONFIG = {
    'user': os.getenv('SNOWFLAKE_USER'),
    'password': os.getenv('SNOWFLAKE_PASSWORD'),
    'account': os.getenv('SNOWFLAKE_ACCOUNT'),
    'warehouse': os.getenv('SNOWFLAKE_WAREHOUSE', 'COMPUTE_WH'),
    'database': os.getenv('SNOWFLAKE_DATABASE', 'SPOTIFY_DB'),
    'schema': os.getenv('SNOWFLAKE_SCHEMA', 'PUBLIC')
}

# Кэш для токена и данных
access_token = None
artist_genre_cache = {}
track_image_cache = {}


def get_spotify_token():
    """Получает access token для Spotify API"""
    global access_token
    if access_token:
        return access_token
    
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    
    resp = requests.post(
        TOKEN_URL,
        data={'grant_type': 'client_credentials'},
        headers={'Authorization': f'Basic {b64_auth}'}
    )
    
    if resp.status_code == 200:
        access_token = resp.json()['access_token']
        return access_token
    else:
        print(f"Ошибка получения токена: {resp.status_code}")
        return None


def get_track_image_from_spotify(track_id: str) -> Optional[str]:
    """Получает изображение обложки трека из Spotify API"""
    if track_id in track_image_cache:
        return track_image_cache[track_id]
    
    token = get_spotify_token()
    if not token:
        return None
    
    try:
        headers = {'Authorization': f'Bearer {token}'}
        resp = requests.get(f"{API_BASE_URL}tracks/{track_id}", headers=headers, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            if data.get('album') and data['album'].get('images'):
                image_url = data['album']['images'][0].get('url')
                track_image_cache[track_id] = image_url
                return image_url
        elif resp.status_code == 429:
            # Rate limit - ждем
            print("Rate limit, ждем 5 секунд...")
            time.sleep(5)
            return get_track_image_from_spotify(track_id)
    except Exception as e:
        print(f"Ошибка получения изображения для {track_id}: {e}")
    
    track_image_cache[track_id] = None
    return None


def get_artist_genre_from_spotify(artist_name: str) -> Optional[str]:
    """Получает жанр артиста из Spotify API"""
    if artist_name in artist_genre_cache:
        return artist_genre_cache[artist_name]
    
    token = get_spotify_token()
    if not token:
        return None
    
    try:
        headers = {'Authorization': f'Bearer {token}'}
        # Ищем артиста по имени
        resp = requests.get(
            f"{API_BASE_URL}search",
            params={'q': artist_name, 'type': 'artist', 'limit': 1},
            headers=headers,
            timeout=5
        )
        
        if resp.status_code == 200:
            data = resp.json()
            artists = data.get('artists', {}).get('items', [])
            if artists:
                genres = artists[0].get('genres', [])
                genre = genres[0] if genres else None
                artist_genre_cache[artist_name] = genre
                return genre
        elif resp.status_code == 429:
            # Rate limit - ждем
            print("Rate limit, ждем 5 секунд...")
            time.sleep(5)
            return get_artist_genre_from_spotify(artist_name)
    except Exception as e:
        print(f"Ошибка получения жанра для {artist_name}: {e}")
    
    artist_genre_cache[artist_name] = None
    return None


def process_csv_row(row: Dict) -> Dict:
    """Обрабатывает строку CSV и возвращает данные для Snowflake"""
    track_id = row.get('track_id', '').strip()
    track_name = row.get('track_name', '').strip()
    artists_str = row.get('artists', '').strip()
    duration_ms = row.get('duration_ms', '')
    popularity = row.get('popularity', '')
    track_genre = row.get('track_genre', '').strip()
    
    # Обработка артистов (берем первого, если несколько)
    artist = None
    if artists_str:
        # Разделяем по запятой или точке с запятой
        artists_list = [a.strip() for a in artists_str.replace(';', ',').split(',')]
        artist = artists_list[0] if artists_list else None
    
    # Обработка числовых полей
    try:
        duration_ms = int(duration_ms) if duration_ms else None
    except:
        duration_ms = None
    
    try:
        popularity = int(popularity) if popularity else None
    except:
        popularity = None
    
    # Получаем жанр (приоритет: track_genre из CSV, если нет - из Spotify API)
    genre = track_genre if track_genre else None
    if not genre and artist:
        genre = get_artist_genre_from_spotify(artist)
        time.sleep(0.04)  # Небольшая задержка для избежания rate limit
    
    # Получаем изображение обложки из Spotify API
    image_url = None
    if track_id:
        image_url = get_track_image_from_spotify(track_id)
        time.sleep(0.1)  # Небольшая задержка для избежания rate limit
    
    return {
        'track_id': track_id,
        'name': track_name,
        'artist': artist,
        'duration_ms': duration_ms,
        'popularity': popularity,
        'genre': genre,
        'image_url': image_url
    }


def load_dataset_to_snowflake(csv_file: str, batch_size: int = 1000, start_from: int = 0, random_order: bool = True):
    """
    Загружает данные из CSV в Snowflake
    
    Args:
        csv_file: путь к CSV файлу
        batch_size: размер батча для загрузки
        start_from: с какой строки начать (для продолжения загрузки, игнорируется если random_order=True)
        random_order: загружать треки в случайном порядке
    """
    print(f"Начинаем загрузку данных из {csv_file}")
    print(f"Размер батча: {batch_size}")
    if random_order:
        print("Режим: случайный порядок загрузки")
    else:
        print(f"Начинаем с строки {start_from}")
    
    # Получаем токен Spotify
    token = get_spotify_token()
    if not token:
        print("Не удалось получить токен Spotify. Продолжаем без получения изображений.")
    
    # Подключаемся к Snowflake
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        
        # Гарантируем наличие колонок
        try:
            cur.execute("ALTER TABLE IF EXISTS TRACKS ADD COLUMN IF NOT EXISTS GENRE STRING")
            cur.execute("ALTER TABLE IF EXISTS TRACKS ADD COLUMN IF NOT EXISTS IMAGE_URL STRING")
        except Exception as e:
            print(f"Warning: Could not add columns: {e}")
        
        # Читаем все строки CSV
        print("Чтение CSV файла...")
        all_rows = []
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                all_rows.append(row)
        
        print(f"Прочитано {len(all_rows)} строк из CSV")
        
        # Перемешиваем строки, если включен случайный порядок
        if random_order:
            print("Перемешивание строк в случайном порядке...")
            random.shuffle(all_rows)
        
        # Пропускаем строки до start_from (если не случайный порядок)
        if not random_order and start_from > 0:
            all_rows = all_rows[start_from:]
            print(f"Пропущено {start_from} строк, осталось {len(all_rows)}")
        
        # Обрабатываем строки
        processed = 0
        skipped = 0
        batch = []
        
        for row_num, row in enumerate(all_rows):
            try:
                # Обрабатываем строку
                track_data = process_csv_row(row)
                
                # Пропускаем, если нет track_id
                if not track_data['track_id']:
                    skipped += 1
                    continue
                
                batch.append(track_data)
                
                # Загружаем батч
                if len(batch) >= batch_size:
                    save_batch_to_snowflake(cur, batch)
                    processed += len(batch)
                    print(f"Обработано: {processed} треков, пропущено: {skipped}")
                    batch = []
                    
                    # Коммитим каждые 10 батчей
                    if processed % (batch_size * 10) == 0:
                        conn.commit()
                        print(f"Коммит выполнен. Всего обработано: {processed}")
            
            except Exception as e:
                print(f"Ошибка обработки строки {row_num}: {e}")
                skipped += 1
                continue
        
        # Загружаем оставшиеся данные
        if batch:
            save_batch_to_snowflake(cur, batch)
            processed += len(batch)
        
        conn.commit()
        print(f"\nЗагрузка завершена!")
        print(f"Всего обработано: {processed} треков")
        print(f"Пропущено: {skipped} строк")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"Ошибка подключения к Snowflake: {e}")
        import traceback
        traceback.print_exc()


def save_batch_to_snowflake(cur, batch: list):
    """Сохраняет батч треков в Snowflake"""
    for track_data in batch:
        try:
            cur.execute("""
                MERGE INTO TRACKS t
                USING (SELECT %s AS ID,
                              %s AS NAME,
                              %s AS ARTIST,
                              %s AS DUR,
                              %s AS POP,
                              %s AS GENRE,
                              %s AS IMG) s
                ON t.TRACK_ID = s.ID
                WHEN NOT MATCHED THEN
                    INSERT (TRACK_ID, NAME, ARTIST, DURATION_MS, POPULARITY, GENRE, IMAGE_URL)
                    VALUES (s.ID, s.NAME, s.ARTIST, s.DUR, s.POP, s.GENRE, s.IMG)
                WHEN MATCHED THEN
                    UPDATE SET
                        NAME = COALESCE(s.NAME, t.NAME),
                        ARTIST = COALESCE(s.ARTIST, t.ARTIST),
                        DURATION_MS = COALESCE(s.DUR, t.DURATION_MS),
                        POPULARITY = COALESCE(s.POP, t.POPULARITY),
                        GENRE = COALESCE(s.GENRE, t.GENRE),
                        IMAGE_URL = COALESCE(s.IMG, t.IMAGE_URL)
            """, (
                track_data['track_id'],
                track_data['name'],
                track_data['artist'],
                track_data['duration_ms'],
                track_data['popularity'],
                track_data['genre'],
                track_data['image_url']
            ))
        except Exception as e:
            print(f"Ошибка сохранения трека {track_data['track_id']}: {e}")


if __name__ == "__main__":
    import sys
    
    csv_file = "dataset.csv"
    batch_size = 1000
    start_from = 0
    random_order = True  # По умолчанию случайный порядок
    
    # Парсим аргументы командной строки
    if len(sys.argv) > 1:
        csv_file = sys.argv[1]
    if len(sys.argv) > 2:
        batch_size = int(sys.argv[2])
    if len(sys.argv) > 3:
        start_from = int(sys.argv[3])
    if len(sys.argv) > 4:
        random_order = sys.argv[4].lower() in ('true', '1', 'yes', 'y')
    
    print("=" * 60)
    print("Загрузка датасета в Snowflake")
    print("=" * 60)
    print(f"Файл: {csv_file}")
    print(f"Размер батча: {batch_size}")
    if random_order:
        print("Порядок: случайный")
    else:
        print(f"Начать с строки: {start_from}")
    print("=" * 60)
    
    load_dataset_to_snowflake(csv_file, batch_size, start_from, random_order)

