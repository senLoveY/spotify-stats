#!/usr/bin/env python3
"""
Скрипт для добавления колонки ARTIST_ID в таблицу TRACKS в Snowflake
и заполнения её данными из таблицы ARTISTS или через Spotify API
"""

import os
import sys
import time
import requests
import base64
from dotenv import load_dotenv
import snowflake.connector
from snowflake.connector.errors import ProgrammingError

# Загружаем переменные окружения
load_dotenv()

# Конфигурация Snowflake
SNOWFLAKE_CONFIG = {
    'user': os.getenv('SNOWFLAKE_USER'),
    'password': os.getenv('SNOWFLAKE_PASSWORD'),
    'account': os.getenv('SNOWFLAKE_ACCOUNT'),
    'warehouse': os.getenv('SNOWFLAKE_WAREHOUSE', 'COMPUTE_WH'),
    'database': os.getenv('SNOWFLAKE_DATABASE', 'SPOTIFY_DB'),
    'schema': os.getenv('SNOWFLAKE_SCHEMA', 'PUBLIC')
}

# Конфигурация Spotify API
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1/"

# Глобальная переменная для токена
spotify_access_token = None


def get_spotify_token():
    """Получает access token для Spotify API используя client credentials flow"""
    global spotify_access_token
    
    if spotify_access_token:
        return spotify_access_token
    
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("Ошибка: SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET должны быть установлены в .env файле")
        return None
    
    # Создаем базовую авторизацию
    client_creds = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    client_creds_b64 = base64.b64encode(client_creds.encode()).decode()
    
    headers = {
        'Authorization': f'Basic {client_creds_b64}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    data = {
        'grant_type': 'client_credentials'
    }
    
    try:
        response = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data)
        response.raise_for_status()
        
        token_data = response.json()
        spotify_access_token = token_data['access_token']
        print(f"✓ Токен Spotify получен (действителен {token_data.get('expires_in', 3600)} секунд)")
        return spotify_access_token
    except Exception as e:
        print(f"✗ Ошибка получения токена Spotify: {e}")
        return None


def get_artist_from_track_id(track_id):
    """Получает информацию об артисте через Spotify API по track_id (более точный способ)"""
    token = get_spotify_token()
    if not token:
        return None
    
    headers = {'Authorization': f'Bearer {token}'}
    
    try:
        # Получаем информацию о треке
        track_url = f"{SPOTIFY_API_BASE_URL}tracks/{track_id}"
        track_response = requests.get(track_url, headers=headers)
        track_response.raise_for_status()
        
        track_data = track_response.json()
        artists = track_data.get('artists', [])
        
        if not artists:
            return None
        
        # Берем первого артиста (как и в коде save_tracks_to_snowflake)
        artist = artists[0]
        artist_id = artist.get('id')
        
        if not artist_id:
            return None
        
        # Получаем полную информацию об артисте
        artist_url = f"{SPOTIFY_API_BASE_URL}artists/{artist_id}"
        artist_response = requests.get(artist_url, headers=headers)
        artist_response.raise_for_status()
        full_artist_data = artist_response.json()
        
        return {
            'artist_id': artist_id,
            'name': full_artist_data.get('name'),
            'popularity': full_artist_data.get('popularity'),
            'followers': full_artist_data.get('followers', {}).get('total'),
            'genres': full_artist_data.get('genres', []),
            'image_url': full_artist_data.get('images', [{}])[0].get('url') if full_artist_data.get('images') else None
        }
    except Exception as e:
        return None


def get_album_from_track_id(track_id):
    """Получает информацию об альбоме через Spotify API по track_id"""
    token = get_spotify_token()
    if not token:
        return None
    
    headers = {'Authorization': f'Bearer {token}'}
    
    try:
        # Получаем информацию о треке
        track_url = f"{SPOTIFY_API_BASE_URL}tracks/{track_id}"
        track_response = requests.get(track_url, headers=headers)
        track_response.raise_for_status()
        
        track_data = track_response.json()
        album = track_data.get('album')
        
        if not album:
            return None
        
        album_id = album.get('id')
        if not album_id:
            return None
        
        # Получаем полную информацию об альбоме
        album_url = f"{SPOTIFY_API_BASE_URL}albums/{album_id}"
        album_response = requests.get(album_url, headers=headers)
        album_response.raise_for_status()
        full_album_data = album_response.json()
        
        # Получаем ID первого артиста из альбома (для связи с ARTISTS)
        album_artists = full_album_data.get('artists', [])
        artist_id = album_artists[0].get('id') if album_artists else None
        
        return {
            'album_id': album_id,
            'name': full_album_data.get('name'),
            'artist_id': artist_id,
            'release_date': full_album_data.get('release_date'),
            'total_tracks': full_album_data.get('total_tracks'),
            'album_type': full_album_data.get('album_type'),  # album, single, compilation
            'image_url': full_album_data.get('images', [{}])[0].get('url') if full_album_data.get('images') else None
        }
    except Exception as e:
        return None


def get_artist_from_spotify_api(artist_name, track_id=None):
    """Получает информацию об артисте через Spotify API по имени или track_id"""
    token = get_spotify_token()
    if not token:
        return None
    
    headers = {'Authorization': f'Bearer {token}'}
    
    # Сначала пытаемся получить через track_id (более точно)
    if track_id:
        artist_data = get_artist_from_track_id(track_id)
        if artist_data:
            return artist_data
    
    try:
        # Ищем артиста по имени
        search_url = f"{SPOTIFY_API_BASE_URL}search"
        params = {
            'q': f'artist:{artist_name}',
            'type': 'artist',
            'limit': 1
        }
        
        response = requests.get(search_url, headers=headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        artists = data.get('artists', {}).get('items', [])
        
        if not artists:
            # Попробуем поиск без префикса "artist:"
            params['q'] = artist_name
            response = requests.get(search_url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            artists = data.get('artists', {}).get('items', [])
        
        if artists:
            artist = artists[0]
            artist_id = artist.get('id')
            
            # Получаем полную информацию об артисте
            artist_url = f"{SPOTIFY_API_BASE_URL}artists/{artist_id}"
            artist_response = requests.get(artist_url, headers=headers)
            artist_response.raise_for_status()
            full_artist_data = artist_response.json()
            
            return {
                'artist_id': artist_id,
                'name': full_artist_data.get('name'),
                'popularity': full_artist_data.get('popularity'),
                'followers': full_artist_data.get('followers', {}).get('total'),
                'genres': full_artist_data.get('genres', []),
                'image_url': full_artist_data.get('images', [{}])[0].get('url') if full_artist_data.get('images') else None
            }
        
        return None
    except Exception as e:
        print(f"  ⚠ Ошибка при поиске артиста '{artist_name}' через API: {e}")
        return None


def add_artist_id_column(conn, cur):
    """Добавляет колонку ARTIST_ID в таблицу TRACKS, если её еще нет"""
    try:
        # Проверяем, существует ли колонка
        cur.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_SCHEMA = 'PUBLIC' 
            AND TABLE_NAME = 'TRACKS' 
            AND COLUMN_NAME = 'ARTIST_ID'
        """)
        
        if cur.fetchone()[0] == 0:
            print("Добавляю колонку ARTIST_ID в таблицу TRACKS...")
            cur.execute("ALTER TABLE TRACKS ADD COLUMN ARTIST_ID STRING")
            conn.commit()
            print("✓ Колонка ARTIST_ID добавлена")
        else:
            print("✓ Колонка ARTIST_ID уже существует")
    except Exception as e:
        print(f"✗ Ошибка при добавлении колонки ARTIST_ID: {e}")
        raise


def get_artist_by_name_from_snowflake(cur, artist_name):
    """Ищет артиста в таблице ARTISTS по имени (без учета регистра)"""
    if not artist_name:
        return None
    
    try:
        # Ищем точное совпадение (без учета регистра)
        cur.execute("""
            SELECT ARTIST_ID, NAME, GENRES, POPULARITY, FOLLOWERS, IMAGE_URL
            FROM ARTISTS
            WHERE UPPER(TRIM(NAME)) = UPPER(TRIM(%s))
            LIMIT 1
        """, (artist_name,))
        
        result = cur.fetchone()
        if result:
            return {
                'artist_id': result[0],
                'name': result[1],
                'genres': result[2],
                'popularity': result[3],
                'followers': result[4],
                'image_url': result[5]
            }
        return None
    except Exception as e:
        print(f"  ⚠ Ошибка при поиске артиста в Snowflake: {e}")
        return None


def insert_or_update_artist_in_snowflake(conn, cur, artist_data):
    """Вставляет или обновляет артиста в таблице ARTISTS"""
    try:
        artist_id = artist_data['artist_id']
        name = artist_data['name']
        popularity = artist_data.get('popularity')
        followers = artist_data.get('followers')
        genres = artist_data.get('genres', [])
        image_url = artist_data.get('image_url')
        
        # Проверяем, существует ли артист
        cur.execute("SELECT COUNT(*) FROM ARTISTS WHERE ARTIST_ID = %s", (artist_id,))
        exists = cur.fetchone()[0] > 0
        
        if exists:
            # Обновляем существующего артиста
            cur.execute("""
                UPDATE ARTISTS
                SET NAME = %s,
                    POPULARITY = COALESCE(%s, POPULARITY),
                    FOLLOWERS = COALESCE(%s, FOLLOWERS),
                    GENRES = COALESCE(%s, GENRES),
                    IMAGE_URL = COALESCE(%s, IMAGE_URL)
                WHERE ARTIST_ID = %s
            """, (name, popularity, followers, genres, image_url, artist_id))
        else:
            # Вставляем нового артиста
            cur.execute("""
                INSERT INTO ARTISTS (ARTIST_ID, NAME, POPULARITY, FOLLOWERS, GENRES, IMAGE_URL)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (artist_id, name, popularity, followers, genres, image_url))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"  ⚠ Ошибка при вставке/обновлении артиста: {e}")
        conn.rollback()
        return False


def update_track_artist_id(conn, cur, track_id, artist_id):
    """Обновляет ARTIST_ID для трека"""
    try:
        cur.execute("""
            UPDATE TRACKS
            SET ARTIST_ID = %s
            WHERE TRACK_ID = %s
        """, (artist_id, track_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"  ⚠ Ошибка при обновлении ARTIST_ID для трека {track_id}: {e}")
        conn.rollback()
        return False


def create_albums_table(conn, cur):
    """Создает таблицу ALBUMS, если её нет"""
    try:
        # Проверяем, существует ли таблица
        cur.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = 'PUBLIC' 
            AND TABLE_NAME = 'ALBUMS'
        """)
        
        if cur.fetchone()[0] == 0:
            print("Создание таблицы ALBUMS...")
            cur.execute("""
                CREATE TABLE ALBUMS (
                    ALBUM_ID STRING PRIMARY KEY,
                    NAME STRING,
                    ARTIST_ID STRING,
                    RELEASE_DATE STRING,
                    TOTAL_TRACKS INTEGER,
                    ALBUM_TYPE STRING,
                    IMAGE_URL STRING
                )
            """)
            conn.commit()
            print("✓ Таблица ALBUMS создана")
        else:
            print("✓ Таблица ALBUMS уже существует")
    except Exception as e:
        print(f"✗ Ошибка при создании таблицы ALBUMS: {e}")
        raise


def add_album_id_column(conn, cur):
    """Добавляет колонку ALBUM_ID в таблицу TRACKS, если её еще нет"""
    try:
        # Проверяем, существует ли колонка
        cur.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_SCHEMA = 'PUBLIC' 
            AND TABLE_NAME = 'TRACKS' 
            AND COLUMN_NAME = 'ALBUM_ID'
        """)
        
        if cur.fetchone()[0] == 0:
            print("Добавляю колонку ALBUM_ID в таблицу TRACKS...")
            cur.execute("ALTER TABLE TRACKS ADD COLUMN ALBUM_ID STRING")
            conn.commit()
            print("✓ Колонка ALBUM_ID добавлена")
        else:
            print("✓ Колонка ALBUM_ID уже существует")
    except Exception as e:
        print(f"✗ Ошибка при добавлении колонки ALBUM_ID: {e}")
        raise


def get_album_by_id_from_snowflake(cur, album_id):
    """Ищет альбом в таблице ALBUMS по ID"""
    if not album_id:
        return None
    
    try:
        cur.execute("""
            SELECT ALBUM_ID, NAME, ARTIST_ID, RELEASE_DATE, TOTAL_TRACKS, ALBUM_TYPE, IMAGE_URL
            FROM ALBUMS
            WHERE ALBUM_ID = %s
            LIMIT 1
        """, (album_id,))
        
        result = cur.fetchone()
        if result:
            return {
                'album_id': result[0],
                'name': result[1],
                'artist_id': result[2],
                'release_date': result[3],
                'total_tracks': result[4],
                'album_type': result[5],
                'image_url': result[6]
            }
        return None
    except Exception as e:
        print(f"  ⚠ Ошибка при поиске альбома в Snowflake: {e}")
        return None


def insert_or_update_album_in_snowflake(conn, cur, album_data):
    """Вставляет или обновляет альбом в таблице ALBUMS"""
    try:
        album_id = album_data['album_id']
        name = album_data['name']
        artist_id = album_data.get('artist_id')
        release_date = album_data.get('release_date')
        total_tracks = album_data.get('total_tracks')
        album_type = album_data.get('album_type')
        image_url = album_data.get('image_url')
        
        # Проверяем, существует ли альбом
        cur.execute("SELECT COUNT(*) FROM ALBUMS WHERE ALBUM_ID = %s", (album_id,))
        exists = cur.fetchone()[0] > 0
        
        if exists:
            # Обновляем существующий альбом
            cur.execute("""
                UPDATE ALBUMS
                SET NAME = %s,
                    ARTIST_ID = COALESCE(%s, ARTIST_ID),
                    RELEASE_DATE = COALESCE(%s, RELEASE_DATE),
                    TOTAL_TRACKS = COALESCE(%s, TOTAL_TRACKS),
                    ALBUM_TYPE = COALESCE(%s, ALBUM_TYPE),
                    IMAGE_URL = COALESCE(%s, IMAGE_URL)
                WHERE ALBUM_ID = %s
            """, (name, artist_id, release_date, total_tracks, album_type, image_url, album_id))
        else:
            # Вставляем новый альбом
            cur.execute("""
                INSERT INTO ALBUMS (ALBUM_ID, NAME, ARTIST_ID, RELEASE_DATE, TOTAL_TRACKS, ALBUM_TYPE, IMAGE_URL)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (album_id, name, artist_id, release_date, total_tracks, album_type, image_url))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"  ⚠ Ошибка при вставке/обновлении альбома: {e}")
        conn.rollback()
        return False


def update_track_album_id(conn, cur, track_id, album_id):
    """Обновляет ALBUM_ID для трека"""
    try:
        cur.execute("""
            UPDATE TRACKS
            SET ALBUM_ID = %s
            WHERE TRACK_ID = %s
        """, (album_id, track_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"  ⚠ Ошибка при обновлении ALBUM_ID для трека {track_id}: {e}")
        conn.rollback()
        return False


def process_tracks():
    """Основная функция обработки треков"""
    print("=" * 60)
    print("Добавление ARTIST_ID и ALBUM_ID в таблицу TRACKS")
    print("=" * 60)
    print()
    
    # Подключаемся к Snowflake
    try:
        print("Подключение к Snowflake...")
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        print("✓ Подключение к Snowflake установлено")
        print()
    except Exception as e:
        print(f"✗ Ошибка подключения к Snowflake: {e}")
        sys.exit(1)
    
    try:
        # Создаем таблицу ALBUMS, если её нет
        create_albums_table(conn, cur)
        print()
        
        # Добавляем колонки ARTIST_ID и ALBUM_ID
        add_artist_id_column(conn, cur)
        add_album_id_column(conn, cur)
        print()
        
        # Получаем все треки, у которых еще нет ARTIST_ID или ALBUM_ID
        print("Получение списка треков без ARTIST_ID или ALBUM_ID...")
        cur.execute("""
            SELECT TRACK_ID, NAME, ARTIST
            FROM TRACKS
            WHERE ((ARTIST_ID IS NULL OR ARTIST_ID = '') OR (ALBUM_ID IS NULL OR ALBUM_ID = ''))
            AND ARTIST IS NOT NULL
            AND ARTIST != ''
        """)
        
        tracks = cur.fetchall()
        total_tracks = len(tracks)
        print(f"✓ Найдено {total_tracks} треков без ARTIST_ID или ALBUM_ID")
        print()
        
        if total_tracks == 0:
            print("Все треки уже имеют ARTIST_ID и ALBUM_ID!")
            return
        
        # Статистика
        stats = {
            'processed': 0,
            'artists_found_in_db': 0,
            'artists_found_via_api': 0,
            'artists_not_found': 0,
            'albums_found_in_db': 0,
            'albums_found_via_api': 0,
            'albums_not_found': 0,
            'errors': 0
        }
        
        # Обрабатываем каждый трек
        print("Обработка треков...")
        print("-" * 60)
        
        for idx, (track_id, track_name, artist_name) in enumerate(tracks, 1):
            try:
                print(f"[{idx}/{total_tracks}] Трек: {track_name} | Артист: {artist_name}")
                
                artist_id = None
                album_id = None
                
                # ========== ОБРАБОТКА АРТИСТА ==========
                # Проверяем, нужен ли ARTIST_ID
                cur.execute("SELECT ARTIST_ID FROM TRACKS WHERE TRACK_ID = %s", (track_id,))
                existing_artist_id = cur.fetchone()[0] if cur.rowcount > 0 else None
                
                if not existing_artist_id:
                    # Сначала ищем в таблице ARTISTS
                    artist_data = get_artist_by_name_from_snowflake(cur, artist_name)
                    
                    if artist_data:
                        # Найден в базе данных
                        artist_id = artist_data['artist_id']
                        print(f"  ✓ Артист найден в таблице ARTISTS: {artist_id}")
                        stats['artists_found_in_db'] += 1
                    else:
                        # Не найден в базе, ищем через API
                        print(f"  → Поиск артиста через Spotify API...")
                        artist_data = get_artist_from_spotify_api(artist_name, track_id)
                        
                        if artist_data:
                            artist_id = artist_data['artist_id']
                            
                            # Сохраняем артиста в таблицу ARTISTS
                            if insert_or_update_artist_in_snowflake(conn, cur, artist_data):
                                print(f"  ✓ Артист получен через API и сохранен: {artist_id}")
                                stats['artists_found_via_api'] += 1
                            else:
                                print(f"  ⚠ Артист получен через API, но ошибка при сохранении")
                                stats['errors'] += 1
                        else:
                            # Не найден нигде
                            print(f"  ✗ Артист не найден")
                            stats['artists_not_found'] += 1
                    
                    # Обновляем ARTIST_ID в таблице TRACKS
                    if artist_id and update_track_artist_id(conn, cur, track_id, artist_id):
                        pass  # Успешно обновлено
                    elif not artist_id:
                        pass  # Пропускаем, если не найден
                else:
                    artist_id = existing_artist_id
                    print(f"  → ARTIST_ID уже установлен: {artist_id}")
                
                # ========== ОБРАБОТКА АЛЬБОМА ==========
                # Проверяем, нужен ли ALBUM_ID
                cur.execute("SELECT ALBUM_ID FROM TRACKS WHERE TRACK_ID = %s", (track_id,))
                existing_album_id = cur.fetchone()[0] if cur.rowcount > 0 else None
                
                if not existing_album_id:
                    print(f"  → Поиск альбома через Spotify API...")
                    album_data = get_album_from_track_id(track_id)
                    
                    if album_data:
                        album_id = album_data['album_id']
                        
                        # Проверяем, есть ли альбом в базе
                        existing_album = get_album_by_id_from_snowflake(cur, album_id)
                        
                        if not existing_album:
                            # Сохраняем альбом в таблицу ALBUMS
                            if insert_or_update_album_in_snowflake(conn, cur, album_data):
                                print(f"  ✓ Альбом получен через API и сохранен: {album_id} ({album_data.get('name', 'N/A')})")
                                stats['albums_found_via_api'] += 1
                            else:
                                print(f"  ⚠ Альбом получен через API, но ошибка при сохранении")
                                stats['errors'] += 1
                        else:
                            print(f"  ✓ Альбом найден в таблице ALBUMS: {album_id} ({existing_album.get('name', 'N/A')})")
                            stats['albums_found_in_db'] += 1
                        
                        # Обновляем ALBUM_ID в таблице TRACKS
                        if album_id and update_track_album_id(conn, cur, track_id, album_id):
                            pass  # Успешно обновлено
                    else:
                        # Не найден
                        print(f"  ✗ Альбом не найден")
                        stats['albums_not_found'] += 1
                else:
                    album_id = existing_album_id
                    print(f"  → ALBUM_ID уже установлен: {album_id}")
                
                # Если обновлен хотя бы один ID, считаем трек обработанным
                if (artist_id or existing_artist_id) or (album_id or existing_album_id):
                    stats['processed'] += 1
                
                # Небольшая задержка, чтобы не превысить лимиты API
                time.sleep(0.1)
                
            except Exception as e:
                print(f"  ✗ Ошибка при обработке трека: {e}")
                import traceback
                traceback.print_exc()
                stats['errors'] += 1
                continue
            
            # Прогресс каждые 50 треков
            if idx % 50 == 0:
                print(f"\nПрогресс: {idx}/{total_tracks} треков обработано\n")
                print("-" * 60)
        
        # Итоговая статистика
        print()
        print("=" * 60)
        print("Итоговая статистика:")
        print("=" * 60)
        print(f"Всего обработано треков: {stats['processed']}")
        print()
        print("Артисты:")
        print(f"  - Найдено в базе данных (ARTISTS): {stats['artists_found_in_db']}")
        print(f"  - Найдено через Spotify API: {stats['artists_found_via_api']}")
        print(f"  - Не найдено: {stats['artists_not_found']}")
        print()
        print("Альбомы:")
        print(f"  - Найдено в базе данных (ALBUMS): {stats['albums_found_in_db']}")
        print(f"  - Найдено через Spotify API: {stats['albums_found_via_api']}")
        print(f"  - Не найдено: {stats['albums_not_found']}")
        print()
        print(f"  - Ошибок: {stats['errors']}")
        print("=" * 60)
        
    except Exception as e:
        print(f"✗ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cur.close()
        conn.close()
        print("\n✓ Соединение с Snowflake закрыто")


if __name__ == "__main__":
    try:
        process_tracks()
    except KeyboardInterrupt:
        print("\n\n⚠ Прервано пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Неожиданная ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)



