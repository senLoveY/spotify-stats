# Описание таблиц базы данных PostgreSQL

## База данных: `spotify_project`

---

## Таблица: **users** (Пользователи системы)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор пользователя |
| `username` | VARCHAR(100) | UNIQUE, NOT NULL | Логин пользователя (уникальный в системе) |
| `password_hash` | VARCHAR(255) | DEFAULT NULL | Хэш пароля пользователя (bcrypt). Может быть NULL для пользователей, зарегистрированных через Spotify |
| `email` | VARCHAR(255) | UNIQUE, DEFAULT NULL, CHECK (формат email через regex) | Email адрес пользователя. Уникальный, может быть NULL. Проверяется формат через регулярное выражение: `^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}\|localhost)$` |
| `spotify_id` | VARCHAR(100) | UNIQUE, DEFAULT NULL | Spotify ID пользователя (уникальный идентификатор из Spotify API). Может быть NULL для пользователей без привязки Spotify |
| `display_name` | VARCHAR(150) | DEFAULT NULL | Отображаемое имя пользователя (может отличаться от username) |
| `role` | VARCHAR(20) | DEFAULT 'user', CHECK (role IN ('user', 'admin')) | Роль пользователя в системе. Допустимые значения: 'user' (обычный пользователь) или 'admin' (администратор) |
| `is_blocked` | BOOLEAN | DEFAULT FALSE | Флаг блокировки пользователя. TRUE - пользователь заблокирован, FALSE - активен |
| `email_verified` | BOOLEAN | DEFAULT FALSE | Флаг подтверждения email адреса. TRUE - email подтвержден, FALSE - не подтвержден |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время регистрации пользователя в системе |

**Дополнительные ограничения:**
- `chk_users_role` - CHECK ограничение: роль может быть только 'user' или 'admin'
- `chk_users_email_format` - CHECK ограничение: проверка формата email через регулярное выражение

**Индексы:**
- Неявный PRIMARY KEY на `id`
- UNIQUE индекс на `username`
- UNIQUE индекс на `email`
- UNIQUE индекс на `spotify_id`

---

## Таблица: **verification_codes** (Коды верификации для 2FA)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор записи кода |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE CASCADE, DEFAULT NULL | ID пользователя, для которого создан код. Может быть NULL. При удалении пользователя коды удаляются автоматически |
| `email` | VARCHAR(255) | NOT NULL | Email адрес, на который отправлен код подтверждения |
| `code` | VARCHAR(6) | NOT NULL, CHECK (LENGTH(code) = 6) | 6-значный код подтверждения. Должен быть точно 6 символов |
| `purpose` | VARCHAR(50) | NOT NULL, DEFAULT 'registration' | Назначение кода. Возможные значения: 'registration' (регистрация), 'login' (вход), '2fa' (двухфакторная аутентификация) |
| `expires_at` | TIMESTAMP | NOT NULL, CHECK (expires_at > created_at) | Дата и время истечения срока действия кода. Должна быть больше created_at |
| `used` | BOOLEAN | DEFAULT FALSE | Флаг использования кода. TRUE - код использован, FALSE - не использован |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время создания кода |

**Дополнительные ограничения:**
- `chk_code_expires_after_created` - CHECK ограничение: expires_at должен быть больше created_at
- `chk_code_length` - CHECK ограничение: длина кода должна быть равна 6 символам

**Индексы:**
- Неявный PRIMARY KEY на `id`

---

## Таблица: **audit_logs** (Логи аудита)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор записи лога |
| `user_id` | INTEGER | REFERENCES users(id), DEFAULT NULL | ID пользователя, выполнившего действие. Может быть NULL для системных действий |
| `action` | VARCHAR(100) | NOT NULL | Тип действия пользователя. Примеры: 'LOGIN', 'LOGOUT', 'UPDATE_FEEDBACK', 'BLOCK_USER', 'TOGGLE_ADMIN', 'EXPORT_EXCEL' |
| `details` | TEXT | DEFAULT NULL | Дополнительная информация о действии. Может содержать описание, контекст или данные действия |
| `timestamp` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время выполнения действия |

**Индексы:**
- Неявный PRIMARY KEY на `id`

**Примечание:** Используется для аудита всех важных действий пользователей в системе. Помогает отслеживать изменения и обеспечивать безопасность.

---

## Таблица: **user_sessions** (Сессии пользователей)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор сессии |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE CASCADE, NOT NULL | ID пользователя, владельца сессии. При удалении пользователя все его сессии удаляются автоматически |
| `session_token` | VARCHAR(255) | UNIQUE, NOT NULL | Уникальный токен сессии. Используется для идентификации активной сессии пользователя |
| `ip_address` | VARCHAR(45) | DEFAULT NULL | IP адрес пользователя при создании сессии. Поддерживает IPv4 и IPv6 (максимум 45 символов) |
| `user_agent` | TEXT | DEFAULT NULL | User Agent браузера пользователя. Содержит информацию о браузере и операционной системе |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время создания сессии |
| `expires_at` | TIMESTAMP | NOT NULL, CHECK (expires_at > created_at) | Дата и время истечения срока действия сессии. Должна быть больше created_at |
| `is_active` | BOOLEAN | DEFAULT TRUE | Флаг активности сессии. TRUE - сессия активна, FALSE - сессия деактивирована (пользователь вышел) |

**Дополнительные ограничения:**
- `chk_session_expires_after_created` - CHECK ограничение: expires_at должен быть больше created_at

**Индексы:**
- Неявный PRIMARY KEY на `id`
- UNIQUE индекс на `session_token`

---

## Таблица: **user_preferences** (Настройки пользователей)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор записи настроек |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE CASCADE, UNIQUE, NOT NULL | ID пользователя. UNIQUE - один набор настроек на пользователя. При удалении пользователя настройки удаляются автоматически |
| `language` | VARCHAR(10) | DEFAULT 'ru', CHECK (language IN ('ru', 'en', 'uk')) | Язык интерфейса. Допустимые значения: 'ru' (русский), 'en' (английский), 'uk' (украинский) |
| `notifications_enabled` | BOOLEAN | DEFAULT TRUE | Включены ли уведомления в системе. TRUE - включены, FALSE - выключены |
| `email_notifications` | BOOLEAN | DEFAULT TRUE | Включены ли email уведомления. TRUE - включены, FALSE - выключены |
| `default_view` | VARCHAR(50) | DEFAULT 'dashboard', CHECK (default_view IN ('dashboard', 'recommendations', 'profile')) | Вид по умолчанию при входе в систему. Допустимые значения: 'dashboard' (дашборд), 'recommendations' (рекомендации), 'profile' (профиль) |
| `timezone` | VARCHAR(50) | DEFAULT 'UTC' | Часовой пояс пользователя. По умолчанию UTC |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время последнего обновления настроек |

**Индексы:**
- Неявный PRIMARY KEY на `id`
- UNIQUE индекс на `user_id`

---

## Таблица: **feedback** (Обратная связь)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор обратной связи |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE SET NULL, DEFAULT NULL | ID пользователя, отправившего обратную связь. Может быть NULL для анонимных обращений. При удалении пользователя устанавливается в NULL |
| `feedback_type` | VARCHAR(50) | NOT NULL, DEFAULT 'other', CHECK (feedback_type IN ('bug', 'feature_request', 'complaint', 'praise', 'other')) | Тип обратной связи. Допустимые значения: 'bug' (ошибка), 'feature_request' (запрос функции), 'complaint' (жалоба), 'praise' (благодарность), 'other' (другое) |
| `title` | VARCHAR(255) | NOT NULL, CHECK (LENGTH(title) > 0 AND LENGTH(title) <= 255) | Заголовок обратной связи. Обязательное поле, длина от 1 до 255 символов |
| `message` | TEXT | NOT NULL, CHECK (LENGTH(message) > 0) | Текст сообщения обратной связи. Обязательное поле, не может быть пустым |
| `page_url` | VARCHAR(500) | DEFAULT NULL | URL страницы, с которой была отправлена обратная связь. Может быть NULL |
| `status` | VARCHAR(20) | DEFAULT 'new', CHECK (status IN ('new', 'reviewed', 'in_progress', 'resolved', 'rejected')) | Статус обработки обратной связи. Допустимые значения: 'new' (новый), 'reviewed' (рассмотрен), 'in_progress' (в работе), 'resolved' (решено), 'rejected' (отклонено) |
| `priority` | VARCHAR(10) | DEFAULT NULL, CHECK (priority IN ('low', 'medium', 'high', 'critical')) | Приоритет обратной связи. Допустимые значения: 'low' (низкий), 'medium' (средний), 'high' (высокий), 'critical' (критический). Может быть NULL |
| `admin_notes` | TEXT | DEFAULT NULL | Заметки администратора по обратной связи. Может содержать комментарии, решения или дополнительную информацию |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время создания обратной связи |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL, CHECK (updated_at >= created_at) | Дата и время последнего обновления обратной связи. Должна быть больше или равна created_at |

**Дополнительные ограничения:**
- `chk_feedback_updated_after_created` - CHECK ограничение: updated_at должен быть больше или равен created_at
- `chk_feedback_title_length` - CHECK ограничение: длина заголовка от 1 до 255 символов
- `chk_feedback_message_length` - CHECK ограничение: сообщение не может быть пустым

**Индексы:**
- Неявный PRIMARY KEY на `id`
- Индекс `idx_feedback_user_id` на `user_id`
- Индекс `idx_feedback_status` на `status`
- Индекс `idx_feedback_type` на `feedback_type`
- Индекс `idx_feedback_created_at` на `created_at` (DESC)

---

## Таблица: **users_history** (История изменений пользователей)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор записи истории |
| `user_id` | INTEGER | DEFAULT NULL | ID пользователя, для которого записана история. Может быть NULL, если пользователь был удален |
| `operation_date` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время операции изменения |
| `operation_type` | VARCHAR(10) | NOT NULL, CHECK (operation_type IN ('INSERT', 'UPDATE', 'DELETE')) | Тип операции. Допустимые значения: 'INSERT' (создание), 'UPDATE' (обновление), 'DELETE' (удаление) |
| `old_value` | JSONB | DEFAULT NULL | Старое значение записи пользователя в формате JSON. Содержит все поля до изменения. NULL для операций INSERT |
| `new_value` | JSONB | DEFAULT NULL | Новое значение записи пользователя в формате JSON. Содержит все поля после изменения. NULL для операций DELETE |
| `username` | VARCHAR(100) | DEFAULT NULL | Имя пользователя, выполнившего изменение. Может быть NULL |
| `changed_by_user_id` | INTEGER | REFERENCES users(id), DEFAULT NULL | ID пользователя, выполнившего изменение. Может быть NULL для системных изменений |

**Индексы:**
- Неявный PRIMARY KEY на `id`
- Индекс `idx_users_history_user_id` на `user_id`
- Индекс `idx_users_history_operation_date` на `operation_date` (DESC)

**Примечание:** Таблица автоматически заполняется триггером `trigger_users_history` при любых изменениях в таблице `users`.

---

## Таблица: **feedback_history** (История изменений обратной связи)

| Поле | Тип данных | Ограничения и допустимые значения | Описание |
|------|------------|----------------------------------|----------|
| `id` | SERIAL | PRIMARY KEY, NOT NULL, AUTO_INCREMENT | Уникальный идентификатор записи истории |
| `feedback_id` | INTEGER | DEFAULT NULL | ID обратной связи, для которой записана история. Может быть NULL, если feedback был удален |
| `operation_date` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP, NOT NULL | Дата и время операции изменения |
| `operation_type` | VARCHAR(10) | NOT NULL, CHECK (operation_type IN ('INSERT', 'UPDATE', 'DELETE')) | Тип операции. Допустимые значения: 'INSERT' (создание), 'UPDATE' (обновление), 'DELETE' (удаление) |
| `old_value` | JSONB | DEFAULT NULL | Старое значение записи feedback в формате JSON. Содержит все поля до изменения. NULL для операций INSERT |
| `new_value` | JSONB | DEFAULT NULL | Новое значение записи feedback в формате JSON. Содержит все поля после изменения. NULL для операций DELETE |
| `username` | VARCHAR(100) | DEFAULT NULL | Имя пользователя, выполнившего изменение. Может быть NULL |
| `changed_by_user_id` | INTEGER | REFERENCES users(id), DEFAULT NULL | ID пользователя, выполнившего изменение. Может быть NULL для системных изменений |

**Индексы:**
- Неявный PRIMARY KEY на `id`
- Индекс `idx_feedback_history_feedback_id` на `feedback_id`
- Индекс `idx_feedback_history_operation_date` на `operation_date` (DESC)

**Примечание:** Таблица автоматически заполняется триггером `trigger_feedback_history` при любых изменениях в таблице `feedback`.

---

## Представления (Views)

### **v_active_users_with_sessions**

Представление для получения активных (не заблокированных) пользователей с информацией об их активных сессиях.

**Колонки:**
- `id`, `username`, `display_name`, `email`, `role`, `is_blocked`, `email_verified`, `created_at` (из таблицы users)
- `active_sessions_count` - количество активных сессий
- `last_session_date` - дата последней сессии
- `latest_session_expires` - дата истечения последней сессии

**Условие:** Только пользователи с `is_blocked = FALSE`

---

### **v_feedback_with_users**

Представление объединяет данные feedback с информацией о пользователях.

**Колонки:**
- Все поля из таблицы `feedback`
- `user_id`, `username`, `display_name`, `email`, `user_role` (из таблицы users)
- `status_order` - числовой порядок статуса для сортировки (1-5)

**Сортировка:** По дате создания (DESC)

---

### **v_user_statistics**

Представление предоставляет статистику по каждому пользователю.

**Колонки:**
- `id`, `username`, `display_name`, `role`, `registration_date` (из таблицы users)
- `total_sessions` - общее количество сессий
- `active_sessions` - количество активных сессий
- `feedback_count` - количество отправленных обратных связей
- `new_feedback_count` - количество новых обратных связей
- `audit_logs_count` - количество записей в логах аудита
- `last_login_date` - дата последнего входа

---

### **v_recent_user_activity**

Представление объединяет информацию о сессиях и действиях пользователей.

**Колонки:**
- `activity_type` - тип активности ('session' или 'audit')
- `activity_id` - ID записи
- `user_id`, `username`, `display_name`
- `action_description` - описание действия
- `activity_date` - дата действия
- `ip_address`, `user_agent` - информация о подключении

**Ограничение:** Последние 1000 записей

**Сортировка:** По дате действия (DESC)

---

## Триггеры и функции

### Функция: **log_users_changes()**

**Тип:** TRIGGER FUNCTION (PL/pgSQL)

**Назначение:** Автоматически логирует все изменения в таблице `users` в таблицу `users_history`.

**Логика:**
- При INSERT: сохраняет новое значение в `new_value`
- При UPDATE: сохраняет старое значение в `old_value` и новое в `new_value`
- При DELETE: сохраняет старое значение в `old_value`
- Определяет пользователя, выполнившего изменение, через переменную сессии `app.current_user_id`

**Триггер:** `trigger_users_history` - срабатывает AFTER INSERT, UPDATE, DELETE на таблице `users`

---

### Функция: **log_feedback_changes()**

**Тип:** TRIGGER FUNCTION (PL/pgSQL)

**Назначение:** Автоматически логирует все изменения в таблице `feedback` в таблицу `feedback_history`.

**Логика:**
- При INSERT: сохраняет новое значение в `new_value`
- При UPDATE: сохраняет старое значение в `old_value` и новое в `new_value`
- При DELETE: сохраняет старое значение в `old_value`
- Определяет пользователя, выполнившего изменение, через переменную сессии `app.current_user_id`

**Триггер:** `trigger_feedback_history` - срабатывает AFTER INSERT, UPDATE, DELETE на таблице `feedback`

---

## Связи между таблицами

```
users (id)
  ├── verification_codes (user_id) → ON DELETE CASCADE
  ├── audit_logs (user_id) → ON DELETE SET NULL (не используется, но может быть NULL)
  ├── user_sessions (user_id) → ON DELETE CASCADE
  ├── user_preferences (user_id) → ON DELETE CASCADE, UNIQUE
  ├── feedback (user_id) → ON DELETE SET NULL
  ├── users_history (changed_by_user_id) → ON DELETE SET NULL
  └── feedback_history (changed_by_user_id) → ON DELETE SET NULL
```

---

*Документация создана на основе анализа кода приложения*
