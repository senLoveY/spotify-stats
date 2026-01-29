# Описание базы данных Snowflake

## База данных: `SPOTIFY_DB`

**Схема:** `PUBLIC`  
**Warehouse:** `COMPUTE_WH` (по умолчанию, настраивается через переменные окружения)

---

## 📊 ОБЗОР

База данных Snowflake используется для хранения и анализа данных о музыкальных предпочтениях пользователей, полученных из Spotify API. Система хранит информацию о треках, артистах, альбомах и истории прослушиваний для последующего анализа и персонализации рекомендаций.

---

## 📋 ТАБЛИЦЫ (Tables)

### 1. **TRACKS** - Треки

**Назначение:** Хранит метаданные о музыкальных треках, полученных из Spotify.

| Колонка | Тип данных | Описание | Ограничения |
|---------|-----------|----------|-------------|
| `TRACK_ID` | STRING | Spotify ID трека | PRIMARY KEY |
| `NAME` | STRING | Название трека | NOT NULL |
| `ARTIST` | STRING | Имя артиста (текстовое поле, дублирует ARTIST_ID) | Может быть NULL |
| `DURATION_MS` | INTEGER | Длительность трека в миллисекундах | Может быть NULL |
| `POPULARITY` | INTEGER | Популярность трека (0-100) | Может быть NULL |
| `GENRE` | STRING | Жанр трека (определяется по жанру артиста) | Может быть NULL |
| `IMAGE_URL` | STRING | URL изображения обложки трека/альбома | Может быть NULL |
| `ARTIST_ID` | STRING | Spotify ID артиста | FOREIGN KEY → ARTISTS.ARTIST_ID |
| `ALBUM_ID` | STRING | Spotify ID альбома | FOREIGN KEY → ALBUMS.ALBUM_ID |

**Особенности:**
- Таблица использует `MERGE INTO` для upsert операций (обновление при существовании, вставка при отсутствии)
- Колонки `GENRE` и `IMAGE_URL` добавляются динамически через `ALTER TABLE IF EXISTS`
- Колонки `ARTIST_ID` и `ALBUM_ID` добавляются скриптом `add_artist_id_to_tracks.py`
- Жанр определяется по первому артисту трека через Spotify API

**Индексы:** Неявный первичный ключ на `TRACK_ID`

**Связи:**
- Связана с `ARTISTS` через `ARTIST_ID`
- Связана с `ALBUMS` через `ALBUM_ID`
- Связана с `LISTENING_HISTORY` через `TRACK_ID` (один-ко-многим)

---

### 2. **ARTISTS** - Артисты

**Назначение:** Хранит информацию об артистах/исполнителях из Spotify.

| Колонка | Тип данных | Описание | Ограничения |
|---------|-----------|----------|-------------|
| `ARTIST_ID` | STRING | Spotify ID артиста | PRIMARY KEY |
| `NAME` | STRING | Имя артиста | NOT NULL |
| `POPULARITY` | INTEGER | Популярность артиста (0-100) | Может быть NULL |
| `FOLLOWERS` | INTEGER | Количество подписчиков артиста | Может быть NULL |
| `GENRES` | ARRAY | Массив жанров артиста | Может быть NULL (тип ARRAY) |
| `IMAGE_URL` | STRING | URL изображения артиста | Может быть NULL |

**Особенности:**
- Данные получаются через Spotify API
- Таблица может быть создана скриптом `add_artist_id_to_tracks.py`
- Жанры хранятся как массив (ARRAY) для поддержки множественных жанров
- Используется для рекомендаций артистов пользователям

**Индексы:** Неявный первичный ключ на `ARTIST_ID`

**Связи:**
- Связана с `TRACKS` через `ARTIST_ID` (один-ко-многим)
- Связана с `ALBUMS` через `ARTIST_ID` (один-ко-многим)

---

### 3. **ALBUMS** - Альбомы

**Назначение:** Хранит информацию об альбомах из Spotify.

| Колонка | Тип данных | Описание | Ограничения |
|---------|-----------|----------|-------------|
| `ALBUM_ID` | STRING | Spotify ID альбома | PRIMARY KEY |
| `NAME` | STRING | Название альбома | NOT NULL |
| `ARTIST_ID` | STRING | Spotify ID основного артиста | FOREIGN KEY → ARTISTS.ARTIST_ID |
| `RELEASE_DATE` | STRING | Дата выпуска альбома (формат: YYYY-MM-DD или YYYY) | Может быть NULL |
| `TOTAL_TRACKS` | INTEGER | Общее количество треков в альбоме | Может быть NULL |
| `ALBUM_TYPE` | STRING | Тип альбома | Возможные значения: 'album', 'single', 'compilation' |
| `IMAGE_URL` | STRING | URL обложки альбома | Может быть NULL |

**Особенности:**
- Таблица создается автоматически скриптом `add_artist_id_to_tracks.py`, если её нет
- Данные получаются через Spotify API при обработке треков
- Связывает треки с альбомами для более детального анализа

**Индексы:** Неявный первичный ключ на `ALBUM_ID`

**Связи:**
- Связана с `ARTISTS` через `ARTIST_ID` (многие-к-одному)
- Связана с `TRACKS` через `ALBUM_ID` (один-ко-многим)

---

### 4. **LISTENING_HISTORY** - История прослушиваний

**Назначение:** Хранит историю прослушиваний треков пользователями. Каждая запись представляет один факт прослушивания.

| Колонка | Тип данных | Описание | Ограничения |
|---------|-----------|----------|-------------|
| `USER_SPOTIFY_ID` | STRING | Spotify ID пользователя | NOT NULL, часть составного ключа |
| `TRACK_ID` | STRING | Spotify ID трека | NOT NULL, FOREIGN KEY → TRACKS.TRACK_ID, часть составного ключа |
| `PLAYED_AT` | TIMESTAMP_NTZ | Время и дата прослушивания | NOT NULL, часть составного ключа |

**Особенности:**
- Таблица фактов (fact table) для аналитики
- Составной ключ: `(USER_SPOTIFY_ID, TRACK_ID, PLAYED_AT)` - один трек может быть прослушан пользователем несколько раз
- Используется для всех аналитических запросов:
  - Топ треков по количеству прослушиваний
  - Распределение по жанрам
  - Активность по часам дня
  - Активность по дням недели
  - Рекомендации артистов

**Индексы:** Составной первичный ключ на `(USER_SPOTIFY_ID, TRACK_ID, PLAYED_AT)`

**Связи:**
- Связана с `TRACKS` через `TRACK_ID` (многие-к-одному)

**Использование:**
- Анализ музыкальных предпочтений пользователей
- Генерация рекомендаций
- Построение графиков и статистики
- Определение "вайба" пользователя

---

## 🔗 СВЯЗИ МЕЖДУ ТАБЛИЦАМИ (ER-диаграмма)

```
ARTISTS (ARTIST_ID) ──┐
                      ├─→ TRACKS (ARTIST_ID, ALBUM_ID)
ALBUMS (ALBUM_ID) ────┤
                      │
TRACKS (TRACK_ID) ────┼→ LISTENING_HISTORY (TRACK_ID)
                      │
                      └─→ USER_SPOTIFY_ID (внешний ключ к системе пользователей)
```

**Описание связей:**
- **ARTISTS → TRACKS**: Один артист может иметь множество треков (1:N)
- **ALBUMS → TRACKS**: Один альбом содержит множество треков (1:N)
- **ARTISTS → ALBUMS**: Один артист может иметь множество альбомов (1:N)
- **TRACKS → LISTENING_HISTORY**: Один трек может быть прослушан множество раз разными пользователями (1:N)

---

## 📈 АНАЛИТИЧЕСКИЕ ЗАПРОСЫ

### 1. Топ треков пользователя
```sql
SELECT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, COUNT(*) as play_count
FROM LISTENING_HISTORY lh
JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
WHERE lh.USER_SPOTIFY_ID = ?
GROUP BY t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL
ORDER BY play_count DESC
LIMIT ?
```

### 2. Распределение по жанрам
```sql
SELECT t.GENRE AS genre, COUNT(*) AS play_count
FROM LISTENING_HISTORY lh
JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
WHERE lh.USER_SPOTIFY_ID = ?
  AND t.GENRE IS NOT NULL
  AND t.GENRE != 'Не определен'
GROUP BY genre
ORDER BY play_count DESC
LIMIT ?
```

### 3. Активность по часам дня
```sql
SELECT EXTRACT(HOUR FROM PLAYED_AT::TIMESTAMP_NTZ) as hour,
       COUNT(*) as play_count
FROM LISTENING_HISTORY
WHERE USER_SPOTIFY_ID = ?
GROUP BY hour
ORDER BY hour
```

### 4. Активность по дням недели
```sql
SELECT DAYOFWEEKISO(PLAYED_AT::TIMESTAMP_NTZ) AS dow,
       COUNT(*) AS play_count
FROM LISTENING_HISTORY
WHERE USER_SPOTIFY_ID = ?
GROUP BY dow
ORDER BY dow
```

### 5. Рекомендации артистов
```sql
-- Получение топ жанров пользователя
SELECT t.GENRE
FROM LISTENING_HISTORY lh
JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
WHERE lh.USER_SPOTIFY_ID = ?
  AND t.GENRE IS NOT NULL
GROUP BY t.GENRE
ORDER BY COUNT(*) DESC
LIMIT 3

-- Получение артистов по жанрам
SELECT a.ARTIST_ID, a.NAME, a.GENRES, a.POPULARITY, a.FOLLOWERS, a.IMAGE_URL
FROM ARTISTS a
WHERE a.GENRES IS NOT NULL
ORDER BY a.POPULARITY DESC
LIMIT 1000
```

---

## 🔄 ПРОЦЕССЫ ЗАПОЛНЕНИЯ ДАННЫХ

### 1. Сохранение треков (`save_tracks_to_snowflake`)
1. Получение данных из Spotify API (недавние прослушивания)
2. Для каждого трека:
   - Upsert в таблицу `TRACKS` (MERGE INTO)
   - Если есть access_token, получение жанра через API артиста
   - Сохранение изображения обложки
3. Вставка записей в `LISTENING_HISTORY` (проверка на дубликаты)

### 2. Обогащение данных (`add_artist_id_to_tracks.py`)
1. Добавление колонок `ARTIST_ID` и `ALBUM_ID` в `TRACKS`
2. Создание таблицы `ALBUMS` (если нет)
3. Для каждого трека без `ARTIST_ID`:
   - Поиск артиста в `ARTISTS` по имени
   - Если не найден - получение через Spotify API
   - Сохранение артиста в `ARTISTS`
   - Обновление `ARTIST_ID` в `TRACKS`
4. Для каждого трека без `ALBUM_ID`:
   - Получение альбома через Spotify API по `track_id`
   - Сохранение альбома в `ALBUMS`
   - Обновление `ALBUM_ID` в `TRACKS`

---

## ⚙️ ТЕХНИЧЕСКИЕ ОСОБЕННОСТИ

### Типы данных Snowflake
- **STRING** - строковые данные
- **INTEGER** - целые числа
- **ARRAY** - массивы (используется для GENRES в ARTISTS)
- **TIMESTAMP_NTZ** - временные метки без часового пояса

### Операции
- **MERGE INTO** - используется для upsert в таблице TRACKS
- **COALESCE** - для обновления только непустых значений
- **EXTRACT** и **DAYOFWEEKISO** - функции для временного анализа

### Производительность
- Использование JOIN для объединения таблиц в аналитических запросах
- Группировка и агрегация для статистики
- Ограничение результатов через LIMIT для оптимизации

---

## 📊 ИСПОЛЬЗОВАНИЕ В ПРИЛОЖЕНИИ

### Основные функции:
1. **`save_tracks_to_snowflake`** - сохранение истории прослушиваний
2. **`get_top_tracks_from_snowflake`** - получение топ треков пользователя
3. **`get_genre_distribution_from_snowflake`** - распределение по жанрам
4. **`get_recommended_artists_from_snowflake`** - рекомендации артистов
5. **`get_listening_activity_by_hour_from_snowflake`** - активность по часам
6. **`get_listening_activity_by_weekday_from_snowflake`** - активность по дням недели
7. **`generate_user_vibe`** - генерация "вайба" пользователя на основе жанров

### Цели использования:
- Персонализация интерфейса
- Генерация рекомендаций
- Визуализация музыкальных предпочтений
- Анализ активности пользователей
- Построение дашборда с графиками и статистикой

---

## 🔐 БЕЗОПАСНОСТЬ И ДОСТУП

**Конфигурация:**
- Данные для подключения хранятся в переменных окружения (`.env`)
- Используется аутентификация через user/password
- Warehouse настраивается через переменные окружения

**Переменные окружения:**
- `SNOWFLAKE_USER` - пользователь
- `SNOWFLAKE_PASSWORD` - пароль
- `SNOWFLAKE_ACCOUNT` - аккаунт Snowflake
- `SNOWFLAKE_WAREHOUSE` - warehouse (по умолчанию `COMPUTE_WH`)
- `SNOWFLAKE_DATABASE` - база данных (по умолчанию `SPOTIFY_DB`)
- `SNOWFLAKE_SCHEMA` - схема (по умолчанию `PUBLIC`)

---

## 📝 ПРИМЕЧАНИЯ

1. **Нормализация данных:**
   - Треки, артисты и альбомы хранятся в отдельных таблицах для избежания дублирования
   - Используются Spotify ID как первичные ключи для обеспечения уникальности

2. **Данные из Spotify API:**
   - Все данные синхронизируются с Spotify API
   - Жанры определяются по артистам, а не напрямую для треков
   - Изображения хранятся как URL, не сами файлы

3. **Масштабируемость:**
   - Snowflake обеспечивает автоматическое масштабирование
   - История прослушиваний может содержать миллионы записей
   - Использование составного ключа в LISTENING_HISTORY оптимизирует хранение

4. **Аналитика:**
   - Система оптимизирована для аналитических запросов
   - Использование JOIN для объединения данных
   - Агрегация на уровне базы данных для производительности

---

*Документация создана на основе анализа кода приложения*



