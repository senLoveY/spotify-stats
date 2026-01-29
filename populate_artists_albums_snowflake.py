#!/usr/bin/env python3
"""
Скрипт для заполнения таблиц ARTISTS и ALBUMS в Snowflake данными из Spotify API.
Использует существующие данные из TRACKS для получения информации об артистах и альбомах.
"""

import requests
import base64
import time
import snowflake.connector
from typing import Dict, Optional, List
import os
from dotenv import load_dotenv

# Загружаем переменные окружения
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
spotify_token_cache = {'token': None, 'expires_at': 0}
artist_cache = {}
album_cache = {}


def get_spotify_token():
    """Получает или обновляет токен Spotify API"""
    current_time = time.time()
    if spotify_token_cache['token'] and spotify_token_cache['expires_at'] > current_time:
        return spotify_token_cache['token']

    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    
    try:
        resp = requests.post(
            TOKEN_URL,
            data={'grant_type': 'client_credentials'},
            headers={'Authorization': f'Basic {b64_auth}'},
            timeout=10
        )
        resp.raise_for_status()
        token_info = resp.json()
        
        spotify_token_cache['token'] = token_info['access_token']
        spotify_token_cache['expires_at'] = current_time + token_info['expires_in'] - 60
        return spotify_token_cache['token']
    except requests.exceptions.RequestException as e:
        print(f"Ошибка получения токена Spotify: {e}")
        return None


def get_artist_from_spotify(artist_id: str) -> Optional[Dict]:
    """Получает информацию об артисте из Spotify API"""
    if artist_id in artist_cache:
        return artist_cache[artist_id]
    
    token = get_spotify_token()
    if not token:
        return None
    
    try:
        headers = {'Authorization': f'Bearer {token}'}
        resp = requests.get(f"{API_BASE_URL}artists/{artist_id}", headers=headers, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            artist_info = {
                'artist_id': data['id'],
                'name': data['name'],
                'genres': data.get('genres', []),
                'popularity': data.get('popularity'),
                'followers': data.get('followers', {}).get('total'),
                'image_url': data['images'][0]['url'] if data.get('images') else None
            }
            artist_cache[artist_id] = artist_info
            return artist_info
        elif resp.status_code == 429:
            print("Rate limit, ждем 10 секунд...")
            time.sleep(10)
            return get_artist_from_spotify(artist_id)
    except Exception as e:
        print(f"Ошибка получения артиста {artist_id}: {e}")
    
    artist_cache[artist_id] = None
    return None


def get_album_from_spotify(album_id: str) -> Optional[Dict]:
    """Получает информацию об альбоме из Spotify API"""
    if album_id in album_cache:
        return album_cache[album_id]
    
    token = get_spotify_token()
    if not token:
        return None
    
    try:
        headers = {'Authorization': f'Bearer {token}'}
        resp = requests.get(f"{API_BASE_URL}albums/{album_id}", headers=headers, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            album_info = {
                'album_id': data['id'],
                'name': data['name'],
                'artist_id': data['artists'][0]['id'] if data.get('artists') else None,
                'release_date': data.get('release_date'),
                'total_tracks': data.get('total_tracks'),
                'popularity': data.get('popularity'),
                'image_url': data['images'][0]['url'] if data.get('images') else None,
                'genre': None  # Альбомы не имеют прямого жанра, можно взять из артиста
            }
            album_cache[album_id] = album_info
            return album_info
        elif resp.status_code == 429:
            print("Rate limit, ждем 10 секунд...")
            time.sleep(10)
            return get_album_from_spotify(album_id)
    except Exception as e:
        print(f"Ошибка получения альбома {album_id}: {e}")
    
    album_cache[album_id] = None
    return None


def get_artist_id_from_track(track_id: str, token: str) -> Optional[str]:
    """Получает ID артиста из информации о треке"""
    try:
        headers = {'Authorization': f'Bearer {token}'}
        resp = requests.get(f"{API_BASE_URL}tracks/{track_id}", headers=headers, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            if data.get('artists') and len(data['artists']) > 0:
                return data['artists'][0]['id']
        elif resp.status_code == 429:
            print("Rate limit, ждем 10 секунд...")
            time.sleep(10)
            return get_artist_id_from_track(track_id, token)
    except Exception as e:
        print(f"Ошибка получения артиста для трека {track_id}: {e}")
    
    return None


def get_album_id_from_track(track_id: str, token: str) -> Optional[str]:
    """Получает ID альбома из информации о треке"""
    try:
        headers = {'Authorization': f'Bearer {token}'}
        resp = requests.get(f"{API_BASE_URL}tracks/{track_id}", headers=headers, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            if data.get('album'):
                return data['album']['id']
        elif resp.status_code == 429:
            print("Rate limit, ждем 10 секунд...")
            time.sleep(10)
            return get_album_id_from_track(track_id, token)
    except Exception as e:
        print(f"Ошибка получения альбома для трека {track_id}: {e}")
    
    return None


def save_artist_to_snowflake(cur, artist_data: Dict):
    """Сохраняет артиста в Snowflake"""
    if not artist_data or not artist_data.get('artist_id'):
        return
    
    try:
        # Преобразуем массив жанров в список для Snowflake
        genres_list = artist_data.get('genres', [])
        
        # Используем ARRAY_CONSTRUCT для создания массива
        if genres_list:
            # Создаем массив из жанров
            genres_placeholders = ','.join(['%s'] * len(genres_list))
            cur.execute(f"""
                MERGE INTO ARTISTS a
                USING (SELECT %s AS ID,
                              %s AS NAME,
                              ARRAY_CONSTRUCT({genres_placeholders}) AS GENRES,
                              %s AS POP,
                              %s AS FOLLOWERS,
                              %s AS IMG) s
                ON a.ARTIST_ID = s.ID
                WHEN NOT MATCHED THEN
                    INSERT (ARTIST_ID, NAME, GENRES, POPULARITY, FOLLOWERS, IMAGE_URL)
                    VALUES (s.ID, s.NAME, s.GENRES, s.POP, s.FOLLOWERS, s.IMG)
                WHEN MATCHED THEN
                    UPDATE SET
                        NAME = s.NAME,
                        GENRES = s.GENRES,
                        POPULARITY = COALESCE(s.POP, a.POPULARITY),
                        FOLLOWERS = COALESCE(s.FOLLOWERS, a.FOLLOWERS),
                        IMAGE_URL = COALESCE(s.IMG, a.IMAGE_URL)
            """, (
                artist_data['artist_id'],
                artist_data['name'],
                *genres_list,
                artist_data.get('popularity'),
                artist_data.get('followers'),
                artist_data.get('image_url')
            ))
        else:
            # Если жанров нет, используем пустой массив
            cur.execute("""
                MERGE INTO ARTISTS a
                USING (SELECT %s AS ID,
                              %s AS NAME,
                              ARRAY_CONSTRUCT() AS GENRES,
                              %s AS POP,
                              %s AS FOLLOWERS,
                              %s AS IMG) s
                ON a.ARTIST_ID = s.ID
                WHEN NOT MATCHED THEN
                    INSERT (ARTIST_ID, NAME, GENRES, POPULARITY, FOLLOWERS, IMAGE_URL)
                    VALUES (s.ID, s.NAME, s.GENRES, s.POP, s.FOLLOWERS, s.IMG)
                WHEN MATCHED THEN
                    UPDATE SET
                        NAME = s.NAME,
                        GENRES = s.GENRES,
                        POPULARITY = COALESCE(s.POP, a.POPULARITY),
                        FOLLOWERS = COALESCE(s.FOLLOWERS, a.FOLLOWERS),
                        IMAGE_URL = COALESCE(s.IMG, a.IMAGE_URL)
            """, (
                artist_data['artist_id'],
                artist_data['name'],
                artist_data.get('popularity'),
                artist_data.get('followers'),
                artist_data.get('image_url')
            ))
    except Exception as e:
        print(f"Ошибка сохранения артиста {artist_data.get('artist_id')}: {e}")
        import traceback
        traceback.print_exc()


def save_album_to_snowflake(cur, album_data: Dict):
    """Сохраняет альбом в Snowflake"""
    if not album_data or not album_data.get('album_id'):
        return
    
    try:
        cur.execute("""
            MERGE INTO ALBUMS a
            USING (SELECT %s AS ID,
                          %s AS NAME,
                          %s AS ARTIST_ID,
                          %s AS RELEASE_DATE,
                          %s AS TOTAL_TRACKS,
                          %s AS POP,
                          %s AS IMG,
                          %s AS GENRE) s
            ON a.ALBUM_ID = s.ID
            WHEN NOT MATCHED THEN
                INSERT (ALBUM_ID, NAME, ARTIST_ID, RELEASE_DATE, TOTAL_TRACKS, POPULARITY, IMAGE_URL, GENRE)
                VALUES (s.ID, s.NAME, s.ARTIST_ID, s.RELEASE_DATE, s.TOTAL_TRACKS, s.POP, s.IMG, s.GENRE)
            WHEN MATCHED THEN
                UPDATE SET
                    NAME = s.NAME,
                    ARTIST_ID = COALESCE(s.ARTIST_ID, a.ARTIST_ID),
                    RELEASE_DATE = COALESCE(s.RELEASE_DATE, a.RELEASE_DATE),
                    TOTAL_TRACKS = COALESCE(s.TOTAL_TRACKS, a.TOTAL_TRACKS),
                    POPULARITY = COALESCE(s.POP, a.POPULARITY),
                    IMAGE_URL = COALESCE(s.IMG, a.IMAGE_URL),
                    GENRE = COALESCE(s.GENRE, a.GENRE)
        """, (
            album_data['album_id'],
            album_data['name'],
            album_data.get('artist_id'),
            album_data.get('release_date'),
            album_data.get('total_tracks'),
            album_data.get('popularity'),
            album_data.get('image_url'),
            album_data.get('genre')
        ))
    except Exception as e:
        print(f"Ошибка сохранения альбома {album_data.get('album_id')}: {e}")


def populate_artists_and_albums(limit: int = 1500, batch_size: int = 50):
    """
    Заполняет таблицы ARTISTS и ALBUMS данными из Spotify API
    на основе существующих треков в TRACKS
    
    Args:
        limit: Максимальное количество треков для обработки
        batch_size: Размер батча для обработки
    """
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        
        # Получаем уникальные треки из TRACKS
        print("Получение списка треков из Snowflake...")
        cur.execute("""
            SELECT DISTINCT TRACK_ID, ARTIST
            FROM TRACKS
            WHERE TRACK_ID IS NOT NULL
            LIMIT %s
        """, (limit,))
        
        tracks = cur.fetchall()
        print(f"Найдено {len(tracks)} треков для обработки")
        
        token = get_spotify_token()
        if not token:
            print("Ошибка: не удалось получить токен Spotify")
            return
        
        processed_artists = set()
        processed_albums = set()
        processed = 0
        errors = 0
        
        for i, (track_id, artist_name) in enumerate(tracks, 1):
            try:
                # Получаем ID артиста и альбома из Spotify API
                artist_id = get_artist_id_from_track(track_id, token)
                album_id = get_album_id_from_track(track_id, token)
                
                time.sleep(0.2)  # Задержка для избежания rate limit
                
                # Обрабатываем артиста
                if artist_id and artist_id not in processed_artists:
                    artist_data = get_artist_from_spotify(artist_id)
                    time.sleep(0.2)
                    
                    if artist_data:
                        save_artist_to_snowflake(cur, artist_data)
                        processed_artists.add(artist_id)
                        print(f"[{i}/{len(tracks)}] Артист сохранен: {artist_data['name']}")
                
                # Обрабатываем альбом
                if album_id and album_id not in processed_albums:
                    album_data = get_album_from_spotify(album_id)
                    time.sleep(0.2)
                    
                    if album_data:
                        # Если у альбома нет artist_id, пытаемся получить из артиста трека
                        if not album_data.get('artist_id') and artist_id:
                            album_data['artist_id'] = artist_id
                        
                        save_album_to_snowflake(cur, album_data)
                        processed_albums.add(album_id)
                        print(f"[{i}/{len(tracks)}] Альбом сохранен: {album_data['name']}")
                
                processed += 1
                
                # Коммитим каждые batch_size записей
                if processed % batch_size == 0:
                    conn.commit()
                    print(f"Коммит выполнен. Обработано: {processed} треков")
            
            except Exception as e:
                errors += 1
                print(f"Ошибка обработки трека {track_id}: {e}")
                continue
        
        # Финальный коммит
        conn.commit()
        
        print("\n" + "="*60)
        print("Заполнение завершено!")
        print(f"Обработано треков: {processed}")
        print(f"Уникальных артистов: {len(processed_artists)}")
        print(f"Уникальных альбомов: {len(processed_albums)}")
        print(f"Ошибок: {errors}")
        print("="*60)
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"Ошибка подключения к Snowflake: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import sys
    
    limit = 1500
    batch_size = 50
    
    if len(sys.argv) > 1:
        limit = int(sys.argv[1])
    if len(sys.argv) > 2:
        batch_size = int(sys.argv[2])
    
    print("="*60)
    print("Заполнение таблиц ARTISTS и ALBUMS в Snowflake")
    print("="*60)
    print(f"Лимит треков: {limit}")
    print(f"Размер батча: {batch_size}")
    print("="*60)
    print()
    
    populate_artists_and_albums(limit=limit, batch_size=batch_size)

